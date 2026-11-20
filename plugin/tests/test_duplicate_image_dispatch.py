import base64
import hashlib
import json
import sqlite3
import time

import httpx
import pytest

from patent_drawing.client import OpenAIImagesClient
from patent_drawing.operation_store import OperationStore, digest
from patent_drawing.operator_policy import AuditedTransport, load_policy
from patent_drawing.workspace import Workspace
from tests.test_images import picture


def policy_file(tmp_path, run, *, enabled=True, mode='initialize'):
    path = tmp_path / (run + '.json')
    path.write_text(json.dumps({
        'schema': 'patent-run-policy-1', 'run_id': run, 'enabled': enabled,
        'ledger_root': str(tmp_path / 'audit'), 'limits': {'image': None, 'text': None},
        'continuity': {'registry_path': str(tmp_path / 'registry.sqlite3'),
                       'ledger_id': 'a' * 64, 'mode': mode},
    }))
    path.chmod(0o600)
    return path


def staged(store, name, transport):
    w = Workspace(store, 'test:account:' + name, allow_models=True,
                  image_client=OpenAIImagesClient('offline-test', transport=transport))
    text = '连接板的边缘有一处缺口。'
    wid = w.ingest([{'name': 'disclosure.txt', 'blob': text.encode()}])['workspace_id']
    state = w.read(wid)
    w.stage(wid, {
        'schema_version': 'drawing-set-2', 'set_id': 'edge', 'revision': 1,
        'source_sha256': state['source_sha256'], 'title': '连接板边缘', 'patent_type': 'invention',
        'technical_types': ['mechanical'], 'parts': [{'id': 'plate', 'name': '连接板', 'label': '1'}],
        'facts': [{'id': 'edge', 'status': 'confirmed', 'text': text,
                   'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': text}]}],
        'views': [{'id': 'main', 'engine': 'image', 'requested_type': 'relationship',
                   'delivered_type': 'relationship', 'title': '关系图', 'purpose': '边缘关系',
                   'instruction': '示意连接板边缘的缺口', 'parts': ['plate'], 'required_facts': ['edge']}],
    }, image_budget=1)
    return w, wid


def successful_reply(request):
    return httpx.Response(200, headers={'x-request-id': 'req_offline'},
                         json={'data': [{'b64_json': base64.b64encode(picture()).decode()}]})


def test_same_run_duplicate_is_local_and_does_not_block_another_run(tmp_path):
    calls = []

    def reply(request):
        calls.append(request)
        return successful_reply(request)

    transport = AuditedTransport(load_policy(policy_file(tmp_path, 'first')),
                                 inner=httpx.MockTransport(reply))
    store = OperationStore(tmp_path / 'workspace')
    first, wid = staged(store, 'first', transport)
    assert first.render(wid, 'main')['model_calls'] == 1
    duplicate, wid = staged(store, 'duplicate', transport)

    with pytest.raises(ValueError, match='未发送图像请求'):
        duplicate.render(wid, 'main')

    gate = duplicate.preflight(wid)
    assert gate['blocking_state'] == 'duplicate_image_request'
    assert gate['supplier_state'] == 'not_called' and gate['model_calls'] == 0
    assert gate['route'] == 'reply' and gate['done'] == 'yes'
    assert '未发送图像请求' in duplicate.package_partial(wid)['message']
    assert duplicate.image_dispatch_count(wid) == 0
    with sqlite3.connect(transport.db) as audit:
        assert audit.execute('select state, status from calls').fetchall() == [('response_saved', 200)]
    with pytest.raises(ValueError, match='未发送图像请求'):
        duplicate.render(wid, 'main')
    assert len(calls) == 1

    other_transport = AuditedTransport(load_policy(policy_file(tmp_path, 'second', mode='resume')),
                                       inner=httpx.MockTransport(reply))
    independent, wid = staged(store, 'independent', other_transport)
    assert independent.preflight(wid)['blocking_state'] is None
    assert independent.render(wid, 'main')['model_calls'] == 1
    assert len(calls) == 2


def test_real_transport_unknown_still_blocks_source_family(tmp_path):
    def no_response(request):
        raise httpx.ReadTimeout('fixture lost the response')

    transport = AuditedTransport(load_policy(policy_file(tmp_path, 'first')),
                                 inner=httpx.MockTransport(no_response))
    store = OperationStore(tmp_path / 'workspace')
    original, wid = staged(store, 'first', transport)
    with pytest.raises(ValueError, match='result_unknown'):
        original.render(wid, 'main')

    other, wid = staged(store, 'independent', transport)
    assert other.preflight(wid)['blocking_state'] == 'result_unknown'
    assert other.preflight(wid)['supplier_state'] == 'result_unknown'
    with sqlite3.connect(transport.db) as audit:
        assert audit.execute('select state from calls').fetchall() == [('result_unknown',)]


