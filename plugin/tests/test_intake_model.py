"""The classifier's private policy is independent of Chatflow parameters."""
import json

import httpx
import pytest

from patent_drawing.operation_store import OperationStore


def classifier(tmp_path, monkeypatch, handler, *, limit=2, approved='a' * 64):
    from patent_drawing.intake_model import IntakeClassifier
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'schema': 'patent-intake-policy-1', 'enabled': True,
        'run_id': 'synthetic-run', 'model': 'gpt-6-luna', 'mode': 'decisions',
        'limit': limit, 'session_limit': 1, 'source_fingerprints': [approved], 'data_scope': 'synthetic'}))
    path.chmod(0o600)
    monkeypatch.setenv('PATENT_DRAWING_INTAKE_POLICY', str(path))
    return IntakeClassifier(OperationStore(tmp_path / 'state'), 'alice:case', 'sk-test-only',
                            transport=httpx.MockTransport(handler))


def test_closed_gate_never_sends(tmp_path, monkeypatch):
    from patent_drawing.intake_model import IntakeClassifier
    monkeypatch.delenv('PATENT_DRAWING_INTAKE_POLICY', raising=False)
    model = IntakeClassifier(OperationStore(tmp_path / 'state'), 'owner', 'sk-test-only',
        transport=httpx.MockTransport(lambda r: pytest.fail('closed policy dispatched')))
    assert model.classify('技术描述', 'a' * 64) == {'state': 'disabled', 'model_calls': 0}


def test_decisions_is_official_bounded_and_source_replay_is_saved(tmp_path, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'https://api.openai.com/v1/decisions'
        body = json.loads(request.content)
        assert body['model'] == 'gpt-6-luna'
        return httpx.Response(200, json={'answers': [{'type': 'choice', 'name': 'technical_type',
            'choice': 'mechanical'}], 'usage': {'input_tokens': 12, 'output_tokens': 0}})
    model = classifier(tmp_path, monkeypatch, handler)
    first = model.classify('一个支撑机构。', 'a' * 64)
    second = model.classify('一个支撑机构。', 'a' * 64)
    assert first['technical_type'] == 'mechanical'
    assert second['technical_type'] == 'mechanical'
    assert first['model_calls'] == 1 and second['model_calls'] == 0
    assert len(requests) == 1
    raw = model.store.get_artifact('intake-policy:synthetic-run', first['request_ref'])
    assert raw == requests[0].content


@pytest.mark.parametrize('limit,fingerprint', [(0, 'a' * 64), (2, 'b' * 64)])
def test_budget_and_source_scope_reject_before_network(tmp_path, monkeypatch, limit, fingerprint):
    model = classifier(tmp_path, monkeypatch, lambda r: pytest.fail('unauthorized send'), limit=limit)
    result = model.classify('技术描述', fingerprint)
    assert result['model_calls'] == 0 and result['state'] == 'not_dispatched'


def test_invalid_response_is_retained_without_retry(tmp_path, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'answers': [{'type': 'choice', 'name': 'technical_type',
            'choice': 'invented-category'}]})
    model = classifier(tmp_path, monkeypatch, handler)
    first = model.classify('技术描述', 'a' * 64)
    second = model.classify('技术描述', 'a' * 64)
    assert first['state'] == second['state'] == 'invalid_output'
    assert len(requests) == 1
