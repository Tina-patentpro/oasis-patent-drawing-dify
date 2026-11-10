"""v0.3.0: files/types -> persistent workspace -> explicit per-figure Loop."""
from pathlib import Path
import argparse
import uuid
import yaml

HERE = Path(__file__).resolve().parent
PROVIDER = 'tina-patentpro/oasisintwrite_patent_drawing/oasisintwrite_patent_drawing'


def build(uid=None, credential_id=None):
    nodes = [];edges = [];source = (HERE / 'quality_flow.py').read_text()
    def node(n, typ, title, **data):
        nodes.append({'id': n, 'type': 'custom', 'position': {'x': len(nodes) * 300, 'y': 0}, 'width': 244, 'height': 100,
            'sourcePosition': 'right', 'targetPosition': 'left', 'data': {'type': typ, 'title': title, 'desc': '', 'selected': False, **data}})
    def edge(a, b, handle='source', inside=False):
        kinds = {x['id']: x['data']['type'] for x in nodes}
        edges.append({'id': a + '-' + handle + '-' + b, 'source': a, 'target': b, 'sourceHandle': handle, 'targetHandle': 'target', 'type': 'custom',
            'data': {'sourceType': kinds[a], 'targetType': kinds[b], 'isInLoop': inside, 'loop_id': 'q_loop' if inside else None}})
    def code(n, title, expression, variables, outputs, inside=False):
        node(n, 'code', title, code_language='python3', code=source + '\n\ndef main(' + ','.join(variables) + '):\n return ' + expression + '\n',
            variables=[{'variable': k, 'value_selector': v} for k, v in variables.items()],
            outputs={k: {'type': t, 'children': None} for k, t in outputs.items()}, **({'isInLoop': True, 'loop_id': 'q_loop'} if inside else {}))
    def tool(n, title, action, parameters, inside=False):
        params = {k: {'type': 'variable', 'value': v} for k, v in parameters.items()}
        params['action'] = {'type': 'mixed', 'value': action}
        node(n, 'tool', title, provider_type='builtin', provider_id=PROVIDER, provider_name=PROVIDER,
            tool_name='patent_drawing_workspace', tool_label='专利附图工作区', tool_configurations={}, tool_parameters=params,
            tool_node_version='2', plugin_unique_identifier=uid, credential_id=credential_id, is_team_authorization=True,
            retry_config={'retry_enabled': False, 'max_retries': 0, 'retry_interval': 1000},
            **({'isInLoop': True, 'loop_id': 'q_loop'} if inside else {}))
    fields = [
        {'variable': 'materials', 'label': '交底正文（已有PDF可留空）', 'type': 'paragraph', 'required': False, 'max_length': 64000, 'default': ''},
        {'variable': 'plan_json', 'label': '审阅图单JSON（高级验证，可留空）', 'type': 'paragraph', 'required': False, 'max_length': 400000, 'default': ''},
        {'variable': 'patent_type', 'label': '专利申请类型', 'type': 'select', 'required': True, 'options': ['发明', '实用新型', '外观设计'], 'default': '发明'},
        {'variable': 'technical_type', 'label': '技术类型', 'type': 'select', 'required': True, 'options': ['自动识别', '机械结构', '电气电路', '机电结构', '软件/计算机方法', '通信', '制造工艺', '化学材料', '生物医学', '半导体'], 'default': '自动识别'},
        {'variable': 'application_stage', 'label': '申请阶段', 'type': 'select', 'required': True, 'options': ['申请前', '已申请'], 'default': '申请前'},
        {'variable': 'image_budget', 'label': '图集附加调用上限（0按操作员授权）', 'type': 'number', 'required': False, 'default': 0},
    ]
    node('q_start', 'start', '交底与用途', variables=fields)
    code('q_types', '绑定申请和技术类型', 'inputs(patent_type,technical_type,application_stage)',
        {x: ['q_start', x] for x in ['patent_type', 'technical_type', 'application_stage']},
        {x: 'string' for x in ['patent_type', 'technical_type', 'application_stage']})
    params = {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['conversation', 'drawing_workspace'], 'query': ['sys', 'query'],
        'files': ['sys', 'files'], 'materials': ['q_start', 'materials'], 'plan_json': ['q_start', 'plan_json'],
        'image_budget': ['q_start', 'image_budget'], **{x: ['q_types', x] for x in ['patent_type', 'technical_type', 'application_stage']}}
    tool('q_receive', '原图修改补充／无图新建规划', 'receive', params)
    outputs = {x: 'string' for x in ['workspace_id', 'route', 'done', 'message','technical_status','blocking_state','blocked_view','resume_action','deferred_blocking_state','deferred_blocked_view',
                                    'expression_mode','supplier_state','semantic_state','pending_action']}
    outputs.update({x: 'array[string]' for x in ['failed_feature_ids', 'unknown_feature_ids', 'unreviewed_view_ids', 'missing_features']})
    code('q_result', '读取工作区与任务状态', 'result(items)', {'items': ['q_receive', 'json']}, outputs)
    node('q_remember', 'assigner', '保存本会话工作区引用', version='2', items=[{'variable_selector': ['conversation', 'drawing_workspace'], 'input_type': 'variable', 'operation': 'over-write', 'value': ['q_result', 'workspace_id']}])
    tool('q_preflight','来源与引导前置检查','preflight',{'context_id':['sys','conversation_id'],'workspace_id':['q_result','workspace_id']})
    code('q_preflight_result','读取前置阻断','result(items)',{'items':['q_preflight','json']},outputs)
    node('q_switch', 'if-else', '预览或执行', cases=[{'case_id': 'execute', 'logical_operator': 'and', 'conditions': [{'id':'user-requested-execution','variable_selector':['q_result','route'],'varType':'string','comparison_operator':'is','value':'execute'}, {'id': 'run-plan', 'variable_selector': ['q_preflight_result', 'route'], 'varType': 'string', 'comparison_operator': 'is', 'value': 'execute'}]}])
    node('q_preview', 'answer', '来源图文与图单', answer='{{#q_receive.files#}}\n\n{{#q_result.message#}}\n{{#q_preflight_result.message#}}')
    node('q_loop', 'loop', '按图单逐图执行', start_node_id='q_loop_start', loop_count=24,
        break_conditions=[{'id': 'completed-set', 'variable_selector': ['q_loop', 'done'], 'varType': 'string', 'comparison_operator': 'is', 'value': 'yes'}], logical_operator='and',
        loop_variables=[{'label': 'workspace_id', 'var_type': 'string', 'value_type': 'variable', 'value': ['q_result', 'workspace_id']},
                        {'label': 'done', 'var_type': 'string', 'value_type': 'constant', 'value': 'no'}], width=1200, height=450)
    node('q_loop_start', 'loop-start', '开始当前图', isInLoop=True, loop_id='q_loop')
    tool('q_step', '执行并持久保存当前图', 'step', {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_loop', 'workspace_id']}, True)
    tool('q_review','核对本图实际技术状态','review_status',{'context_id':['sys','conversation_id'],'workspace_id':['q_loop','workspace_id']},True)
    code('q_review_result','读取技术阻断','result(items)',{'items':['q_review','json']},outputs,True)
    code('q_step_result', '读取当前图完成状态', 'result(items)', {'items': ['q_step', 'json']}, outputs, True)
    code('q_loop_outcome', '合并执行和审阅停止信号', 'loop_outcome(step_done, review_done)',
         {'step_done': ['q_step_result', 'done'], 'review_done': ['q_review_result', 'done']}, {'done': 'string'}, True)
    node('q_save_done', 'assigner', '推进显式循环状态', version='2', isInLoop=True, loop_id='q_loop', items=[{'variable_selector': ['q_loop', 'done'], 'input_type': 'variable', 'operation': 'over-write', 'value': ['q_loop_outcome', 'done']}])
    tool('q_package', '核查成套图并生成PDF/ZIP', 'package', {'context_id': ['sys', 'conversation_id'], 'workspace_id': ['q_result', 'workspace_id']})
    code('q_final_result', '读取核查与交付状态', 'result(items)', {'items': ['q_package', 'json']}, outputs)
    node('q_answer', 'answer', '成套附图草稿与核查', answer='{{#q_package.files#}}\n\n{{#q_final_result.message#}}')
    for a, b in [('q_start', 'q_types'), ('q_types', 'q_receive'), ('q_receive', 'q_result'), ('q_result', 'q_remember'), ('q_remember','q_preflight'),('q_preflight','q_preflight_result'),('q_preflight_result', 'q_switch'),
        ('q_loop', 'q_package'), ('q_package', 'q_final_result'), ('q_final_result', 'q_answer')]: edge(a, b)
    edge('q_switch', 'q_loop', 'execute');edge('q_switch', 'q_preview', 'false')
    for a, b in [('q_loop_start', 'q_step'), ('q_step', 'q_step_result'), ('q_step_result','q_review'),('q_review','q_review_result'),('q_review_result','q_loop_outcome'),('q_loop_outcome', 'q_save_done')]:edge(a, b, inside=True)
    for i, n in enumerate(nodes):
        if n['data'].get('isInLoop'):
            n.update(parentId='q_loop', extent='parent');n['position'] = {'x': (i - 8) * 260, 'y': 90}
        else:n['position'] = {'x': (i % 6) * 310, 'y': (i // 6) * 550}
    by_id = {n['id']: n for n in nodes}
    by_id['q_loop_start'].update(type='custom-loop-start', width=44, height=48, selectable=False, draggable=False)
    by_id['q_loop'].update(width=1200, height=450)
    for n in nodes:
        if n.get('parentId') == 'q_loop': n['zIndex'] = 1002
    features = {'opening_statement': '上传交底PDF/图片，或填写正文。先发送“预览”核对来源，再发送“规划”“确认图单”“执行单图”。已有原图时修改补充，无原图时按技术类型新建。空间图保存后先审阅再继续局部图；关联改号需实际底图锚点齐备，整套完成才提升版本。可发送“改名”“修改：标号10改为110”“查看已完成”“导出已完成”“导出”。已登记受限工程示意时，可发送“导出工程示意”。拒绝或未知结果保留，不自动重试。',
        'suggested_questions': ['预览', '规划', '导出'], 'file_upload': {'enabled': True, 'allowed_file_extensions': ['.PDF', '.PNG', '.JPG', '.JPEG', '.TXT', '.MD'],
        'allowed_file_types': ['image', 'document'], 'allowed_file_upload_methods': ['local_file'], 'number_limits': 6,
        'image': {'enabled': True, 'number_limits': 6, 'transfer_methods': ['local_file']}}, 'text_to_speech': {'enabled': False, 'voice': '', 'language': ''}}
    for k in ['speech_to_text', 'sensitive_word_avoidance', 'retriever_resource', 'suggested_questions_after_answer']:features[k] = {'enabled': False}
    return {'version': '0.7.0', 'kind': 'app', 'app': {'name': '专利附图集-GPT-0.3.9-受控验证', 'mode': 'advanced-chat',
        'description': '原图修改补充、无图新建、按技术类型规划、分层标注、中国附图规则核查；工程候选，非申请就绪。',
        'icon': '📐', 'icon_type': 'emoji', 'icon_background': '#E0F2FE', 'use_icon_as_answer_icon': False},
        'dependencies': [{'type': 'package', 'value': {'plugin_unique_identifier': uid}}] if uid else [],
        'workflow': {'graph': {'nodes': nodes, 'edges': edges, 'viewport': {'x': 0, 'y': 0, 'zoom': .5}}, 'features': features, 'environment_variables': [],
            'conversation_variables': [{'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'patent-quality/workspace')), 'name': 'drawing_workspace',
                'selector': ['conversation', 'drawing_workspace'], 'value_type': 'string', 'value': '', 'description': '本会话持久工作区引用；原件/历史保留，重置仅清引用'}]}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--uid');parser.add_argument('--credential-id');parser.add_argument('--output', type=Path, default=HERE / 'patent_drawing_quality_chatflow-0.3.9-candidate.yml')
    args = parser.parse_args();args.output.write_text(yaml.safe_dump(build(args.uid, args.credential_id), allow_unicode=True, sort_keys=False));print(args.output)
