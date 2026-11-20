"""Pure Dify code-node adapters; no credentials, HTTP or model dispatch."""
import json
import re
import time


EXECUTION_STATES = {'continue', 'paused', 'blocked', 'ready_to_package', 'completed'}
READBACK_STATES = {'historical_revision', 'complete_draft', 'incomplete_draft',
                   'engineering_reference', 'structural_reference', 'structural_reference_pending',
                   'no_completed_figures', 'filing_drawings', 'filing_review_candidate', 'no_drawings'}


def coverage_message(row, message):
    findings = row.get('domain_findings')
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get('message'), str):
                continue
            detail = finding['message'].strip()
            locations = [finding[key] for key in ('view_id', 'page_id')
                         if isinstance(finding.get(key), str) and finding[key]]
            if locations:
                detail += '（' + '，'.join(locations) + '）'
            if detail and detail not in message:
                message = '\n'.join(filter(None, (message, detail)))
    if row.get('blocking_state') == 'precise_edit_rejected':
        blocked = row.get('blocked_view')
        if isinstance(blocked, str) and blocked and blocked not in message:
            message += '\n请核对图件 ' + blocked + ' 的原图和接合范围。'
        values = row.get('completed_view_ids', ())
        views = [v for v in values if isinstance(v, str) and v] if isinstance(values, list) else []
        task = row.get('task_id')
        if views:
            message += '\n已保存：' + '、'.join(views) + '。'
            if isinstance(task, str) and re.fullmatch(r'work_[a-f0-9]{64}', task):
                message += '可发送“导出任务 ' + task + '”取回已有图件。'
    if row.get('blocking_state') == 'planning_coverage_incomplete':
        command = '修改' if row.get('planning_command') == '修改' else '规划'
        if row.get('planning_input') == 'imported':
            message += '\n请修订图单后发送“导入计划”，也可发送“规划：”并说明要保留的关系。'
        else:
            message += '\n请发送“' + command + '：”并补充要保留的关系、来源或标号位置。'
    return message


