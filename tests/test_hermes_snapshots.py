"""Hermes snapshot publication/recovery with generic fenced completion metadata."""

import asyncio
import fcntl
import os
import sqlite3
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from agents.hermes.agent_adapter import Adapter
from agents.hermes.lifecycle import (
    Lifecycle,
    checkpoint_filename,
    committed_checkpoint,
    restore_checkpoint,
    write_checkpoint,
)
from common.security import directory
from runtime import server
from runtime.contract import AgentConfig


@pytest.fixture
def hermes_adapter(tmp_path):
    # Use real snapshot I/O while replacing only the external framework reasoning loop.
    adapter = Adapter.__new__(Adapter)
    adapter.config = AgentConfig('session', 'model', state=tmp_path)
    adapter.database = tmp_path / 'state.db'
    with sqlite3.connect(adapter.database) as db:
        db.execute('CREATE TABLE history (text TEXT)')
        db.execute("INSERT INTO history VALUES ('committed turn')")
    adapter.invalidate_system_prompt = Mock()
    adapter.db = SimpleNamespace(get_messages_as_conversation=Mock(return_value=[]), close=Mock())
    adapter.agent = SimpleNamespace(session_id='session', run_conversation=Mock(), close=Mock())
    return adapter


@pytest.mark.parametrize('completed', [True, False])
def test_hermes_run_creates_its_own_snapshot_before_returning(hermes_adapter, completed):
    adapter = hermes_adapter
    adapter.agent.run_conversation.return_value = {
        'completed': completed, 'final_response': 'answer', 'turn_exit_reason': 'budget'}
    result = adapter.run('hello')
    assert result.completed is completed and result.text == 'answer'
    with sqlite3.connect(adapter.config.state / 'checkpoint.db') as db:
        assert db.execute('SELECT text FROM history').fetchall() == [('committed turn',)]
    assert not (adapter.config.state / 'checkpoint.next.db').exists()


def test_hermes_snapshot_failure_prevents_successful_turn_result(hermes_adapter):
    adapter = hermes_adapter
    adapter.agent.run_conversation.return_value = {'completed': True, 'final_response': 'answer'}
    with patch.object(adapter, 'save_snapshot', side_effect=OSError('snapshot failed')), \
            pytest.raises(OSError, match='snapshot failed'):
        adapter.run('hello')


def test_failed_hermes_turn_does_not_create_a_snapshot(hermes_adapter):
    adapter = hermes_adapter
    adapter.agent.run_conversation.return_value = {'failed': True, 'error': 'turn failed'}
    with pytest.raises(RuntimeError, match='turn failed'):
        adapter.run('hello')
    assert not (adapter.config.state / 'checkpoint.db').exists()


@pytest.fixture
def checkpoint_turn(durable, tmp_path, monkeypatch):
    store = durable.store
    run = store.claim(store.create(durable.agent, durable.conversation,
                                   durable.user, 'key', 'hello')['id'], 'owner')
    sub = durable.agent['sub']
    (tmp_path / sub).mkdir()
    control = tmp_path / '.control' / sub
    control.mkdir(parents=True)
    legacy = control / f"{run['conversation_id']}.db"
    legacy.write_bytes(b'previous completed session database')
    local = tmp_path / 'local'
    local.mkdir()
    (local / 'checkpoint.db').write_bytes(b'new session database')
    closes = []

    async def close():
        # Even failed finalization must finish worker shutdown before admitting another turn.
        assert server.ping()['status'] == 'HealthyBusy'
        descriptor = os.open(control / 'execution.lock', os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        closes.append(True)

    lifecycle = Lifecycle()
    with directory(control) as control_fd:
        lifecycle.before_start(local, control_fd, run['conversation_id'], {})
    fake_worker = SimpleNamespace(state=local, adapter_state=None, lifecycle=lifecycle, close=close)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'busy', True)
    monkeypatch.setattr(server, 'worker', fake_worker)
    monkeypatch.setattr(server, 'get_worker', AsyncMock(return_value=(fake_worker, False)))
    payload = server.Invocation(operation='start', run_id=run['id'],
                                conversation_id=run['conversation_id'],
                                team_id=durable.agent['team_id'], message='hello')

    def produce(kind='complete'):
        async def events(_payload):
            yield {'type': kind, 'text': 'answer', 'worker_instance_id': 'worker-id'}

        fake_worker.events = events

        async def invoke():
            monkeypatch.setattr(server, 'worker_lock', asyncio.Lock(), raising=False)
            await server.produce_run(store, run, sub, payload)
        asyncio.run(invoke())

    def restore():
        restored = tmp_path / f'restored-{uuid4()}'
        restored.mkdir()
        with directory(control) as fd:
            restore_checkpoint(restored, fd, run['conversation_id'],
                               committed_checkpoint(store.conversation(run)))
        return (restored / 'state.db').read_bytes()

    return SimpleNamespace(store=store, run=run, control=control, legacy=legacy,
                           worker=fake_worker, closes=closes, produce=produce, restore=restore)


