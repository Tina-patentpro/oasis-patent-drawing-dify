"""Pure Dify code-node adapters; no credentials, HTTP or model dispatch."""
import json


def result(items):
    if not isinstance(items, list): raise ValueError('工具结果应为JSON数组')
    rows = [x for x in items if isinstance(x, dict) and 'workspace_id' in x]
    if len(rows) != 1: raise ValueError('缺少唯一工作区结果；未自动重试')
    x = rows[0]
    todos = {}
    for field in ['failed_feature_ids', 'unknown_feature_ids', 'unreviewed_view_ids']:
        value = x.get(field, [])
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
    blocked = (bool(x.get('blocking_state')) or supplier_block or x.get('semantic_state') in {'FAIL','UNKNOWN'}
               or guide_blocked or relation_review
               or bool(todos['failed_feature_ids'])
               or actual_unknown
               or (bool(todos['unreviewed_view_ids']) and not independent_source)
               or (x.get('pending_action') in {'prepare_guide','private_review','actual_review'} and not independent_source))
    message=x.get('message', '')
    if supplier_block:
        reason = '供应商拒绝本次请求' if supplier=='provider_rejected' else '供应商请求结果未知'
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
    output = {'workspace_id': x['workspace_id'], 'route': 'reply' if blocked else x.get('route', 'reply'),
            'done': 'yes' if blocked else x.get('done', 'no'), 'message': message,
            'missing_features': missing,
            **todos,
            **{k:str(x.get(k) or '') for k in ['technical_status','blocking_state','blocked_view','resume_action','deferred_blocking_state','deferred_blocked_view',
                                             'expression_mode','supplier_state','semantic_state','pending_action']}}
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
    return output


def loop_outcome(step_done, review_done):
    return {'done': 'no' if step_done == review_done == 'no' else 'yes'}


def inputs(patent_type, technical_type, application_stage):
    patents = {'发明': 'invention', '实用新型': 'utility_model', '外观设计': 'design'}
    technologies = {'自动识别': 'auto', '机械结构': 'mechanical', '电气电路': 'electrical',
        '机电结构': 'mechanical,electrical', '软件/计算机方法': 'computer_method', '通信': 'communication', '制造工艺': 'manufacturing_process',
        '化学材料': 'chemical_material', '生物医学': 'bio_medical', '半导体': 'semiconductor'}
    stages = {'申请前': 'draft', '已申请': 'filed'}
    if patent_type not in patents or technical_type not in technologies or application_stage not in stages:
        raise ValueError('请选择实际申请类型、技术类型和申请阶段')
    return {'patent_type': patents[patent_type], 'technical_type': technologies[technical_type], 'application_stage': stages[application_stage]}