def result(items):
    rows = [x for x in items if isinstance(x, dict) and isinstance(x.get('workspace_id'), str)] if isinstance(items, list) else []
    if len(rows) != 1:
        rows = [{'workspace_id': '', 'done': 'yes', 'route': 'reply', 'execution_state': 'paused',
                 'resume_action': 'inspect_original_result', 'blocking_state': 'missing_tool_receipt',
                 'message': '本轮结果待核。请在原会话发送“查看进度”。'}]
    x = rows[0]
    todos = {}
    for field in ['failed_feature_ids', 'unknown_feature_ids', 'unreviewed_view_ids', 'missing_ids',
                  'completed_view_ids']:
        value = x.get(field, [])
        if field == 'missing_ids' and value is None:
            # Earlier persisted task snapshots used null when the blocker had no list.
            blocked_view = x.get('blocked_view')
            value = [blocked_view] if isinstance(blocked_view, str) and blocked_view else []
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError('待办字段应为字符串数组：' + field)
        todos[field] = value
    unknown = todos['unknown_feature_ids']
    if any(value.count('/') != 1 or not all(value.split('/')) for value in unknown):
        raise ValueError('实际图待确认特征应使用视图/特征标识：unknown_feature_ids')
    independent_source = bool(x.get('deferred_blocking_state')) and x.get('route') == 'execute'
    missing = x.get('missing_features', [])
    if not isinstance(missing, list) or any(not isinstance(v, str) or not v for v in missing):
        raise ValueError('引导待补充字段应为字符串数组：missing_features')
    guide_blocked = bool(missing) and not independent_source
    relation_review = x.get('pending_action') == 'professional_relation_review' and not independent_source
    actual_unknown = bool(unknown) and not independent_source
    supplier = x.get('supplier_state')
    supplier_block = supplier in ('provider_rejected', 'result_unknown')
    linked_pending = isinstance(x.get('linked_edit'), dict) and x.get('confirmed') is not True
    blocked = (bool(x.get('blocking_state')) or supplier_block or x.get('semantic_state') in {'FAIL','UNKNOWN'}
               or guide_blocked or relation_review or linked_pending
               or bool(todos['failed_feature_ids'])
               or actual_unknown
               or (bool(todos['unreviewed_view_ids']) and not independent_source)
               or (x.get('pending_action') in {'prepare_guide','private_review','actual_review'} and not independent_source))
    message=x.get('message', '')
    supplier_event = x.get('supplier_event', '')
    if supplier_event == 'inherited_refusal_lock':
        message += '\n本轮未发出图像请求，当前图单受历史供应商拒绝记录锁定。'
    elif supplier_block:
        reason = '原图像请求被供应商拒绝' if supplier=='provider_rejected' else '供应商请求结果未知'
        if reason not in message:
            message += '\n'+reason+'，请核对原始记录。'
    if x.get('blocking_state') == 'local_preflight_rejected' and '未发送图像请求' not in message:
        message += '\n本地预算前置检查未通过，未发送图像请求。'
    if actual_unknown and not all(value in message for value in unknown):
        message += '\n实际图待确认特征：' + '、'.join(unknown) + '。'
    if relation_review and '专业审阅' not in message:
        message += '\n局部连接示意需绑定原交底和本版引导图的专业审阅回执。'
    if guide_blocked:
        names={'pin-hole-fit':'销孔配合','rib-insertion-contact':'入孔肋接触',
               'channel-contour':'通道轮廓','channel-partition-solids':'实体分隔壁',
               'source-fidelity':'来源形体保真','cable-side-opening':'电缆侧焊接开口'}
        detail='、'.join(names.get(f,f)+'（'+f+'）'if f in names else f for f in missing)
        message += '\n引导待补充，视图 '+str(x.get('blocked_view')or'待定位')+'：'+detail+'。'
    filing_status = x.get('filing_status', '')
    filing_actions = x.get('filing_next_actions', [])
    if filing_status not in {'', 'PASS', 'FAIL', 'UNKNOWN'}:
        raise ValueError('申报核查状态无效')
    if not isinstance(filing_actions, list) or any(not isinstance(item, str) for item in filing_actions):
        raise ValueError('申报核查事项无效')
    if filing_actions:
        message += '\n' + '\n'.join(filing_actions)
    execution_state = x.get('execution_state', '')
    if execution_state not in EXECUTION_STATES | {''}:
        raise ValueError('执行状态无效')
    if blocked and execution_state not in {'paused', 'completed'}:
        execution_state = 'blocked'
    publication = x.get('deliverable_status', '')
    output = {'workspace_id': x['workspace_id'], 'route': 'reply' if blocked else x.get('route', 'reply'),
            'work_identity': str(x.get('work_identity') or ''),
            'done': 'yes' if blocked else x.get('done', 'no'), 'message': message,
            'execution_state': execution_state,
            'execution_stage': str(x.get('execution_stage') or ''),
            'readback': 'yes' if publication in READBACK_STATES or x.get('execution_stage') == 'inspect' else 'no',
            'missing_features': missing, 'filing_status': filing_status, 'filing_next_actions': filing_actions,
            **todos,
            **{k:str(x.get(k) or '') for k in ['technical_status','blocking_state','blocked_view','resume_action','deferred_blocking_state','deferred_blocked_view',
                                             'expression_mode','supplier_state','supplier_event','original_request_id',
                                             'semantic_state','pending_action','deliverable_status','current_revision','export_revision']}}
    if linked_pending:
        output['blocking_state'] = output['blocking_state'] or 'awaiting_linked_confirmation'
        output['resume_action'] = output['resume_action'] or 'review_linked_edit_scope'
    if supplier_block:
        output['blocking_state'] = output['blocking_state'] or supplier
        output['pending_action'] = output['pending_action'] or 'inspect_original'
        output['resume_action'] = output['resume_action'] or (
            'qualifying_evidence_and_private_recovery' if supplier=='provider_rejected' else 'inspect_original_result')
    if guide_blocked:
        output['blocking_state']=output['blocking_state']or'needs_source'
        output['pending_action']=output['pending_action']or'prepare_guide'
    if relation_review:
        output['blocking_state'] = output['blocking_state'] or 'awaiting_guide_review'
        output['resume_action'] = output['resume_action'] or 'professional_relation_review'
    if actual_unknown:
        output['blocked_view'] = output['blocked_view'] or unknown[0].split('/')[0]
        output['blocking_state'] = output['blocking_state'] or 'awaiting_visual_review'
        output['pending_action'] = output['pending_action'] or 'actual_review'
    if output['blocking_state'] == 'invalid_parent_crop':
        output['resume_action'] = 'confirm_parent_crop'
        output['message'] += '\n请在审阅图单中填写当前母图的有效像素区域，再发送“导入计划”。'
    if output['blocking_state'] == 'planning_coverage_incomplete':
        output['resume_action'] = 'refine_drawing_plan'
    if publication != 'historical_revision':
        output['message'] = coverage_message(x, output['message'])
    semantic = x.get('claims_semantic_summary')
    if isinstance(semantic, str) and semantic.strip():
        output['message'] = '\n'.join(filter(None, (output['message'], semantic)))
    return output


