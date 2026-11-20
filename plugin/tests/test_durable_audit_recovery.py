import sqlite3

import httpx
import pytest

from patent_drawing.operator_policy import AuditedTransport, load_policy
from patent_drawing.workspace import Workspace
from patent_drawing.work_worker import WorkWorker
from tests.test_duplicate_image_dispatch import policy_file, staged, successful_reply
from tests.test_durable_work import WorkerStopped, attach_intake, finish, queue_setup


@pytest.mark.parametrize('changed', [None, 'response.bin', 'headers.json'])
def test_restart_recovers_verified_audited_response_before_workspace_receipt(tmp_path, monkeypatch, changed):
    policy_path = policy_file(tmp_path, 'fixture')
    source = load_policy(policy_path)
    operator = {'sha256': source.sha256, 'limits': source.limits, 'run_id': source.run_id}
    monkeypatch.setenv('PATENT_DRAWING_OPERATOR_POLICY', str(policy_path))
    store, queue, actor = queue_setup(tmp_path / 'workspace')
    sent = []

    def supplier(request):
        assert 'x-patent-work-context' not in request.headers
        sent.append(request.url.path)
        return successful_reply(request)

    class InterruptAfterAuditing(AuditedTransport):
        def handle_request(self, request):
            response = super().handle_request(request)
            response.close()
            raise WorkerStopped()

    transport = InterruptAfterAuditing(source, inner=httpx.MockTransport(supplier))
    original, wid = staged(store, 'audit', transport)
    store.save(actor.owner, 'workspace:' + wid, original.read(wid))
    original.owner = actor.owner
    attach_intake(original, wid)
    receipt = queue.enqueue(actor, wid, 1, 'draw', allow_models=True, policy=operator)
    worker = WorkWorker(queue, lambda owner: original)
    with pytest.raises(WorkerStopped):
        worker.run_one()
    snapshot = queue.inspect(actor, wid)
    assert snapshot['job_status'] == 'running'
    evidence = store.load(actor.owner, snapshot['operation_id'] + ':evidence')
    assert set(evidence) == {'request'}
    if changed:
        with sqlite3.connect(transport.db) as ledger:
            slot, = ledger.execute('SELECT slot FROM calls WHERE run=?', (source.run_id,)).fetchone()
        path = source.ledger_root / source.run_id / slot / changed
        path.write_bytes(path.read_bytes() + b' ')
    restarted = WorkWorker(queue, lambda owner: Workspace(store, owner))
    result = finish(restarted, receipt, queue, actor, wid)
    assert sent == ['/v1/images/generations']
    if changed:
        assert result['job_status'] == 'unknown'
        assert result['resume_action'] == 'inspect_original_result'
        assert result['dispatch_state'] == 'dispatching'
    else:
        assert result['job_status'] == 'partial'
        assert result['dispatch_state'] == 'response_saved'
        assert 'main.png' in queue.files(actor, wid, receipt['task_id'])


