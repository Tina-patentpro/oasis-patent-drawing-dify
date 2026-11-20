from patent_drawing.material_evidence import (apply_supplements, initial_evidence, next_candidates,
                                             planner_context, source_rows)
from patent_drawing.material_sections import catalog_material
from tests.test_material_sections import page


def materials():
    pages = [page('权利要求书\n1. 一种装置，控制器向接收器发送信号。\n'
                  '说明书\n控制器对应标号10。\n'
                  '技术交底书\n接收器向控制器发送同一信号。\n接收器对应标号20。\n'
                  '其他材料\n图1为结构示意图。', 1)]
    return pages, source_rows(pages, catalog_material({'pages': pages}))


def test_only_supplements_enter_sol_and_priority_wins_conflicts():
    pages, rows = materials()
    evidence = initial_evidence(rows['claims'], {'marks': 'missing', 'views': 'missing'})
    higher = next_candidates(rows['specification'], evidence)
    assert len(higher) == 1 and higher[0]['gaps'] == ['marks']
    evidence = apply_supplements(evidence, higher, {higher[0]['id']: 'supplement_marks'})
    lower = next_candidates(rows['disclosure'], evidence)
    assert not any('标号20' in c['quote'] and 'marks' in c['gaps'] for c in lower)
    assert any('接收器向控制器' in c['quote'] for c in lower)
    evidence = apply_supplements(evidence, lower, {c['id']: 'conflict' if '接收器向' in c['quote'] else 'irrelevant'
                                                  for c in lower})
    last, = next_candidates(rows['other'], evidence)
    evidence = apply_supplements(evidence, [last], {last['id']: 'supplement_views'})
    context = planner_context(pages, evidence, [])
    assert [item['type'] for item in context['citations']] == ['claims','specification','other']
    assert '接收器向控制器发送同一信号' not in str(context)
    assert evidence['rejected'][0]['decision'] == 'conflict'
    assert evidence['rejected'][0]['compared'] == [rows['claims'][0]['id']]


def test_unknown_decisions_do_not_supply_closed_or_missing_facts():
    _, rows = materials()
    evidence = initial_evidence(rows['claims'], {'marks': 'covered', 'views': 'missing'})
    assert not next_candidates(rows['specification'], evidence)
    target, = next_candidates(rows['other'], evidence)
    evidence = apply_supplements(evidence, [target], {target['id']: 'uncertain'})
    assert len(evidence['accepted']) == 1
    assert evidence['gaps'] == ['views']
    assert evidence['rejected'][0]['decision'] == 'uncertain'


def test_mixed_entry_preserves_pure_claims_body(tmp_path):
    from patent_drawing.intake import Intake
    from patent_drawing.workspace import Workspace
    from tests.test_claims_input import BODY
    from tests.test_durable_work import queue_setup

    store, _, actor = queue_setup(tmp_path)
    workspace = Workspace(store, actor.owner)
    form = {'source_mode':'mixed_materials'}
    intake = Intake(workspace)
    preview = intake.apply({**form, 'query':'预览'}, [{'name':'claim.txt', 'blob':BODY.encode()}])
    confirmed = intake.apply({**form, 'query':'确认', 'expected_revision':preview['intake_revision']})
    assert workspace.read(confirmed['workspace_id'])['bundle']['claims_body'] == BODY


def test_material_planning_uses_shared_chatflow_entry():
    from chatflows.build_quality_chatflow import build

    app = build(mixed_materials=True)
    nodes = {node['id']:node['data'] for node in app['workflow']['graph']['nodes']}
    assert nodes['q_intake']['tool_parameters']['source_mode']['value'] == 'mixed_materials'
    assert 'q_admit_work' in nodes and 'q_inspect_work' in nodes
    assert not any(x['type'] in ('llm','agent') for x in nodes.values())
    assert app['workflow']['features']['file_upload']['number_limits'] == 6