def preview_message_parts(message, preflight_message, deliverable_status):
    if deliverable_status in {'historical_revision', 'incomplete_draft', 'complete_draft'} or not preflight_message or preflight_message.strip() in message:
        return {'message': message}
    return {'message': message + '\n\n' + preflight_message}


def with_delivery_files(items, files, query=''):
    output = result(items)
    if output['deliverable_status'] in {'complete_draft', 'historical_revision', 'filing_drawings'}:
        rows = files if isinstance(files, list) else []
        names = {str(item.get('filename') or item.get('name') or '') for item in rows if isinstance(item, dict)}
        if not (any(name.endswith('.pdf') for name in names) and any(name.endswith('.zip') for name in names)):
            revision = output['export_revision']
            previous = '上次来源' if isinstance(query, str) and query.startswith('导出上次来源版本') else ''
            command = (query if output['deliverable_status'] == 'filing_drawings' and query
                       else f'导出{previous}版本 {revision}')
            output.update(execution_state='paused', resume_action='download_same_revision',
                          message=f'本轮下载文件待核。可发送“{command}”重新取回同版文件。'
                                  if revision else '本轮下载文件待核。可发送“查看进度”确认已保存版本。')
    return output


def claims_readback(items, files, query=''):
    rows = [row for row in items if isinstance(row, dict)] if isinstance(items, list) else []
    match = re.fullmatch(r'(查看|导出)任务\s+(work_[a-f0-9]{64})', (query or '').strip())
    action, task_id = match.groups() if match else (None, None)
    if match:
        rows = [row for row in rows if (
            row.get('task_id') == task_id or row.get('job_status') == 'not_started' and not row.get('task_id')
        )]
    output = {**result(rows), 'readback': 'no'}
    if len(rows) == 1:
        issue_rows = rows[0].get('claims_view_limit_issues')
        held = []
        if rows[0].get('claims_plan_status') == 'partial' and isinstance(issue_rows, list):
            for issue in issue_rows:
                if (isinstance(issue, dict) and isinstance(issue.get('view_id'), str)
                        and re.fullmatch(r'fig[1-9][0-9]?', issue['view_id'])
                        and type(issue.get('logical_count')) is int and type(issue.get('limit')) is int
                        and 0 < issue['limit'] < issue['logical_count']):
                    held.append(f"图 {issue['view_id'][3:]}（{issue['logical_count']}/{issue['limit']} 条引文）")
        if held:
            raw_ids = rows[0].get('completed_view_ids')
            available = [value[3:] for value in raw_ids
                         if isinstance(value, str) and re.fullmatch(r'fig[1-9][0-9]?',value)] if isinstance(raw_ids, list) else []
            prefix = '已保存同版部分图单。'
            if available:
                prefix += '可取回图 ' + '、'.join(available) + '。'
            output['message'] = prefix + '待核' + '、'.join(held) + '。\n' + output['message']
        vote = rows[0].get('claims_vote_review')
        recovery = rows[0].get('claims_recovery')
        if isinstance(recovery, dict) and rows[0].get('job_status') == 'completed':
            strategy = recovery.get('strategy')
            if strategy == 'cap-1':
                output['message'] += '\n首发截断后，原任务以 25,000 输出 token 上限继续并取得图单。'
            elif strategy == 'compact-1':
                output['message'] += '\n本图单由原任务内的一次压缩续做取得。'
        if isinstance(vote, dict) and all(type(vote.get(key)) is int and 0 <= vote[key] <= 14
                                          for key in ('disagreements', 'rejudged', 'uncertain')):
            output['message'] += (f"\nLuna 逐题判断有 {vote['disagreements']} 项分歧，"
                                  f"复判 {vote['rejudged']} 项，保留待核 {vote['uncertain']} 项。")
            low = vote.get('low_confidence')
            if type(low) is int and 0 < low <= 11:
                output['message'] += f' 同票低置信 {low} 项，供核对。'
    if not match:
        return output
    command = f'{action}任务 {task_id}'
    if output['blocking_state'] == 'missing_tool_receipt':
        output['message'] = f'本轮结果待核。请在原会话发送“{command}”。'
        return output
    hashes = rows[0].get('file_hashes') if len(rows) == 1 and rows[0].get('task_id') == task_id else None
    if action != '导出' or not isinstance(hashes, dict):
        return output
    plan_name = next((name for name in ('claims-plan.json','material-plan.json') if name in hashes), None)
    if not plan_name:
        return output
    filenames = [row.get('filename') or row.get('name') for row in files
                 if isinstance(row, dict)] if isinstance(files, list) else []
    if filenames == [plan_name] and len(files) == 1:
        output['readback'] = 'yes'
        pending = rows[0].get('claims_guard_pending')
        if rows[0].get('claims_plan_status') == 'partial' and type(pending) is int and pending > 0:
            output['message'] = '已取回同版部分图单，守卫缺证可在图单中逐条核对。\n' + output['message']
    else:
        notice = f'本轮图单文件未送达。可发送“{command}”重新取回。'
        output.update(execution_state='paused', resume_action='download_same_revision',
                      message='\n'.join(filter(None, (notice, output['message']))))
    return output