@pytest.mark.parametrize('kind', ['complete', 'partial'])
def test_checkpoint_pointer_commits_with_message_and_run_before_worker_reuse(checkpoint_turn, kind):
    turn = checkpoint_turn
    expected = checkpoint_filename(turn.run['conversation_id'], turn.run['id'])
    append = turn.store.append

    def commit(run, data, **kwargs):
        if kwargs.get('adapter_state'):
            assert server.ping()['status'] == 'HealthyBusy'
            assert committed_checkpoint(turn.store.conversation(run)) is None
            assert (turn.control / expected).read_bytes() == b'new session database'
            assert turn.legacy.read_bytes() == b'previous completed session database'
        return append(run, data, **kwargs)

    with patch.object(turn.store, 'append', side_effect=commit):
        turn.produce(kind)

    assert turn.store.run(turn.run['id'])['status'] == kind
    assert committed_checkpoint(turn.store.conversation(turn.run)) == expected
    assert turn.restore() == b'new session database'
    assert turn.worker.adapter_state == {'adapter': 'hermes-v1', 'checkpoint': expected}
    assert not turn.closes
    assert server.ping()['status'] == 'Healthy'
    message = next(item for item in turn.store.metadata.scan()['Items']
                   if item.get('role') == 'assistant')
    assert message['text'] == 'answer' and message['run_id'] == turn.run['id']
    # Run/event TTL must not delete the conversation's authoritative checkpoint pointer.
    turn.store.table.delete_item(Key={'pk': turn.run['pk'], 'sk': 'META'})
    assert turn.restore() == b'new session database'


def test_reconciliation_during_finalization_leaves_orphan_and_discards_worker(checkpoint_turn):
    turn = checkpoint_turn
    append = turn.store.append

    def reconcile_before_commit(run, data, **kwargs):
        if kwargs.get('adapter_state'):
            append(deepcopy(run), {'type': 'interrupted'}, status='interrupted')
        return append(run, data, **kwargs)

    with patch.object(turn.store, 'append', side_effect=reconcile_before_commit):
        turn.produce()

    assert turn.store.run(turn.run['id'])['status'] == 'interrupted'
    assert committed_checkpoint(turn.store.conversation(turn.run)) is None
    assert turn.restore() == b'previous completed session database'
    assert (turn.control / checkpoint_filename(
        turn.run['conversation_id'], turn.run['id'])).read_bytes() == b'new session database'
    assert turn.closes == [True] and server.worker is None
    assert server.ping()['status'] == 'Healthy'
    assert not any(item.get('role') == 'assistant' for item in turn.store.metadata.scan()['Items'])


