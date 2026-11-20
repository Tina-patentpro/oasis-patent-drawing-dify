"""v0.3.0: files/types -> persistent workspace -> explicit per-figure Loop."""
from pathlib import Path
import argparse
import json
import uuid
import yaml

HERE = Path(__file__).resolve().parent
PROVIDER = 'tina-patentpro/oasisintwrite_patent_drawing/oasisintwrite_patent_drawing'


def build(uid=None, credential_id=None, *, name='专利附图集-GPT R22（对话采集候选）', figure_routing=False, complex_agent=False, agent_preparation=True, agent_uid=None, durable_work=False, claims_only=False, mixed_materials=False):
    if mixed_materials:
        claims_only = True
    if claims_only:
        durable_work, complex_agent = True, False
    if complex_agent and not figure_routing:
        raise ValueError('complex preparation requires figure routing')
    if durable_work and not claims_only and not (figure_routing and complex_agent and agent_preparation):
        raise ValueError('durable work needs native figure routing and one signed Agent graph for both branches')
    nodes = [];edges = [];source = (HERE / 'quality_flow.py').read_text() + '\n' + (HERE / 'intake_flow.py').read_text()
    if figure_routing or claims_only:
        source += '\n' + (HERE / 'figure_flow.py').read_text(encoding='utf-8')
    def node(n, typ, title, **data):
        nodes.append({'id': n, 'type': 'custom', 'position': {'x': len(nodes) * 300, 'y': 0}, 'width': 244, 'height': 100,
            'sourcePosition': 'right', 'targetPosition': 'left', 'data': {'type': typ, 'title': title, 'desc': '', 'selected': False, **data}})
    def edge(a, b, handle='source', inside=False):
        kinds = {x['id']: x['data']['type'] for x in nodes}
        edges.append({'id': a + '-' + handle + '-' + b, 'source': a, 'target': b, 'sourceHandle': handle, 'targetHandle': 'target', 'type': 'custom',
            'data': {'sourceType': kinds[a], 'targetType': kinds[b], 'isInLoop': inside, 'loop_id': 'q_loop' if inside else None}})
    def code(n, title, expression, variables, outputs, inside=False):
        defaults = {'files': '=None', 'query': "=''"}
        required = [key for key in variables if key not in defaults]
        arguments = required + [key + defaults[key] for key in variables if key in defaults]
        node(n, 'code', title, code_language='python3', code=source + '\n\ndef main(' + ','.join(arguments) + '):\n return ' + expression + '\n',
            variables=[{'variable': k, 'value_selector': v} for k, v in variables.items()],
            outputs={k: {'type': t, 'children': None} for k, t in outputs.items()},
            error_strategy='fail-branch', **({'isInLoop': True, 'loop_id': 'q_loop'} if inside else {}))
    def tool(n, title, action, parameters, inside=False, fixed=None):
        if action not in {'intake', 'inspect_work'} and 'expected_revision' not in parameters:
            parameters = {**parameters, 'expected_revision': ['q_intake_result', 'intake_revision']}
        if action in {'step', 'preflight', 'review_status', 'package'}:
            parameters['expected_work_identity'] = ['q_result', 'work_identity']
        params = {k: {'type': 'variable', 'value': v} for k, v in parameters.items()}
        params['action'] = {'type': 'mixed', 'value': action}
        params.update({name: {'type': 'mixed', 'value': value} for name, value in (fixed or {}).items()})
        if durable_work:
            params['durable_work'] = {'type': 'mixed', 'value': 'yes'}
        if complex_agent and action == 'receive':
            params['require_image_route'] = {'type': 'mixed', 'value': 'agent' if agent_preparation else 'direct'}
        node(n, 'tool', title, provider_type='builtin', provider_id=PROVIDER, provider_name=PROVIDER,
            tool_name='patent_drawing_workspace', tool_label='专利附图工作区', tool_configurations={}, tool_parameters=params,
            tool_node_version='2', plugin_unique_identifier=uid, credential_id=credential_id, is_team_authorization=True,
            retry_config={'retry_enabled': False, 'max_retries': 0, 'retry_interval': 1000},
            error_strategy='fail-branch', **({'isInLoop': True, 'loop_id': 'q_loop'} if inside else {}))
    def condition(case_id, selector, value):
        return {'case_id': case_id, 'logical_operator': 'and', 'conditions': [
            {'id': case_id, 'variable_selector': selector, 'varType': 'string',
             'comparison_operator': 'is', 'value': value}]}
    fields = [
        {'variable': 'materials', 'label': '交底正文（已有PDF可留空）', 'type': 'paragraph', 'required': False, 'max_length': 64000, 'default': ''},
        {'variable': 'plan_json', 'label': '审阅图单JSON（高级验证，可留空）', 'type': 'paragraph', 'required': False, 'max_length': 400000, 'default': ''},
        {'variable': 'patent_type', 'label': '专利申请类型（可在对话中补充）', 'type': 'select', 'required': False, 'options': ['待确认', '发明', '实用新型', '外观设计'], 'default': '待确认'},
        {'variable': 'technical_type', 'label': '技术类型（可自动分析）', 'type': 'select', 'required': False, 'options': ['待确认', '自动识别', '机械结构', '电气电路', '机电结构', '软件/计算机方法', '通信', '制造工艺', '化学材料', '生物医学', '半导体'], 'default': '待确认'},
        {'variable': 'application_stage', 'label': '申请阶段（可在对话中补充）', 'type': 'select', 'required': False, 'options': ['待确认', '申请前', '已申请'], 'default': '待确认'},
        {'variable': 'image_budget', 'label': '图集附加调用上限（0按操作员授权）', 'type': 'number', 'required': False, 'default': 0},
    ]
    node('q_start', 'start', '交底与用途', variables=fields)
    if not durable_work:
        code('q_clock', '记录本轮开始时间', 'turn_clock()', {}, {'started_at': 'string'})
    code('q_types', '绑定申请和技术类型', 'inputs(patent_type,technical_type,application_stage)',
        {x: ['q_start', x] for x in ['patent_type', 'technical_type', 'application_stage']},
        {x: 'string' for x in ['patent_type', 'technical_type', 'application_stage']})
    intake_params = {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['conversation', 'drawing_workspace'], 'query': ['sys', 'query'],
        'files': ['sys', 'files'], 'materials': ['q_start', 'materials'], 'plan_json': ['q_start', 'plan_json'],
        'image_budget': ['q_start', 'image_budget'], **{x: ['q_types', x] for x in ['patent_type', 'technical_type', 'application_stage']}}
    intake_params.update(expected_revision=['conversation', 'intake_revision'], operation_id=['sys', 'workflow_run_id'])
    tool('q_intake', '核对权利要求正文' if claims_only and not mixed_materials else '分析材料并补全信息', 'intake',
         intake_params, fixed={'source_mode': 'mixed_materials' if mixed_materials else 'claims_only'} if claims_only else None)
    intake_outputs = {x: 'string' for x in ['workspace_id', 'intake_revision', 'route', 'message', 'patent_type', 'technical_type', 'application_stage']}
    code('q_intake_result', '读取采集摘要和修订号', 'intake_result(items)', {'items': ['q_intake', 'json']}, intake_outputs)
    node('q_intake_remember', 'assigner', '保存本会话采集引用', version='2', items=[
        {'variable_selector': ['conversation', 'intake_revision'], 'input_type': 'variable', 'operation': 'over-write', 'value': ['q_intake_result', 'intake_revision']},
        {'variable_selector': ['conversation', 'drawing_workspace'], 'input_type': 'variable', 'operation': 'over-write', 'value': ['q_intake_result', 'workspace_id']}])
    node('q_intake_switch', 'if-else', '补充信息或进入工作区', cases=[{'case_id': 'forward', 'logical_operator': 'and', 'conditions': [
        {'id': 'intake-confirmed', 'variable_selector': ['q_intake_result', 'route'], 'varType': 'string', 'comparison_operator': 'is', 'value': 'forward'}]}])
    node('q_intake_answer', 'answer', '采集摘要与下一问', answer='{{#q_intake.files#}}\n\n{{#q_intake_result.message#}}')
    params = {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_intake_result', 'workspace_id'], 'query': ['sys', 'query'],
        'plan_json': ['q_start', 'plan_json'], 'image_budget': ['q_start', 'image_budget'],
        **{x: ['q_intake_result', x] for x in ['patent_type', 'technical_type', 'application_stage']}}
    if figure_routing and not durable_work:
        families = json.loads((HERE.parent / 'plugin/_assets/figure_routing.json').read_text(encoding='utf-8'))
        tool('q_figure_decision', 'GPT-6 Luna 判断所需图型（可多选）', 'route_figures',
             {key: params[key] for key in ('context_id', 'workspace_id', 'query', 'plan_json')})
        code('q_figure_selection', '读取多图型判断', 'figure_selection(items)',
             {'items': ['q_figure_decision', 'json']}, {'operation_id': 'string', 'selected': 'array[string]'})
        code('q_plan_admission', '判断是否先回复规划受理', 'plan_admission(query)',
             {'query': ['sys', 'query']}, {'ack': 'string'})
        node('q_plan_ack_switch', 'if-else', '规划前回复', cases=[condition('ack', ['q_plan_admission', 'ack'], 'yes')])
        node('q_plan_ack', 'answer', '图单规划已受理', answer='已保存本次材料，正在核对图型及图单。可在原会话发送“查看进度”。')
        edge('q_intake_switch', 'q_plan_admission', 'forward')
        edge('q_plan_admission', 'q_plan_ack_switch')
        edge('q_plan_ack_switch', 'q_plan_ack', 'ack')
        edge('q_plan_ack', 'q_figure_decision')
        edge('q_plan_ack_switch', 'q_figure_decision', 'false')
        edge('q_figure_decision', 'q_figure_selection')
        merge_inputs = {'operation_id': ['q_figure_selection', 'operation_id']}
        for family, profile in families.items():
            prompt_node = 'q_prompt_' + family
            code(prompt_node, profile['label'] + '制图指令',
                 'figure_instruction(selected, ' + repr(family) + ', ' + repr(profile['instruction']) + ')',
                 {'selected': ['q_figure_selection', 'selected']}, {'instruction': 'string'})
            edge('q_figure_selection', prompt_node)
            merge_inputs[family] = [prompt_node, 'instruction']
        code('q_figure_merge', '合并图型指令', 'merge_figure_instructions(' + ', '.join(
            ['operation_id', *[family + '=' + family for family in families]]) + ')',
            merge_inputs, {'routing_json': 'string'})
        for family in families:
            edge('q_prompt_' + family, 'q_figure_merge')
        params['figure_routing_json'] = ['q_figure_merge', 'routing_json']
    tool('q_receive', '原图修改补充／无图新建规划', 'receive', params)
    outputs = {x: 'string' for x in ['workspace_id', 'work_identity', 'route', 'done', 'message','technical_status','blocking_state','blocked_view','resume_action','deferred_blocking_state','deferred_blocked_view',
                                    'execution_state','execution_stage','readback',
                                    'expression_mode','supplier_state','supplier_event','original_request_id',
                                    'semantic_state','pending_action','deliverable_status','current_revision','export_revision','filing_status']}
    outputs.update({x: 'array[string]' for x in ['failed_feature_ids', 'unknown_feature_ids', 'unreviewed_view_ids', 'missing_features', 'missing_ids', 'filing_next_actions','completed_view_ids']})
    code('q_result', '读取工作区与任务状态', 'with_delivery_files(items, files, query)',
         {'items': ['q_receive', 'json'], 'files': ['q_receive', 'files'], 'query': ['sys', 'query']}, outputs)
    if durable_work:
        early = {'query': ['sys', 'query'], 'workspace_id': ['conversation', 'drawing_workspace']}
        actor = {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_intake_result', 'workspace_id']}
        work_outputs = {key: 'string' for key in (
            'route', 'scope', 'task_id', 'job_status', 'prepared', 'message', 'blocking_state', 'resume_action')}
        code('q_durable_early', '识别持久任务查询',
             f'durable_inspection(query,workspace_id,claims_only={claims_only!r})', early, {'route': 'string'})
        node('q_durable_early_switch', 'if-else', '读取已提交进度或继续采集',
             cases=[condition('inspect', ['q_durable_early', 'route'], 'inspect')])
        tool('q_inspect_work', '读取任务进度或原文件', 'inspect_work',
             {**early, 'context_id': actor['context_id'], 'expected_revision': ['conversation', 'intake_revision']})
        code('q_inspect_result', '读取持久任务回执', 'durable_status(items)',
             {'items': ['q_inspect_work', 'json']}, work_outputs)
        node('q_inspect_switch', 'if-else', '显示可核对的复杂图准备',
             cases=[condition('prepared', ['q_inspect_result', 'prepared'], 'yes')])
        readback = 'claims_readback' if claims_only else 'with_delivery_files'
        code('q_work_readback', '核对下载文件', f'{readback}(items,files,query)',
             {'items': ['q_inspect_work', 'json'], 'files': ['q_inspect_work', 'files'], 'query': ['sys', 'query']}, outputs)
        node('q_readback_answer', 'answer', '任务进度与同版文件',
             answer='{{#q_inspect_work.files#}}\n\n{{#q_work_readback.message#}}')
        if claims_only:
            node('q_claims_file_switch', 'if-else', '确认文件与原任务一致',
                 cases=[condition('download', ['q_work_readback', 'readback'], 'yes')])
            node('q_claims_status_answer', 'answer', '原任务状态及待核关系',
                 answer='{{#q_work_readback.message#}}')
        tool('q_accept_saved_preparation', '核对本版准备回执', 'accept_image_preparation', {
             'context_id': actor['context_id'], 'workspace_id': ['conversation', 'drawing_workspace'],
             'expected_revision': ['conversation', 'intake_revision'],
             'task_id': ['q_inspect_result', 'task_id'], 'scope': ['q_inspect_result', 'scope']})
        code('q_prepared_result', '显示已核参考与约束', 'durable_status(items)',
             {'items': ['q_accept_saved_preparation', 'json']}, work_outputs)
        node('q_prepared_answer', 'answer', '复杂图准备与下一步', answer='{{#q_prepared_result.message#}}')
        tool('q_admit_work', '核对并登记可执行任务', 'admit_work',
             {**actor, **{key: params[key] for key in ('query', 'plan_json', 'image_budget')}})
        code('q_admission_result', '读取登记或原生交接范围', 'durable_status(items)',
             {'items': ['q_admit_work', 'json']}, work_outputs)
        node('q_admission_switch', 'if-else', '进入原生 Agent、已有任务或图单',
             cases=[condition('agent', ['q_admission_result', 'route'], 'agent'),
                    condition('agent_poll', ['q_admission_result', 'route'], 'agent_poll'),
                    condition('receive', ['q_admission_result', 'route'], 'receive')])
        node('q_admission_answer', 'answer', '任务已受理或需要核对', answer='{{#q_admission_result.message#}}')
        agent_inputs = {**actor, 'scope': ['q_admission_result', 'scope'],
                        'expected_revision': ['q_intake_result', 'intake_revision']}
        node('q_complex_agent', 'agent', '交接已核复杂图范围',
             agent_strategy_provider_name='tina-patentpro/patent_drawing_preparation/patent_drawing_agent',
             agent_strategy_name='prepare_complex_drawing', agent_strategy_label='准备复杂图',
             agent_parameters={**{key: {'type': 'variable', 'value': selector} for key, selector in agent_inputs.items()},
                               'durable_work': {'type': 'mixed', 'value': 'yes'}},
             tool_node_version='2', error_strategy='fail-branch')
        tool('q_agent_enqueue', '按原会话登记复杂图准备', 'enqueue_work',
             {**agent_inputs, 'preparation_json': ['q_complex_agent', 'text']},
             fixed={'work_kind': 'agent_prepare'})
        code('q_agent_enqueue_result', '读取复杂图准备回执', 'durable_status(items)',
             {'items': ['q_agent_enqueue', 'json']}, work_outputs)
        node('q_agent_enqueued_answer', 'answer', '复杂图准备受理', answer='{{#q_agent_enqueue_result.message#}}')
        tool('q_agent_poll', '查询原生准备的持久任务', 'inspect_work', agent_inputs)
        code('q_agent_progress', '读取已登记的准备任务', 'durable_status(items)',
             {'items': ['q_agent_poll', 'json']}, work_outputs)
        node('q_agent_answer', 'answer', '复杂图任务及进度', answer='{{#q_agent_progress.message#}}')
        node('q_durable_draw_switch', 'if-else', '显示预览或登记已确认的绘图',
             cases=[condition('execute', ['q_result', 'route'], 'execute')])
        tool('q_enqueue_work', '登记已确认的绘图和成套任务', 'enqueue_work',
             {**actor, 'expected_work_identity': ['q_result', 'work_identity']},
             fixed={'work_kind': 'draw'})
        code('q_enqueued_result', '读取已保存的任务回执', 'durable_status(items)',
             {'items': ['q_enqueue_work', 'json']}, work_outputs)
        node('q_enqueue_answer', 'answer', '绘图任务及下一步', answer='{{#q_enqueued_result.message#}}')
        node('q_preview', 'answer', '当前图单和原文件', answer='{{#q_receive.files#}}\n\n{{#q_result.message#}}')
        node('q_recovery_answer', 'answer', '核对最近提交的任务状态',
             answer='请在原会话发送“查看进度”核对最后提交的任务，已有完整图件可用“导出版本 编号”取回。')
        for a, b in [
            ('q_start', 'q_durable_early'), ('q_durable_early', 'q_durable_early_switch'),
            ('q_inspect_work', 'q_inspect_result'), ('q_inspect_result', 'q_inspect_switch'),
            ('q_accept_saved_preparation', 'q_prepared_result'), ('q_prepared_result', 'q_prepared_answer'),
            ('q_work_readback', 'q_claims_file_switch' if claims_only else 'q_readback_answer'), ('q_types', 'q_intake'), ('q_intake', 'q_intake_result'),
            ('q_intake_result', 'q_intake_remember'), ('q_intake_remember', 'q_intake_switch'),
            ('q_admit_work', 'q_admission_result'), ('q_admission_result', 'q_admission_switch'),
            ('q_agent_poll', 'q_agent_progress'), ('q_agent_progress', 'q_agent_answer'),
            ('q_receive', 'q_result'), ('q_result', 'q_durable_draw_switch'),
            ('q_enqueue_work', 'q_enqueued_result'), ('q_enqueued_result', 'q_enqueue_answer'),
        ]:
            edge(a, b)
        for a, handle, b in [
            ('q_durable_early_switch', 'inspect', 'q_inspect_work'), ('q_durable_early_switch', 'false', 'q_types'),
            ('q_inspect_switch', 'prepared', 'q_accept_saved_preparation'), ('q_inspect_switch', 'false', 'q_work_readback'),
            ('q_intake_switch', 'forward', 'q_admit_work'), ('q_intake_switch', 'false', 'q_intake_answer'),
            ('q_admission_switch', 'agent', 'q_complex_agent'), ('q_admission_switch', 'agent_poll', 'q_agent_poll'),
            ('q_admission_switch', 'receive', 'q_receive'), ('q_admission_switch', 'false', 'q_admission_answer'),
            ('q_durable_draw_switch', 'execute', 'q_enqueue_work'), ('q_durable_draw_switch', 'false', 'q_preview'),
        ]:
            edge(a, b, handle)
        if claims_only:
            edge('q_claims_file_switch', 'q_readback_answer', 'download')
            edge('q_claims_file_switch', 'q_claims_status_answer', 'false')
        edge('q_complex_agent', 'q_agent_enqueue')
        edge('q_agent_enqueue', 'q_agent_enqueue_result')
        edge('q_agent_enqueue_result', 'q_agent_enqueued_answer')
        for n in nodes:
            if n['data'].get('error_strategy') == 'fail-branch':
                target = 'q_agent_poll' if n['id'] in {'q_complex_agent', 'q_agent_enqueue'} else 'q_recovery_answer'
                edge(n['id'], target, 'fail-branch')
        app = assemble(nodes, edges, name, uid, agent_uid, figure_routing, complex_agent, durable_work)
        if claims_only:
            return restrict_claims_graph(app, mixed_materials=mixed_materials)
        return app
    node('q_result_switch', 'if-else', '直接返回已保存结果', cases=[
        condition('readback', ['q_result', 'readback'], 'yes'),
        condition('needs_inspection', ['q_result', 'execution_state'], 'paused')])
    node('q_readback_answer', 'answer', '已保存的图集或状态',
         answer='{{#q_receive.files#}}\n\n{{#q_result.message#}}')
    node('q_remember', 'assigner', '保存本会话工作区引用', version='2', items=[{'variable_selector': ['conversation', 'drawing_workspace'], 'input_type': 'variable', 'operation': 'over-write', 'value': ['q_result', 'workspace_id']}])
    tool('q_preflight','来源、引导与母图区域检查','preflight',{'context_id':['sys','conversation_id'],'workspace_id':['q_result','workspace_id']})
    code('q_preflight_result','读取前置阻断','result(items)',{'items':['q_preflight','json']},outputs)
    node('q_switch', 'if-else', '展示改动范围或执行已确认图单', cases=[{'case_id': 'execute', 'logical_operator': 'and', 'conditions': [{'id':'user-requested-execution','variable_selector':['q_result','route'],'varType':'string','comparison_operator':'is','value':'execute'}, {'id': 'run-plan', 'variable_selector': ['q_preflight_result', 'route'], 'varType': 'string', 'comparison_operator': 'is', 'value': 'execute'}]}])
    code('q_preview_message', '展示当前回复或取回的历史版本',
        'preview_message_parts(message,preflight_message,deliverable_status)',
        {'message': ['q_result', 'message'], 'preflight_message': ['q_preflight_result', 'message'],
         'deliverable_status': ['q_result', 'deliverable_status']}, {'message': 'string'})
    node('q_preview', 'answer', '来源图文与图单', answer='{{#q_receive.files#}}\n\n{{#q_preview_message.message#}}')
    if complex_agent:
        tool('q_complexity', 'Luna 逐图判断复杂度', 'route_images',
             {key: params[key] for key in ('context_id', 'workspace_id', 'query')})
        code('q_route_result', '读取已核图像路线', 'image_route_result(items)',
             {'items': ['q_complexity', 'json']}, {key: 'string' for key in ('scope', 'route', 'message')})
        node('q_route_switch', 'if-else', '复杂图进入 Agent 准备', cases=[
            {'case_id': 'agent', 'logical_operator': 'and', 'conditions': [
                {'id': 'prepare', 'variable_selector': ['q_route_result', 'route'],
                 'varType': 'string', 'comparison_operator': 'is', 'value': 'agent' if agent_preparation else 'skip_agent'}]}])
        agent_inputs = {**{key: params[key] for key in ('context_id', 'workspace_id')},
                        'scope': ['q_route_result', 'scope']}
        node('q_complex_agent', 'agent', '准备经核复杂图',
             agent_strategy_provider_name='tina-patentpro/patent_drawing_preparation/patent_drawing_agent',
             agent_strategy_name='prepare_complex_drawing', agent_strategy_label='准备复杂图',
             agent_parameters={key: {'type': 'variable', 'value': selector} for key, selector in agent_inputs.items()},
             tool_node_version='2', error_strategy='fail-branch')
        tool('q_apply_preparation', '核对并保存 Agent 准备', 'accept_image_preparation',
             {**agent_inputs, 'preparation_json': ['q_complex_agent', 'text']})
        code('q_prepared_result', '读取已保存的准备范围', 'image_route_result(items)',
             {'items': ['q_apply_preparation', 'json']}, {key: 'string' for key in ('scope', 'route', 'message')})
        node('q_prepared_answer', 'answer', '复杂图图单与执行模型',
             answer='{{#q_preview_message.message#}}\n\n{{#q_prepared_result.message#}}')
        node('q_complexity_answer', 'answer', '当前图单与图像准备',
             answer='{{#q_preview_message.message#}}\n{{#q_route_result.message#}}')
        node('q_complexity_switch', 'if-else', '仅为候选图单判断复杂度',
             cases=[condition('candidate', ['q_result', 'execution_stage'], 'proposed')])
        edge('q_preview_message', 'q_complexity_switch')
        edge('q_complexity_switch', 'q_complexity', 'candidate')
        edge('q_complexity_switch', 'q_preview', 'false')
        edge('q_complexity', 'q_route_result')
        edge('q_route_result', 'q_route_switch')
        edge('q_route_switch', 'q_complex_agent', 'agent')
        edge('q_route_switch', 'q_complexity_answer', 'false')
        edge('q_complex_agent', 'q_apply_preparation')
        edge('q_apply_preparation', 'q_prepared_result')
        edge('q_prepared_result', 'q_prepared_answer')

    node('q_loop', 'loop', '按图单逐图执行', start_node_id='q_loop_start', loop_count=24, error_strategy='fail-branch',
        break_conditions=[{'id': 'completed-set', 'variable_selector': ['q_loop', 'done'], 'varType': 'string', 'comparison_operator': 'is', 'value': 'yes'}], logical_operator='and',
        loop_variables=[{'label': 'workspace_id', 'var_type': 'string', 'value_type': 'variable', 'value': ['q_result', 'workspace_id']},
                        {'label': 'done', 'var_type': 'string', 'value_type': 'constant', 'value': 'no'},
                        {'label': 'interrupted', 'var_type': 'string', 'value_type': 'constant', 'value': 'no'}],
        width=1200, height=450)
    node('q_loop_start', 'loop-start', '开始当前图', isInLoop=True, loop_id='q_loop')
    tool('q_step', '执行并持久保存当前图', 'step', {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_loop', 'workspace_id']}, True)
    tool('q_review','核对本图实际技术状态','review_status',{'context_id':['sys','conversation_id'],'workspace_id':['q_loop','workspace_id']},True)
    code('q_review_result','读取技术阻断','result(items)',{'items':['q_review','json']},outputs,True)
    code('q_step_result', '读取当前图完成状态', 'result(items)', {'items': ['q_step', 'json']}, outputs, True)
    code('q_loop_outcome', '按执行与审阅状态推进', 'stop_before_deadline(step_done, review_done, started_at)',
         {'step_done': ['q_step_result', 'done'], 'review_done': ['q_review_result', 'done'],
          'started_at': ['q_clock', 'started_at']}, {'done': 'string'}, True)
    node('q_save_done', 'assigner', '推进显式循环状态', version='2', isInLoop=True, loop_id='q_loop', items=[{'variable_selector': ['q_loop', 'done'], 'input_type': 'variable', 'operation': 'over-write', 'value': ['q_loop_outcome', 'done']}])
    node('q_stop_on_error', 'assigner', '保存本轮暂停状态', version='2', isInLoop=True, loop_id='q_loop',
         items=[{'variable_selector': ['q_loop', 'done'], 'input_type': 'constant', 'operation': 'set', 'value': 'yes'},
                {'variable_selector': ['q_loop', 'interrupted'], 'input_type': 'constant', 'operation': 'set', 'value': 'yes'}])
    tool('q_completion', '读取已保存的执行进度', 'review_status',
         {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_result', 'workspace_id']})
    code('q_completion_result', '检查完整打包状态', 'result(items)', {'items': ['q_completion', 'json']}, outputs)
    node('q_package_switch', 'if-else', '仅打包已通过校验的图集', cases=[
        condition('ready', ['q_completion_result', 'execution_state'], 'ready_to_package')])
    node('q_pending_answer', 'answer', '进度和下一步', answer='{{#q_completion_result.message#}}')
    node('q_work_ack', 'answer', '已受理图集处理', answer='已受理图集处理。若连接中断，可在本会话发送“查看进度”。')
    node('q_recovery_answer', 'answer', '本轮结果待核',
         answer='本轮结果待核。请在原会话发送“查看进度”。已有版本可用“导出”或“导出版本 编号”取回。')
    tool('q_package', '核查成套图并生成PDF/ZIP', 'package', {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_result', 'workspace_id']})
    code('q_final_result', '读取核查与交付状态', 'with_delivery_files(items, files)',
         {'items': ['q_package', 'json'], 'files': ['q_package', 'files']}, outputs)
    node('q_answer', 'answer', '成套附图草稿与核查', answer='{{#q_package.files#}}\n\n{{#q_final_result.message#}}')
    node('q_loop_result', 'if-else', '检查本轮是否中断',
         cases=[condition('interrupted', ['q_loop', 'interrupted'], 'yes')])
    for a, b in [('q_start', 'q_clock'), ('q_clock', 'q_types'), ('q_types', 'q_intake'), ('q_intake', 'q_intake_result'), ('q_intake_result', 'q_intake_remember'), ('q_intake_remember', 'q_intake_switch'), ('q_receive', 'q_result'), ('q_result', 'q_result_switch'), ('q_remember','q_preflight'),('q_preflight','q_preflight_result'),('q_preflight_result', 'q_switch'),
        ('q_work_ack', 'q_loop'), ('q_loop', 'q_loop_result'), ('q_completion','q_completion_result'),
        ('q_completion_result','q_package_switch'), ('q_package', 'q_final_result'), ('q_final_result', 'q_answer')]: edge(a, b)
    edge('q_result_switch', 'q_readback_answer', 'readback')
    edge('q_result_switch', 'q_readback_answer', 'needs_inspection')
    edge('q_result_switch', 'q_remember', 'false')
    edge('q_switch', 'q_work_ack', 'execute')
    edge('q_loop_result', 'q_recovery_answer', 'interrupted')
    edge('q_loop_result', 'q_completion', 'false')
    edge('q_package_switch', 'q_package', 'ready')
    edge('q_package_switch', 'q_pending_answer', 'false')
    edge('q_switch', 'q_preview_message', 'false')
    if not complex_agent:
        edge('q_preview_message', 'q_preview')
    if figure_routing:
        edge('q_figure_merge', 'q_receive')
    else:
        edge('q_intake_switch', 'q_receive', 'forward')
    edge('q_intake_switch', 'q_intake_answer', 'false')
    for a, b in [('q_loop_start', 'q_step'), ('q_step', 'q_step_result'), ('q_step_result','q_review'),('q_review','q_review_result'),('q_review_result','q_loop_outcome'),('q_loop_outcome', 'q_save_done')]:edge(a, b, inside=True)
    for n in nodes:
        if n['data'].get('error_strategy') == 'fail-branch':
            inside = n['data'].get('isInLoop', False)
            edge(n['id'], 'q_stop_on_error' if inside else 'q_recovery_answer', 'fail-branch', inside=inside)
    loop_index = 0
    for i, n in enumerate(nodes):
        if n['data'].get('isInLoop'):
            n.update(parentId='q_loop', extent='parent')
            if figure_routing:
                n['position'] = {'x': 35 + (loop_index % 3) * 300, 'y': 65 + (loop_index // 3) * 110}
                loop_index += 1
            else:
                n['position'] = {'x': (i - 8) * 260, 'y': 90}
        else:n['position'] = {'x': (i % 6) * 310, 'y': (i // 6) * 550}
    by_id = {n['id']: n for n in nodes}
    by_id['q_loop_start'].update(type='custom-loop-start', width=44, height=48, selectable=False, draggable=False)
    by_id['q_loop'].update(width=1200, height=450)
    for n in nodes:
        if n.get('parentId') == 'q_loop': n['zIndex'] = 1002
    return assemble(nodes, edges, name, uid, agent_uid, figure_routing, complex_agent, durable_work)


def assemble(nodes, edges, name, uid, agent_uid, figure_routing, complex_agent, durable_work):
    if durable_work:
        for index, n in enumerate(nodes):
            n['position'] = {'x': (index % 6) * 310, 'y': (index // 6) * 550}
    features = {'opening_statement': '直接描述技术方案或上传交底书及已有附图。我会先分析材料，带出有依据的信息，并逐项询问必要缺项。核对摘要后回复“确认”，再发送“预览”或“规划”。已有原图时修改补充，无原图时按技术内容新建图集。图单确认、绘图及工程示意导出按当前状态逐步进行。',
        'suggested_questions': ['预览', '规划', '导出'], 'file_upload': {'enabled': True, 'allowed_file_extensions': ['.PDF', '.PNG', '.JPG', '.JPEG', '.TXT', '.MD'],
        'allowed_file_types': ['image', 'document'], 'allowed_file_upload_methods': ['local_file'], 'number_limits': 6,
        'image': {'enabled': True, 'number_limits': 6, 'transfer_methods': ['local_file']}}, 'text_to_speech': {'enabled': False, 'voice': '', 'language': ''}}
    if figure_routing:
        features['opening_statement'] += ' 一份交底可同时规划结构图、流程图等不同图型。'
    if durable_work:
        features['opening_statement'] += ' 持久任务在后台处理。发送“查看进度”可读取最近保存的图件与下一步。'
    for k in ['speech_to_text', 'sensitive_word_avoidance', 'retriever_resource', 'suggested_questions_after_answer']:features[k] = {'enabled': False}
    return {'version': '0.7.0', 'kind': 'app', 'app': {'name': name, 'mode': 'advanced-chat',
        'description': '原图修改补充、无图新建、按技术类型规划、分层标注、中国附图规则核查；工程候选，非申请就绪。',
        'icon': '📐', 'icon_type': 'emoji', 'icon_background': '#E0F2FE', 'use_icon_as_answer_icon': False},
        'dependencies': [{'type': 'package', 'value': {'plugin_unique_identifier': ident}}
                         for ident in (uid, agent_uid if complex_agent else None) if ident],
        'workflow': {'graph': {'nodes': nodes, 'edges': edges, 'viewport': {'x': 0, 'y': 0, 'zoom': .5}}, 'features': features, 'environment_variables': [],
            'conversation_variables': [{'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'patent-quality/workspace')), 'name': 'drawing_workspace',
                'selector': ['conversation', 'drawing_workspace'], 'value_type': 'string', 'value': '', 'description': '本会话已确认工作区引用'},
                {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'patent-quality/intake-revision')), 'name': 'intake_revision',
                 'selector': ['conversation', 'intake_revision'], 'value_type': 'string', 'value': '', 'description': '插件采集状态修订号，续答带回校验'}]}}


def restrict_claims_graph(app, *, mixed_materials=False):
    workflow = app['workflow']
    graph = workflow['graph']
    keep = {'q_start', 'q_durable_early', 'q_durable_early_switch', 'q_inspect_work',
            'q_work_readback', 'q_readback_answer', 'q_claims_file_switch', 'q_claims_status_answer',
            'q_intake', 'q_intake_result', 'q_intake_remember',
            'q_intake_switch', 'q_intake_answer', 'q_admit_work', 'q_admission_result',
            'q_admission_answer', 'q_recovery_answer'}
    graph['nodes'] = [node for node in graph['nodes'] if node['id'] in keep]
    graph['edges'] = [edge for edge in graph['edges'] if edge['source'] in keep and edge['target'] in keep]
    kinds = {node['id']: node['data']['type'] for node in graph['nodes']}
    for start, handle, end in (
        ('q_inspect_work', 'source', 'q_work_readback'),
        ('q_admission_result', 'source', 'q_admission_answer'),
        ('q_durable_early_switch', 'false', 'q_intake'),
    ):
        graph['edges'].append({'id': start + '-' + handle + '-' + end, 'type': 'custom',
                               'source': start, 'sourceHandle': handle, 'target': end,
                               'targetHandle': 'target', 'data': {'isInLoop': False, 'loop_id': None,
                               'sourceType': kinds[start], 'targetType': kinds[end]}})
    for node in graph['nodes']:
        if node['id'] == 'q_start':
            fields = node['data']['variables']
            node['data']['variables'] = [dict(fields[0], label='权利要求书正文')]
        if node['id'] == 'q_work_readback':
            node['data']['title'] = '核对原任务、待核图位及同版图单'
        if node['id'] == 'q_readback_answer':
            node['data']['title'] = '原任务状态及同版图单'
        if node['id'] == 'q_recovery_answer':
            node['data']['answer'] = ('请在原会话重发刚才的“查看任务”或“导出任务”命令及原任务标识。'
                                      '如果尚未指定任务，可发送“查看进度”。')
        if node['id'] in {'q_intake', 'q_admit_work'}:
            parameters = node['data']['tool_parameters']
            for key in ('patent_type', 'technical_type', 'application_stage', 'image_budget', 'plan_json'):
                parameters.pop(key, None)
    app['app']['description'] = '汇集专利材料并按来源证据规划附图。' if mixed_materials else '仅以权利要求正文规划成套附图及图说草案。'
    features = workflow['features']
    if mixed_materials:
        graph['nodes'][0]['data']['variables'][0]['label'] = '专利申请材料或技术交底正文'
        features['opening_statement'] = ('上传或粘贴权利要求书、说明书、交底书和原图。核对材料后回复“确认”，'
                                          '再发送“规划”。通过“继续”查看原任务，按回执导出同版图单。')
        features['suggested_questions'] = ['确认', '规划', '查看进度']
        return app
    features['opening_statement'] = ('提交权利要求正文，核对后回复“确认”，再发送“规划”。'
                                      '图单回执会列出规划重点、已核范围和待核事项。'
                                      '发送“继续”查看原任务状态。图单完成后，按回执导出同版文件。')
    features['suggested_questions'] = ['确认', '规划', '查看进度']
    features['file_upload'].update(allowed_file_extensions=['.PDF', '.TXT', '.MD'],
                                   allowed_file_types=['document'], number_limits=1,
                                   image={'enabled': False})
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--uid')
    parser.add_argument('--agent-uid')
    parser.add_argument('--credential-id')
    parser.add_argument('--name')
    parser.add_argument('--figure-routing', action='store_true', help='Include GPT-6 Luna and figure-specific nodes')
    parser.add_argument('--complex-agent', action='store_true')
    parser.add_argument('--direct-comparison', action='store_true')
    parser.add_argument('--durable-work', action='store_true')
    parser.add_argument('--claims-only', action='store_true')
    parser.add_argument('--mixed-materials', action='store_true')
    parser.add_argument('--output', type=Path, default=HERE / 'patent-drawing-r22.yml')
    args = parser.parse_args()
    options = {'name': args.name} if args.name else {}
    options['figure_routing'] = args.figure_routing
    options['complex_agent'] = args.complex_agent
    options['agent_uid'] = args.agent_uid
    options['agent_preparation'] = not args.direct_comparison
    options['durable_work'] = args.durable_work
    options['claims_only'] = args.claims_only
    options['mixed_materials'] = args.mixed_materials
    args.output.write_text(yaml.safe_dump(build(args.uid, args.credential_id, **options), allow_unicode=True, sort_keys=False))
    print(args.output)