@pytest.mark.parametrize('fault', [None, 'other_lineage', 'prior_unknown', 'prior_rejected',
                                  'later_claim', 'untrusted_terminal'])
def test_legacy_duplicate_requires_bound_no_send_evidence(tmp_path, monkeypatch, fault):
    from patent_drawing import operator_policy

    path = policy_file(tmp_path, 'first')
    enabled_raw = path.read_bytes()
    enabled = load_policy(path)
    transport = AuditedTransport(enabled, inner=httpx.MockTransport(successful_reply))
    store = OperationStore(tmp_path / 'workspace')
    first, wid = staged(store, 'first', transport)
    first.render(wid, 'main')
    duplicate, wid = staged(store, 'legacy', transport)
    message = 'request already dispatched; automatic retry forbidden'
    with monkeypatch.context() as patch:
        patch.setattr(operator_policy, 'DuplicateImageRequest', lambda: ValueError(message))
        with pytest.raises(ValueError, match=message):
            duplicate.render(wid, 'main')
    state = duplicate.read(wid)
    blocked = duplicate._blocked_attempt(state, 'main')
    assert blocked['state'] == 'result_unknown'
    op = blocked['operation_id']
    original = store.operation_status(duplicate.owner, op)
    workflow = 'c0363fd5-1ac8-48d0-9fe1-ce0147811c83'
    native = {
        'out': {'kind': 'native_real', 'provider_dispatches': 4, 'llm_dispatches': 0,
                'workflow_run_id': workflow, 'invocation_returned': True,
                'terminal': {'id': workflow, 'status': 'failed', 'outputs': {}, 'total_tokens': 0,
                             'created_at': int(time.time()) + 1,
                             'error': '[drawing_workspace_error] ' + message}},
        'artifacts': [],
        'metadata': [{'workspace_id': wid, 'model_calls': int(fault == 'untrusted_terminal')}],
    }
    raw = json.dumps(native).encode()
    evidence = {'operation_id': op, 'run_id': enabled.run_id, 'policy_sha256': enabled.sha256,
                'request_sha256': blocked['candidate_sha256'], 'source_sha256': state['source_sha256'],
                'lineage_sha256': duplicate.lineage_context(state)[1],
                'plan_sha256': digest(state['work']['plan']),
                'native_terminal_sha256': hashlib.sha256(raw).hexdigest()}
    closed = policy_file(tmp_path, 'closed', enabled=False, mode='resume')
    monkeypatch.setenv('PATENT_DRAWING_OPERATOR_POLICY', str(closed))
    monkeypatch.setenv('PATENT_DRAWING_ISOLATED_WORKER', '1')
    attestations = tmp_path / 'local-no-send'
    attestations.mkdir(mode=0o700)
    attestation = attestations / (op + '.json')
    attestation.write_text(json.dumps({'workspace_id': wid, 'view_id': 'main',
        'evidence': evidence, 'workflow_run_id': workflow, 'closed_policy_sha256': load_policy(closed).sha256,
        'ledger_id': enabled.continuity['ledger_id']}))
    attestation.chmod(0o600)
    with sqlite3.connect(transport.db) as audit:
        if fault == 'other_lineage':
            audit.execute('update lineage_calls set lineage=?', ('b' * 64,))
        if fault == 'prior_unknown':
            audit.execute("update calls set state='result_unknown'")
        if fault == 'prior_rejected':
            audit.execute("update calls set state='provider_rejected',status=400")
        if fault == 'later_claim':
            audit.execute('update calls set started=?', (native['out']['terminal']['created_at']+1,))
    if fault:
        with pytest.raises(ValueError):
            duplicate.record_local_duplicate_rejection(wid, 'main', evidence, raw, enabled_raw, 'offline')
        assert duplicate._blocked_attempt(state, 'main')['state'] == 'result_unknown'
    else:
        result = duplicate.record_local_duplicate_rejection(wid, 'main', evidence, raw, enabled_raw, 'offline')
        assert result['state'] == 'confirmed_not_dispatched' and result['model_calls'] == 0
        assert duplicate._blocked_attempt(state, 'main') is None
        assert duplicate.read(wid) == state
        assert duplicate.record_local_duplicate_rejection(wid, 'main', evidence, raw, enabled_raw,
                                                          'offline') == result
    assert store.operation_status(duplicate.owner, op) == original
