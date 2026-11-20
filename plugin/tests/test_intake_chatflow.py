import importlib.util
from pathlib import Path


def build(**options):
    path = Path(__file__).resolve().parents[2] / 'chatflows/build_quality_chatflow.py'
    spec = importlib.util.spec_from_file_location('intake_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build('test-author/test:0.3.10@' + 'a' * 64, **options)


def test_optional_first_input_and_plugin_intake_precede_receive():
    flow = build()['workflow']
    nodes = {n['id']: n['data'] for n in flow['graph']['nodes']}
    fields = {v['variable']: v for v in nodes['q_start']['variables']}
    for name in ['patent_type', 'application_stage', 'technical_type']:
        assert fields[name]['required'] is False
        assert fields[name]['default'] == '待确认'
        assert fields[name]['default'] in fields[name]['options']
    assert nodes['q_intake']['tool_parameters']['action']['value'] == 'intake'
    assert 'files' not in nodes['q_receive']['tool_parameters']
    assert 'materials' not in nodes['q_receive']['tool_parameters']
    incoming = [e for e in flow['graph']['edges'] if e['target'] == 'q_receive']
    assert len(incoming) == 1 and incoming[0]['source'] == 'q_intake_switch'
    assert incoming[0]['sourceHandle'] == 'forward'
    assert nodes['q_receive']['tool_parameters']['workspace_id']['value'] == ['q_intake_result', 'workspace_id']
    assert not any(n['data']['type'] in {'llm', 'agent', 'question-classifier'} for n in flow['graph']['nodes'])
    for name, data in nodes.items():
        if data['type'] == 'tool' and name != 'q_intake':
            assert data['tool_parameters']['expected_revision']['value'] == ['q_intake_result', 'intake_revision']


def test_claims_continue_routes_to_read_only_original_task():
    flow = build(claims_only=True)['workflow']['graph']
    nodes = {n['id']: n['data'] for n in flow['nodes']}
    sandbox = {}
    exec(nodes['q_durable_early']['code'], sandbox)
    assert sandbox['main']('workspace', '继续') == {'route': 'inspect'}
    assert sandbox['main']('workspace', '规划') == {'route': 'intake'}
    assert sandbox['main']('', '继续') == {'route': 'intake'}
    assert nodes['q_inspect_work']['tool_parameters']['action']['value'] == 'inspect_work'
    assert not nodes['q_inspect_work']['retry_config']['retry_enabled']
    recovery = nodes['q_recovery_answer']['answer']
    assert '导出任务' in recovery and '导出版本' not in recovery


def test_type_adapter_accepts_empty_without_filling_application_facts():
    path = Path(__file__).resolve().parents[2] / 'chatflows/quality_flow.py'
    spec = importlib.util.spec_from_file_location('quality_adapter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.inputs('', '', '') == {'patent_type': '', 'technical_type': '', 'application_stage': ''}
    assert module.inputs('待确认', '待确认', '待确认') == {'patent_type': '', 'technical_type': '', 'application_stage': ''}
    assert module.inputs('发明', '机电结构', '已申请') == {
        'patent_type': 'invention', 'technical_type': 'mechanical,electrical', 'application_stage': 'filed'}


def test_chatflow_preserves_the_empty_export_status_and_recoverable_revision():
    node = next(row['data'] for row in build()['workflow']['graph']['nodes'] if row['id'] == 'q_result')
    sandbox = {}
    exec(node['code'], sandbox)
    result = sandbox['main']([{
        'workspace_id': 'case', 'route': 'reply', 'done': 'yes', 'blocking_state': 'needs_expression',
        'blocked_view': 'detail', 'resume_action': 'supply_or_review_expression',
        'message': '没有已完成的待执行图件。', 'deliverable_status': 'no_completed_figures',
        'missing_ids': ['detail'], 'current_revision': 1, 'can_export_current': True,
    }])
    assert result['deliverable_status'] == 'no_completed_figures'
    assert result['missing_ids'] == ['detail']
    assert result['current_revision'] == '1' and result['done'] == 'yes'
    assert set(result) == set(node['outputs'])


def test_structural_reference_export_does_not_imply_a_new_supplier_request():
    node = next(row['data'] for row in build()['workflow']['graph']['nodes'] if row['id'] == 'q_result')
    sandbox = {}
    exec(node['code'], sandbox)
    result = sandbox['main']([{'workspace_id': 'case', 'route': 'reply',
        'supplier_state': 'provider_rejected', 'blocking_state': 'provider_rejected',
        'deliverable_status': 'structural_reference', 'message': '已导出来源关系结构示意。'}])
    assert result['supplier_state'] == 'provider_rejected'
    assert '原图像请求被供应商拒绝' in result['message']
    assert '本次请求' not in result['message']


def test_historical_export_answer_excludes_current_preflight_and_avoids_duplicate_guidance():
    flow = build()['workflow']['graph']
    node = next(row['data'] for row in flow['nodes'] if row['id'] == 'q_preview_message')
    sandbox = {}
    exec(node['code'], sandbox)
    show = sandbox['main']
    assert show('已取回完整图集第 1 版。', '当前图集第 2 版待补', 'historical_revision') == {
        'message': '已取回完整图集第 1 版。',
    }
    assert show('图单待确认。', '图单待确认。', '') == {'message': '图单待确认。'}


def test_partial_delivery_answer_uses_export_status_and_preserves_review_todos():
    flow = build()['workflow']['graph']
    nodes = {node['id']: node['data'] for node in flow['nodes']}
    result_scope, answer_scope = {}, {}
    exec(nodes['q_result']['code'], result_scope)
    exec(nodes['q_preview_message']['code'], answer_scope)
    receipt = result_scope['main']([{
        'workspace_id': 'case', 'route': 'reply', 'done': 'yes',
        'deliverable_status': 'incomplete_draft', 'supplier_state': 'response_saved',
        'missing_ids': ['sequence'], 'unknown_feature_ids': ['main/source-fidelity'],
        'message': '已导出已完成图件。未生成图件：sequence。本次导出未新增模型调用。',
    }])
    shown = answer_scope['main'](receipt['message'], '前置检查通过，本次检查未新增模型调用。',
                                 receipt['deliverable_status'])
    assert shown == {'message': receipt['message']}
    assert 'sequence' in shown['message'] and 'main/source-fidelity' in shown['message']
    assert receipt['route'] == 'reply' and receipt['supplier_state'] == 'response_saved'


def test_complete_download_answer_keeps_the_authoritative_export_message():
    node = next(row['data'] for row in build()['workflow']['graph']['nodes'] if row['id'] == 'q_preview_message')
    scope = {}
    exec(node['code'], scope)
    message = '已导出最近完整版本1，共 2 张图。技术状态 UNKNOWN。'
    assert scope['main'](message, '没有待执行修订。', 'complete_draft') == {'message': message}


def test_package_branch_requires_a_fresh_ready_to_package_receipt():
    graph = build()['workflow']['graph']
    edges = graph['edges']
    incoming = [edge for edge in edges if edge['target'] == 'q_package']
    assert [(edge['source'], edge['sourceHandle']) for edge in incoming] == [
        ('q_package_switch', 'ready'),
    ]
    status = next(node['data'] for node in graph['nodes'] if node['id'] == 'q_completion')
    assert status['tool_parameters']['action']['value'] == 'review_status'
    assert any(e['source'] == 'q_loop_result' and e['target'] == 'q_completion' for e in edges)
    assert {e['source'] for e in edges if e['sourceHandle'] == 'fail-branch' and e['target'] == 'q_stop_on_error'} == {
        'q_step', 'q_review', 'q_step_result', 'q_review_result', 'q_loop_outcome',
    }
    assert any(e['source'] == 'q_loop_result' and e['sourceHandle'] == 'interrupted'
               and e['target'] == 'q_recovery_answer' for e in edges)
    assert any(e['source'] == 'q_package_switch' and e['sourceHandle'] == 'false'
               and e['target'] == 'q_pending_answer' for e in edges)


def test_missing_quality_receipt_is_a_non_executing_recovery_answer():
    node = next(row['data'] for row in build()['workflow']['graph']['nodes'] if row['id'] == 'q_result')
    scope = {}
    exec(node['code'], scope)
    result = scope['main']([])
    assert result['route'] == 'reply'
    assert result['execution_state'] == 'paused'
    assert result['resume_action'] == 'inspect_original_result'
    assert '查看进度' in result['message']
    assert set(result) == set(node['outputs'])


def test_history_answer_skips_execution_and_complexity_nodes():
    graph = build(figure_routing=True, complex_agent=True)['workflow']['graph']
    edges = graph['edges']
    assert any(e['source'] == 'q_result_switch' and e['sourceHandle'] == 'readback'
               and e['target'] == 'q_readback_answer' for e in edges)
    assert not any(e['source'] == 'q_result' and e['target'] == 'q_preflight' for e in edges)


def test_unhandled_outer_tool_error_reaches_a_reply_without_resending():
    graph = build(figure_routing=True, complex_agent=True)['workflow']['graph']
    edges = graph['edges']
    for key in ('q_intake', 'q_receive', 'q_package', 'q_complexity'):
        node = next(n['data'] for n in graph['nodes'] if n['id'] == key)
        assert node['error_strategy'] == 'fail-branch'
        assert node['retry_config']['retry_enabled'] is False
        assert any(e['source'] == key and e['sourceHandle'] == 'fail-branch'
                   and e['target'] == 'q_recovery_answer' for e in edges)


def test_linked_edit_preview_preserves_scope_and_blocks_the_loop():
    node = next(row['data'] for row in build()['workflow']['graph']['nodes'] if row['id'] == 'q_result')
    scope = {}
    exec(node['code'], scope)
    message = '图1（main）允许改动像素范围 [750, 100, 800, 140]。附图说明从「电源10」更新为「电源110」。'
    receipt = scope['main']([{'workspace_id': 'case', 'route': 'execute', 'message': message,
                             'linked_edit': {'ready': True}, 'confirmed': False}])
    assert receipt['route'] == 'reply' and receipt['done'] == 'yes'
    assert receipt['blocking_state'] == 'awaiting_linked_confirmation'
    assert receipt['resume_action'] == 'review_linked_edit_scope' and receipt['message'] == message
    confirmed = scope['main']([{'workspace_id': 'case', 'route': 'execute', 'confirmed': True}])
    assert confirmed['route'] == 'execute'


def test_chatflow_explains_how_to_correct_an_out_of_bounds_parent_crop():
    flow = build()['workflow']['graph']
    node = next(row['data'] for row in flow['nodes'] if row['id'] == 'q_preflight_result')
    sandbox = {}
    exec(node['code'], sandbox)
    output = sandbox['main']([{
        'workspace_id': 'case', 'route': 'execute', 'blocking_state': 'invalid_parent_crop',
        'blocked_view': 'detail', 'supplier_state': 'not_called', 'message': '区域超出实际母图。',
    }])
    assert output['route'] == 'reply' and output['done'] == 'yes'
    assert output['resume_action'] == 'confirm_parent_crop'
    assert '导入计划' in output['message'] and '当前母图' in output['message']
    assert output['supplier_state'] == 'not_called'


def test_incomplete_planning_prompts_for_revision_instead_of_execution():
    node = next(row['data'] for row in build()['workflow']['graph']['nodes'] if row['id'] == 'q_result')
    sandbox = {}
    exec(node['code'], sandbox)
    result = sandbox['main']([{
        'workspace_id': 'case', 'route': 'execute', 'blocking_state': 'planning_coverage_incomplete',
        'message': '图单缺导电柱的实际引线标号。',
    }])
    assert result['route'] == 'reply' and result['done'] == 'yes'
    assert result['resume_action'] == 'refine_drawing_plan'
    assert '规划：' in result['message'] and '导电柱' in result['message']
    imported = sandbox['main']([{
        'workspace_id': 'case', 'route': 'execute', 'blocking_state': 'planning_coverage_incomplete',
        'planning_input': 'imported', 'message': '图 heat 将膜面取向和邻片接触画成串联。',
    }])
    assert imported['route'] == 'reply' and imported['done'] == 'yes'
    assert '导入计划' in imported['message'] and '邻片接触' in imported['message']


def test_durable_coverage_report_preserves_resume_scope_and_explains_missing_view():
    graph = build(figure_routing=True, complex_agent=True, durable_work=True)['workflow']['graph']
    nodes = {row['id']: row['data'] for row in graph['nodes']}
    receipt = {
        'workspace_id': 'case', 'route': 'reply', 'job_status': 'waiting_input',
        'task_id': 'work_' + 'a' * 64, 'scope': 'bound', 'message': '候选图单待核。',
        'blocking_state': 'planning_coverage_incomplete', 'planning_command': '修改',
        'domain_findings': [{'view_id': 'cabinet', 'page_id': 'source-page-1',
                             'message': '请提供辅助供电轨的上游和下游端点。'}],
    }
    for node_id in ('q_admission_result', 'q_work_readback'):
        sandbox = {}
        exec(nodes[node_id]['code'], sandbox)
        result = sandbox['main']([receipt])
        assert result['route'] == 'reply'
        assert '辅助供电轨' in result['message'] and 'source-page-1' in result['message']
        assert 'cabinet' in result['message'] and '修改：' in result['message']
        if node_id == 'q_admission_result':
            assert result['task_id'] == receipt['task_id'] and result['scope'] == 'bound'
            assert result['job_status'] == 'waiting_input'
            delayed = sandbox['main']([])
            assert delayed['route'] == 'reply' and '查看进度' in delayed['message']


def test_filing_status_and_next_actions_reach_chatflow_answer():
    path = Path(__file__).resolve().parents[2] / 'chatflows/quality_flow.py'
    spec = importlib.util.spec_from_file_location('filing_quality_adapter', path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

    data = adapter.result([{'workspace_id': 'w', 'route': 'reply', 'filing_status': 'UNKNOWN',
                    'filing_next_actions': ['F2 缩小样张待核', 'F4 来源待核']}])
    assert data['filing_status'] == 'UNKNOWN'
    assert 'F2 缩小样张待核' in data['message']
    nodes = {row['id']: row['data'] for row in build(figure_routing=True)['workflow']['graph']['nodes']}
    assert 'filing_status' in nodes['q_final_result']['outputs']


def test_precise_edit_rejection_keeps_completed_views_and_same_task_recovery():
    graph = build(figure_routing=True, complex_agent=True, durable_work=True)['workflow']['graph']
    nodes = {row['id']: row['data'] for row in graph['nodes']}
    task_id = 'work_' + 'c' * 64
    receipt = {
        'workspace_id': 'w', 'task_id': task_id, 'scope': 'confirmed',
        'route': 'reply', 'job_status': 'partial', 'blocked_view': 'section',
        'blocking_state': 'precise_edit_rejected', 'resume_action': 'review_saved_result',
        'completed_view_ids': ['overview'], 'missing_ids': None, 'message': '局部补线线宽超限。',
    }
    for key in ('q_enqueued_result', 'q_work_readback'):
        sandbox = {}
        exec(nodes[key]['code'], sandbox)
        output = sandbox['main']([receipt])
        assert output['route'] == 'reply'
        assert output.get('missing_ids', ['section']) == ['section']
        assert 'overview' in output['message'] and 'section' in output['message']
        assert '导出任务 ' + task_id in output['message']
        assert output['resume_action'] == 'review_saved_result'


def test_claims_readback_keeps_original_export_command_when_file_is_missing():
    graph=build(claims_only=True)['workflow']['graph']
    node=next(r['data'] for r in graph['nodes'] if r['id']=='q_work_readback')
    namespace={}
    exec(node['code'],namespace)
    task='work_' + 'a'*64
    msg='本版图单已保存。'
    args={'items':[{'workspace_id':'ws','task_id':task,'file_hashes':{'claims-plan.json':'b'*64},
                   'message':msg,'deliverable_status':'engineering_reference'}],
          'query':'导出任务 ' + task}
    read=namespace['main']
    out=read(**args,files=[])
    assert out['resume_action']=='download_same_revision'
    assert args['query'] in out['message']
    assert read(**args,files=[{'filename':'claims-plan.json'}])['message']==msg


def test_claims_readback_marks_partial_guard_plan_and_keeps_original_failure_distinct():
    graph = build(claims_only=True)['workflow']['graph']
    code = next(n['data']['code'] for n in graph['nodes'] if n['id'] == 'q_work_readback')
    namespace = {}
    exec(code, namespace)
    read = namespace['main']
    task = 'work_' + 'a'*64
    command = '导出任务 ' + task
    partial = {'workspace_id': 'ws', 'task_id': task, 'claims_plan_status': 'partial',
               'claims_guard_pending': 2, 'message': '权3／fig1 守卫引证缺证。',
               'file_hashes': {'claims-plan.json': 'b'*64}}
    out = read([partial], [{'filename': 'claims-plan.json'}], command)
    assert out['readback'] == 'yes' and '同版部分图单' in out['message']
    assert partial['message'] in out['message']
    out = read([partial], [], command)
    assert out['readback'] == 'no' and out['resume_action'] == 'download_same_revision'
    assert command in out['message']
    failed = {'workspace_id': 'ws', 'task_id': task, 'job_status': 'failed',
              'blocking_state': 'planner_invalid_output', 'message': '整份响应结构错误。'}
    out = read([failed], [], command)
    assert out['readback'] == 'no' and out['message'] == failed['message']


def test_claims_readback_shows_semantic_choices_for_the_original_task():
    graph = build(claims_only=True)['workflow']['graph']
    code = next(n['data']['code'] for n in graph['nodes'] if n['id'] == 'q_work_readback')
    namespace = {}
    exec(code, namespace)
    task = 'work_' + 'b'*64
    row = {'workspace_id': 'ws', 'task_id': task, 'file_hashes': {'claims-plan.json': 'a'*64},
           'message': '图单已保存。', 'claims_semantic_summary': '建议重点表达 并行路径与汇合。'}
    result = namespace['main']([row], [{'filename': 'claims-plan.json'}], '导出任务 '+task)
    assert result['readback'] == 'yes'
    assert result['message'].splitlines() == [row['message'], row['claims_semantic_summary']]


def test_claims_readback_shows_vote_counts_for_the_original_task():
    graph = build(claims_only=True)['workflow']['graph']
    code = next(n['data']['code'] for n in graph['nodes'] if n['id'] == 'q_work_readback')
    namespace = {}
    exec(code, namespace)
    claims_readback = namespace['main']

    task_id = 'work_' + '7' * 64
    row = {'workspace_id': 'ws', 'task_id': task_id, 'job_status': 'queued', 'message': '等待图单规划。',
           'claims_vote_review': {'disagreements': 2, 'rejudged': 2, 'uncertain': 1, 'low_confidence': 1}}
    for query in ('继续', '查看任务 ' + task_id):
        reply = claims_readback([row], [], query)
        assert 'Luna 逐题判断有 2 项分歧，复判 2 项，保留待核 1 项。' in reply['message']
        assert '同票低置信 1 项' in reply['message']


def test_claims_readback_retries_explicit_id_without_a_receipt_or_expected_file():
    graph = build(claims_only=True)['workflow']['graph']
    node = next(row['data'] for row in graph['nodes'] if row['id']=='q_work_readback')
    namespace = {}
    exec(node['code'], namespace)
    read = namespace['main']
    task = 'work_' + 'a'*64
    for action in ('查看', '导出'):
        query = action + '任务 ' + task
        assert query in read(items=[], files=[], query=query)['message']
    reply = {'workspace_id': 'ws', 'task_id': task, 'file_hashes': {'claims-plan.json': 'a'*64}}
    query = '导出任务 ' + task
    assert read(items=[reply], files=[{'filename':'wrong.txt'}], query=query)['resume_action']=='download_same_revision'
    denied = {'workspace_id': '', 'job_status': 'not_started', 'message': '当前会话尚无此任务。'}
    assert read(items=[denied], files=[], query=query)['message'] == denied['message']


def test_claims_readback_ignores_a_wrong_task_receipt_even_with_a_json_file():
    graph = build(claims_only=True)['workflow']['graph']
    node = next(row['data'] for row in graph['nodes'] if row['id']=='q_work_readback')
    namespace = {}
    exec(node['code'], namespace)
    task = 'work_' + 'a'*64
    wrong = {'workspace_id':'ws','task_id':'work_' + 'b'*64,'message':'其他任务的附件详情'}
    result = namespace['main'](items=[wrong], files=[{'filename':'claims-plan.json'}], query='导出任务 '+task)
    assert result['blocking_state']=='missing_tool_receipt'
    assert '导出任务 '+task in result['message']
    assert wrong['message'] not in result['message']
    assert result['readback']=='no'
    assert any(edge['source']=='q_work_readback' and edge['target']=='q_claims_file_switch'
               for edge in graph['edges'])
    right={**wrong, 'task_id':task, 'file_hashes':{'claims-plan.json':'a'*64}}
    assert namespace['main'](items=[right], files=[{'filename':'claims-plan.json'}],
                             query='导出任务 '+task)['readback']=='yes'


def test_claims_readback_preserves_source_provenance_and_original_task():
    graph=build(claims_only=True)['workflow']['graph']
    node=next(row['data'] for row in graph['nodes'] if row['id']=='q_work_readback')
    namespace={};exec(node['code'],namespace)
    task='work_'+'c'*64
    message='规则已按原文补齐 2 条显式关系（权2／fig1、权3／fig1），逐条依据见图单 source_derivations。'
    item={'workspace_id':'ws','task_id':task,'message':message,'file_hashes':{'claims-plan.json':'d'*64}}
    out=namespace['main'](items=[item],files=[{'filename':'claims-plan.json'}],query='导出任务 '+task)
    assert out['message']==message and out['readback']=='yes'
    out=namespace['main'](items=[item],files=[],query='导出任务 '+task)
    assert message in out['message'] and '导出任务 '+task in out['message']


def test_mixed_chatflow_readback_exposes_sparse_figures_with_a_limit_issue():
    graph = build(mixed_materials=True)['workflow']['graph']
    code = next(n['data']['code'] for n in graph['nodes'] if n['id'] == 'q_work_readback')
    namespace = {}
    exec(code, namespace)
    task = 'work_' + 'a'*64
    receipt = {'workspace_id':'ws', 'task_id':task, 'job_status':'completed',
               'claims_plan_status':'partial', 'message':'图单已保存。',
               'completed_view_ids':['fig1','fig2','fig4','fig5'],
               'claims_view_limit_issues':[{'view_id':'fig3','logical_count':97,'limit':96}],
               'file_hashes':{'claims-plan.json':'b'*64}}
    result = namespace['main']([receipt],[{'filename':'claims-plan.json'}],'导出任务 '+task)
    assert result['readback']=='yes'
    assert '同版部分图单' in result['message']
    assert '图 3' in result['message'] and '97/96' in result['message']
    assert '图 1、2、4、5' in result['message']