def test_luna_ballots_resume_from_independently_audited_child_operations(tmp_path, monkeypatch):
    import json
    from threading import Barrier

    from patent_drawing.figure_routing import DecisionClient
    from patent_drawing.intake import Intake

    policy_path = policy_file(tmp_path, 'luna-paired')
    policy = load_policy(policy_path)
    monkeypatch.setenv('PATENT_DRAWING_OPERATOR_POLICY', str(policy_path))
    store, queue, actor = queue_setup(tmp_path / 'workspace')
    body = '1. 一种装置，包括相连的两个端部。'
    intake = Intake(Workspace(store, actor.owner))
    initial = intake.apply({'source_mode': 'claims_only', 'query': '预览', 'materials': body},
                           [{'name': 'claims.txt', 'blob': body.encode()}])
    confirmed = intake.apply({'source_mode': 'claims_only', 'query': '确认',
                              'expected_revision': initial['intake_revision']})
    wid = confirmed['workspace_id']
    entered = Barrier(2, timeout=5)
    sent = []

    def supplier(request):
        assert request.url.path == '/v1/decisions'
        assert 'x-patent-work-context' not in request.headers
        payload = json.loads(request.content)
        assert payload['input'] == body
        sent.append(payload)
        entered.wait()
        return httpx.Response(200, json={'model': 'gpt-6-luna', 'answers': [
            {'name': q['name'], 'type': 'choice', 'choice': next(
                option['value'] for option in q['choices']
                if option['value'] in {'not_needed', 'not_disclosed', 'not_applicable'})}
            for q in payload['questions']
        ], 'usage': {'input_tokens': 32}}, headers={'x-request-id': 'req_luna_pair'})

    class Interrupted(AuditedTransport):
        def handle_request(self, request):
            super().handle_request(request).close()
            raise WorkerStopped()

    transport = Interrupted(policy, inner=httpx.MockTransport(supplier))
    worker = WorkWorker(queue, lambda owner: Workspace(
        store, owner, allow_models=True, decision_client=DecisionClient('offline-test', transport=transport)))
    operator = {'sha256':policy.sha256,'limits':policy.limits,'run_id':policy.run_id}
    job = queue.enqueue(actor, wid, int(confirmed['intake_revision']), 'plan',
                        query='规划', allow_models=True, policy=operator)
    with pytest.raises(WorkerStopped):
        worker.run_one()
    with sqlite3.connect(transport.db) as ledger:
        audit = ledger.execute('SELECT task,operation FROM work_audit WHERE run=?',(policy.run_id,)).fetchall()
    assert len(audit) == 2 and len({operation for _,operation in audit}) == 2
    for task, operation in audit:
        assert task == job['task_id'] and operation.startswith('figure_vote_')
        assert set(store.load(actor.owner, operation + ':evidence')) == {'request'}

    recovery = WorkWorker(queue, lambda owner: Workspace(store, owner))
    assert recovery.run_one()
    end = queue.inspect(actor, wid, task_id=job['task_id'])
    assert len(sent) == 2 and end['execution_stage'] == 'planning'
    assert end['claims_vote_review'] == {'disagreements': 0, 'rejudged': 0, 'uncertain': 0, 'low_confidence': 0}
    receipt = store.load(actor.owner, end['operation_id'] + ':receipt')
    assert all(ballot['usage']['input_tokens'] == 32 for ballot in receipt['ballots'].values())


def test_material_ballot_keeps_its_worker_binding_in_the_audit_transport(tmp_path):
    from patent_drawing.figure_routing import DecisionClient
    from patent_drawing.operator_policy import bind_work_operation, bound_work_task
    from patent_drawing.text_client import OpenAITextClient

    policy = load_policy(policy_file(tmp_path, 'material-audit'))
    operation = 'material_' + 'a' * 64
    worker = ('material-app', 'b' * 64, 'work_' + 'c' * 64, 'd' * 64)
    responses = []

    def supplier(request):
        assert request.url.path == '/v1/decisions'
        assert 'x-patent-work-context' not in request.headers
        responses.append(request.content)
        return httpx.Response(200, json={'model': 'gpt-6-luna', 'answers': [
            {'type': 'choice', 'name': 'sec1', 'choice': 'disclosure'},
        ]}, headers={'x-request-id': 'req_document_classification'})

    transport = AuditedTransport(policy, inner=httpx.MockTransport(supplier))
    ballot = {'model':'gpt-6-luna', 'input':'材料正文', 'questions': [
        {'type':'choice', 'name':'sec1', 'instructions':'判别材料类型',
         'choices':[{'value':'disclosure','description':'交底书'},{'value':'other','description':'其他材料'}]},
    ]}
    with bound_work_task(*worker):
        bind_work_operation(operation)
        result = DecisionClient('test', transport=transport).classify_ballot(ballot, lambda *_:None)
        with pytest.raises(ValueError, match='invalid internal task context'):
            OpenAITextClient('test', transport=transport).plan('规划', '材料正文')
    assert result['choices'] == {'sec1':'disclosure'}
    assert len(responses) == 1
    with sqlite3.connect(transport.db) as connection:
        assert connection.execute('select app,build,task,owner,operation from work_audit').fetchall() == [
            (*worker, operation),
        ]
        assert connection.execute('select kind,state,status from calls').fetchall() == [
            ('text','response_saved',200),
        ]