def loop_outcome(step_done, review_done):
    return {'done': 'no' if step_done == review_done == 'no' else 'yes'}


def plan_admission(query):
    text = (query or '').strip()
    start = text.startswith(('规划', '生成：', '生成:', '继续准备复杂图', '请帮我规划',
                             '请你规划', '帮我规划', '请规划'))
    return {'ack': 'yes' if start else 'no'}


def durable_inspection(query, workspace_id, *, claims_only=False):
    query = (query or '').strip()
    status_query = query == '查看进度' or (claims_only and query == '继续')
    return {'route': 'inspect' if (status_query and workspace_id)
            or re.fullmatch(r'(?:查看|导出)任务\s+work_[a-f0-9]{64}', query) else 'intake'}


def turn_clock():
    return {'started_at': str(time.time())}


def stop_before_deadline(step_done, review_done, started_at):
    spent = time.time() - float(started_at)
    return {'done': 'yes'} if spent >= 45 else loop_outcome(step_done, review_done)


def inputs(patent_type, technical_type, application_stage):
    patents = {'发明': 'invention', '实用新型': 'utility_model', '外观设计': 'design'}
    technologies = {'自动识别': 'auto', '机械结构': 'mechanical', '电气电路': 'electrical',
        '机电结构': 'mechanical,electrical', '软件/计算机方法': 'computer_method', '通信': 'communication', '制造工艺': 'manufacturing_process',
        '化学材料': 'chemical_material', '生物医学': 'bio_medical', '半导体': 'semiconductor'}
    stages = {'申请前': 'draft', '已申请': 'filed'}
    patents[''] = ''
    technologies[''] = ''
    technologies['自动识别'] = ''
    stages[''] = ''
    patents['待确认'] = ''
    technologies['待确认'] = ''
    stages['待确认'] = ''
    patent_type = patent_type or ''
    technical_type = technical_type or ''
    application_stage = application_stage or ''
    if patent_type not in patents or technical_type not in technologies or application_stage not in stages:
        raise ValueError('请选择实际申请类型、技术类型和申请阶段')
    return {'patent_type': patents[patent_type], 'technical_type': technologies[technical_type], 'application_stage': stages[application_stage]}


def image_route_result(items):
    rows = [x for x in items if isinstance(x, dict) and 'workspace_id' in x] if isinstance(items, list) else []
    if len(rows) != 1:
        return {'scope': '', 'route': 'reply', 'message': '复杂图准备待核。请在原会话发送“查看进度”。'}
    return {key: str(rows[0].get(key) or '') for key in ('scope', 'route', 'message')}