def test_failure_after_candidate_written_before_commit_does_not_advance_database(checkpoint_turn):
    turn = checkpoint_turn
    append = turn.store.append

    def fail_commit(run, data, **kwargs):
        if kwargs.get('adapter_state'):
            raise OSError('connection lost before commit')
        return append(run, data, **kwargs)

    with patch.object(turn.store, 'append', side_effect=fail_commit):
        turn.produce()
    assert committed_checkpoint(turn.store.conversation(turn.run)) is None
    assert turn.restore() == b'previous completed session database'
    assert turn.closes == [True] and server.worker is None
    assert not any(item.get('role') == 'assistant' for item in turn.store.metadata.scan()['Items'])


def test_ambiguous_success_discards_worker_but_restores_committed_pointer(checkpoint_turn):
    turn = checkpoint_turn
    append = turn.store.append

    def fail_after_commit(run, data, **kwargs):
        if kwargs.get('adapter_state'):
            # Commit on the server while the caller never receives the successful response.
            append(deepcopy(run), data, **kwargs)
            raise OSError('response lost after commit')
        return append(run, data, **kwargs)

    with patch.object(turn.store, 'append', side_effect=fail_after_commit):
        turn.produce()
    assert turn.store.run(turn.run['id'])['status'] == 'complete'
    assert turn.restore() == b'new session database'
    assert turn.closes == [True] and server.worker is None


def test_run_checkpoint_publication_never_overwrites(tmp_path):
    name = checkpoint_filename(str(uuid4()), str(uuid4()))
    with directory(tmp_path) as fd:
        write_checkpoint(fd, name, b'first', immutable=True)
        with pytest.raises(FileExistsError):
            write_checkpoint(fd, name, b'second', immutable=True)
    assert (tmp_path / name).read_bytes() == b'first'
    assert list(tmp_path.iterdir()) == [tmp_path / name]


def test_restore_ignores_orphans_and_fails_closed_for_missing_committed_file(tmp_path):
    conversation_id = str(uuid4())
    name = checkpoint_filename(conversation_id, str(uuid4()))
    other = checkpoint_filename(str(uuid4()), str(uuid4()))
    (tmp_path / name).write_bytes(b'orphan')
    local = tmp_path / 'local'
    local.mkdir()
    with directory(tmp_path) as fd:
        restore_checkpoint(local, fd, conversation_id)
        assert not (local / 'state.db').exists()
        with pytest.raises(ValueError, match='Invalid committed checkpoint'):
            restore_checkpoint(local, fd, conversation_id, other)
        with pytest.raises(ValueError):
            restore_checkpoint(local, fd, conversation_id, '../state.db')
        with pytest.raises(RuntimeError, match='unavailable'):
            restore_checkpoint(local, fd, conversation_id,
                               checkpoint_filename(conversation_id, str(uuid4())))


def test_hermes_missing_snapshot_is_rejected_before_completion(checkpoint_turn):
    turn = checkpoint_turn
    (turn.worker.state / 'checkpoint.db').unlink()
    turn.produce()
    assert turn.store.run(turn.run['id'])['status'] == 'failed'
    assert committed_checkpoint(turn.store.conversation(turn.run)) is None
    assert turn.closes == [True]


def test_legacy_metadata_pointer_is_restored_and_new_adapter_state_takes_precedence(tmp_path):
    conversation_id = str(uuid4())
    old = checkpoint_filename(conversation_id, str(uuid4()))
    new = checkpoint_filename(conversation_id, str(uuid4()))
    (tmp_path / old).write_bytes(b'legacy')
    (tmp_path / new).write_bytes(b'current')
    state = tmp_path / 'local'
    state.mkdir()
    lifecycle = Lifecycle()
    with directory(tmp_path) as fd:
        lifecycle.before_start(state, fd, conversation_id, {'checkpoint_name': old})
        assert (state / 'state.db').read_bytes() == b'legacy'
        lifecycle.before_start(state, fd, conversation_id, {
            'checkpoint_name': old, 'adapter_state': {'adapter': 'hermes-v1', 'checkpoint': new}})
        assert (state / 'state.db').read_bytes() == b'current'
        with pytest.raises(ValueError, match='Incompatible'):
            lifecycle.before_start(state, fd, conversation_id, {'adapter_state': {'adapter': 'other'}})