def test_mixed_worker_keeps_source_and_resumes_saved_classification(tmp_path):
    import json

    import httpx
    import pytest

    from patent_drawing.figure_routing import DecisionClient
    from patent_drawing.intake import Intake
    from patent_drawing.text_client import OpenAITextClient
    from patent_drawing.work_worker import WorkWorker
    from patent_drawing.workspace import Workspace
    from tests.test_durable_work import WorkerStopped, queue_setup

    store, queue, actor = queue_setup(tmp_path)
    body = ('权利要求书\n1. 一种设备，控制器向接收器发送信号。\n'
            '说明书\n控制器对应标号10。\n技术交底书\n接收器向控制器发送同一信号。\n其他材料\n图1为系统示意图。')
    intake = Intake(Workspace(store, actor.owner))
    form = {'source_mode':'mixed_materials'}
    preview = intake.apply({**form, 'query':'预览'}, [{'name':'input.txt','blob':body.encode()}])
    state = intake.apply({**form,'query':'确认','expected_revision':preview['intake_revision']})
    wid = state['workspace_id']
    task = queue.enqueue(actor, wid, int(state['intake_revision']), 'plan', query='规划', allow_models=True)
    seen = []

    def luna(request):
        payload = json.loads(request.content)
        ctx = json.loads(payload['input'])
        phase = ctx['phase']
        seen.append(phase)
        answers = []
        for q in payload['questions']:
            values = {x['value'] for x in q['choices']}
            value = ('missing' if 'missing' in values and q['name'] in ('marks','views')
                     else 'covered' if 'covered' in values else
                     next((v for v in values if v.startswith('supplement_')), None)
                     or ('conflict' if 'conflict' in values else 'uncertain'))
            if phase == 'material_catalog':
                value = next((s['hint'] for s in ctx['sections'] if s['id']==q['name']), 'uncertain')
            answers.append({'name':q['name'],'type':'choice','choice':value})
        return httpx.Response(200, json={'model':'gpt-6-luna','answers':answers},headers={'x-request-id':'req_test_luna'})

    def sol(request):
        seen.append('sol')
        payload = json.loads(request.content)
        ctx = json.loads(payload['input'][1]['content'])
        assert '接收器向控制器发送同一信号' not in json.dumps(ctx,ensure_ascii=False)
        assert payload['max_output_tokens'] == 20000
        cid = ctx['citations'][0]['id']
        plan = {'views':[{'id':'fig1','type':'flow','title':'收发流程','purpose':'表达收发',
                          'evidence_ids':[cid],'part_ids':[],'depends_on':[], 'disposition':'new','original_ids':[]}],
                'parts':[], 'relations':[], 'unresolved':[]}
        return httpx.Response(200, json={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(plan)}]}]},headers={'x-request-id':'req_test_sol'})

    class InterruptOnce(DecisionClient):
        def classify_ballot(self, payload, evidence_sink):
            def capture(kind, data):
                evidence_sink(kind,data)
                if len(seen)==1 and kind=='receipt':
                    raise WorkerStopped()
            return super().classify_ballot(payload,capture)

    def workspace(owner):
        return Workspace(store, owner, allow_models=True,
                         decision_client=InterruptOnce('test',transport=httpx.MockTransport(luna)),
                         text_client=OpenAITextClient('test',transport=httpx.MockTransport(sol)))

    with pytest.raises(WorkerStopped):
        WorkWorker(queue, workspace).run_one()
    for _ in range(10):
        if not WorkWorker(queue, workspace).run_one():
            break
    result=queue.inspect(actor, wid, task_id=task['task_id'])
    assert result['job_status']=='completed'
    assert seen.count('material_catalog')==seen.count('sol')==1
    files=queue.files(actor, wid, task['task_id'])
    plan=json.loads(files['material-plan.json'])
    assert len(plan['views']) == 1
    assert plan['schema_version']=='material-drawing-plan-1'
    assert plan['audit_counts']['conflicts']==1
    assert files==queue.files(actor,wid,task['task_id'])


def test_mixed_plan_keeps_verified_view_when_a_declared_part_is_pending():
    from patent_drawing.material_work import _validate_plan

    citations = [{'id':'e1', 'quote':'图1 支承组件11与托板12连接。'}]
    parts = [{'id':'p1','name':'支承组件（共四个）','mark':'11','evidence_id':'e1'},
             {'id':'p2','name':'螺钉（形式待确认）','mark':'','evidence_id':'e1'}]
    base = {'type':'structure','title':'已证支承','purpose':'表达连接','disposition':'retain',
            'original_ids':['original1'],'evidence_ids':['e1'],'depends_on':[],'part_ids':['p1','p2']}
    raw = {'views':[{**base,'id':'fig1'}, {**base,'id':'fig2','part_ids':['p9']}],
           'parts':parts,'relations':[],'unresolved':[]}
    result = _validate_plan(raw,citations,[{'id':'original1'}])
    assert [v['id'] for v in result['views']] == ['fig1']
    view, = result['views']
    assert view['part_ids'] == ['p1']
    assert view['unverified_part_ids'] == ['p2']
    part, = result['parts']
    assert (part['id'],part['name'],part['mark'],part['model_name']) == (
        'p1','支承组件','11','支承组件（共四个）')
    assert len(result['renumbering_scope']) == 1
    assert result['renumbering_scope'][0]['view_ids'] == ['fig1']
    assert result['unresolved']


