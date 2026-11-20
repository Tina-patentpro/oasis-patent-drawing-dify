import sqlite3
from contextlib import contextmanager

import pytest

from patent_drawing.operation_store import OperationStore


def test_saved_response_can_be_read_during_sqlite_writer_contention(tmp_path):
    store = OperationStore(tmp_path)
    assert store.run_once('owner', 'once', {'view': 'main'}, lambda: {'answer': 'saved'}) == {
        'answer': 'saved',
    }
    with sqlite3.connect(store.db) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.MonkeyPatch.context() as patch:
            fast_connect = store.connect

            @contextmanager
            def use_fast_timeout():
                connection = sqlite3.connect(store.db, timeout=0)
                try:
                    with connection:
                        yield connection
                finally:
                    connection.close()

            patch.setattr(store, 'connect', use_fast_timeout)
            assert store.run_once('owner', 'once', {'view': 'main'}, lambda: pytest.fail('duplicate')) == {
                'answer': 'saved',
            }
            patch.setattr(store, 'connect', fast_connect)


def test_successful_response_recovers_after_process_exits_before_commit(tmp_path, monkeypatch):
    store = OperationStore(tmp_path)
    connect = store.connect
    sent = []

    @contextmanager
    def fail_after_response():
        if sent:
            raise sqlite3.OperationalError('database is locked')
        with connect() as connection:
            yield connection

    def produce():
        sent.append('one')
        return {'answer': 'saved before SQLite write'}

    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', fail_after_response)
        with pytest.raises(sqlite3.OperationalError):
            store.run_once('owner', 'once', {'view': 'main'}, produce)

    fresh = OperationStore(tmp_path)
    assert fresh.run_once('owner', 'once', {'view': 'main'}, lambda: pytest.fail('duplicate')) == {
        'answer': 'saved before SQLite write',
    }
    assert fresh.dispatch_count('owner', 'once') == 1 and len(sent) == 1
    with pytest.raises(ValueError, match='identity mismatch'):
        fresh.run_once('owner', 'once', {'view': 'other'}, lambda: pytest.fail('wrong identity'))


def test_interrupted_external_action_with_no_response_stays_unknown(tmp_path):
    class ProcessStopped(BaseException):
        pass

    def interrupted():
        raise ProcessStopped

    with pytest.raises(ProcessStopped):
        OperationStore(tmp_path).run_once('owner', 'once', {'view': 'main'}, interrupted)

    fresh = OperationStore(tmp_path)
    with pytest.raises(ValueError, match='UNKNOWN'):
        fresh.run_once('owner', 'once', {'view': 'main'}, lambda: pytest.fail('duplicate'))
    assert fresh.dispatch_count('owner', 'once') == 1


def test_existing_tenant_namespace_can_be_read_without_database_writer(tmp_path, monkeypatch):
    from tools.workspace import namespace

    class Storage:
        def exist(self, key):
            return True

        def get(self, key):
            return b'a' * 32

    store = OperationStore(tmp_path)

    @contextmanager
    def no_global_writer():
        pytest.fail('reading an existing tenant namespace should not start a SQLite transaction')
        yield

    monkeypatch.setattr(store, 'connect', no_global_writer)
    assert namespace(store, Storage()) == 'a' * 32


@pytest.mark.parametrize('resume_from', ['checkpoint', 'raw-response'])
def test_workspace_resumes_a_saved_image_after_restart_with_models_closed(tmp_path, monkeypatch, resume_from):
    import httpx
    from patent_drawing.workspace import Workspace
    from tests.test_duplicate_image_dispatch import staged, successful_reply

    sent = []

    def reply(request):
        sent.append(request)
        return successful_reply(request)

    store = OperationStore(tmp_path)
    workspace, wid = staged(store, 'restart', httpx.MockTransport(reply))

    def stop_after_response(*args):
        raise sqlite3.OperationalError('database is locked')

    with monkeypatch.context() as patch:
        if resume_from == 'checkpoint':
            patch.setattr(store, '_commit_response', stop_after_response)
        else:
            save = store.save

            def stop_after_original(owner, name, value):
                save(owner, name, value)
                if name.startswith('render_') and name.endswith(':evidence') and 'headers' in value:
                    raise KeyboardInterrupt()

            patch.setattr(store, 'save', stop_after_original)
        with pytest.raises((sqlite3.OperationalError, KeyboardInterrupt)):
            workspace.render(wid, 'main')

    fresh = Workspace(OperationStore(tmp_path), workspace.owner, allow_models=False)
    assert fresh.preflight(wid)['resume_action'] == 'resume_local_composition'
    result = fresh.render(wid, 'main')
    assert result['model_calls'] == 0
    assert result['record']['request_id'] == 'req_offline'
    assert len(sent) == 1 and fresh.image_dispatch_count(wid) == 1


def test_interrupted_original_refusal_is_restored_without_resend(tmp_path, monkeypatch):
    import httpx
    from patent_drawing.workspace import Workspace
    from tests.test_duplicate_image_dispatch import staged

    sent = []

    def reject(request):
        sent.append(request)
        return httpx.Response(400, headers={'x-request-id': 'req_saved_refusal'},
                              json={'error': {'code': 'moderation_blocked', 'type': 'invalid_request_error'}})

    store = OperationStore(tmp_path)
    workspace, wid = staged(store, 'refused', httpx.MockTransport(reject))
    save = store.save

    def interrupt(owner, name, value):
        save(owner, name, value)
        if name.startswith('render_') and name.endswith(':evidence') and 'headers' in value:
            raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', interrupt)
        with pytest.raises(KeyboardInterrupt):
            workspace.render(wid, 'main')

    fresh = Workspace(OperationStore(tmp_path), workspace.owner)
    assert fresh.preflight(wid)['blocking_state'] == 'provider_rejected'
    with pytest.raises(ValueError, match='recovery authorization'):
        fresh.render(wid, 'main')
    assert len(sent) == fresh.image_dispatch_count(wid) == 1