def test_relation_part_ids_resolve_only_to_names_grounded_in_its_own_quote():
    from patent_drawing.material_work import _validate_plan

    quote='终端收到确认后才移除对应队列项。'
    citations=[{'id':'e1','quote':quote},{'id':'e2','quote':'服务器返回已获确认。'}]
    parts=[{'id':ident,'name':name,'mark':'','evidence_id':evidence}
           for ident,name,evidence in [('p1','终端','e1'),('p2','对应队列项','e1'),('p3','服务器','e2')]]
    view={'id':'fig1','type':'flow','title':'同步流程','purpose':'保留确认门控','disposition':'new',
          'original_ids':[],'evidence_ids':['e1'],'depends_on':[],'part_ids':['p1','p2']}
    relation={'subject':'p1','predicate':'移除','object':'p2','quote':quote,
              'condition_quote':'收到确认后才','view_ids':['fig1'],'evidence_id':'e1'}
    raw={'views':[view],'parts':parts,'relations':[relation,{**relation,'subject':'p3'}],'unresolved':[]}
    result=_validate_plan(raw,citations,[])
    accepted,=result['relations']
    assert (accepted['subject'],accepted['object'])==('终端','对应队列项')
    assert (accepted['model_subject'],accepted['model_object'])==('p1','p2')
    assert accepted['condition_quote']=='收到确认后才'


def test_account_queue_ownership_uses_literal_endpoints_and_retains_unproved_part_binding():
    from patent_drawing.material_work import _validate_plan

    quote = '限定：账户切换时必须隔离各账户的队列；'
    citations = [{'id':'e1','quote':quote}, {'id':'e2','quote':'修改写入本地待同步队列。'}]
    parts = [{'id':'p5','name':'本地待同步队列','mark':'','evidence_id':'e2'},
             {'id':'p21','name':'账户','mark':'','evidence_id':'e1'}]
    view = {'id':'fig2','type':'relationship','title':'队列隔离','purpose':'核对归属',
            'disposition':'new','original_ids':[],'evidence_ids':['e1','e2'],
            'depends_on':[],'part_ids':['p5','p21']}
    relation = {'subject':'p5','object':'p21','predicate':'切换账户时必须按账户隔离',
                'evidence_id':'e1','quote':quote,'condition_quote':'账户切换时','view_ids':['fig2']}
    raw = {'views':[view],'parts':parts,'relations':[relation], 'unresolved':[]}
    output = _validate_plan(raw,citations,[])
    item, = output['relations']
    assert (item['subject'],item['object'],item['view_ids']) == ('账户','队列',['fig2'])
    proof = item['ownership']
    assert proof['condition']['quote'] == '账户切换时'
    assert proof['owner_scope']['quote'] == '各账户的队列'
    assert proof['part_binding'] == {'status':'pending','part_id':'p5'}
    for key in ('condition','owner','owner_scope','queue','isolation'):
        span = proof[key]
        assert quote.encode()[span['start']:span['end']].decode() == span['quote']

    literal = {**relation,'subject':'账户','object':'各账户的队列','predicate':'归属'}
    verified, = _validate_plan({**raw,'relations':[literal]},citations,[])['relations']
    assert (verified['subject'],verified['object']) == ('账户','队列')
    assert verified['ownership']['part_binding'] == {'status':'literal'}

    for changed in ({'subject':'队列','object':'账户','predicate':'拥有'},
                    {'subject':'账户','object':'队列','predicate':'共用'},
                    {'subject':'终端A','object':'队列','predicate':'隔离'},
                    {'subject':'账户A','object':'队列','predicate':'隔离'},
                    {'predicate':'不必隔离'},
                    {'predicate':'无需隔离'},
                    {'subject':'账户','object':'队列','predicate':'共用',
                     'quote':'必须隔离各账户的队列','condition_quote':''},
                    {'condition_quote':''},{'view_ids':['fig3']},
                    {'quote':'隔离各账户的队列','condition_quote':'账户切换时'}):
        assert not _validate_plan({**raw,'relations':[{**relation,**changed}]},citations,[])['relations']
