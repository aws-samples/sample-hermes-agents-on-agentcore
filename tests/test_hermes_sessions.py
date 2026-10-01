"""Hermes-specific session recovery through its optional host lifecycle integration."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from common.security import directory
from runtime import server
from runtime.contract import RUN_BUDGET_SECONDS


def test_agent_execution_budget_is_at_least_one_hour():
    assert server.EXECUTION_DEADLINE_SECONDS >= 60 * 60
    assert RUN_BUDGET_SECONDS >= 60 * 60


def write_database(path, message):
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS messages (text TEXT)')
            connection.execute('INSERT INTO messages VALUES (?)', (message,))
    finally:
        connection.close()


def read_messages(path):
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute('SELECT text FROM messages')]
    finally:
        connection.close()


@pytest.fixture
def session_storage(tmp_path, monkeypatch):
    sub = str(uuid4())
    (tmp_path / sub).mkdir()
    (tmp_path / sub / 'workspace').mkdir()
    (tmp_path / sub / 'hermes').mkdir()
    control = tmp_path / '.control' / sub
    control.mkdir(parents=True)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'busy', False)
    monkeypatch.setattr(server, 'bedrock', object(), raising=False)
    monkeypatch.setenv('BEDROCK_MODEL_ID', 'test-model')
    monkeypatch.setenv('MODEL_ALIAS', 'test-model')
    monkeypatch.setattr(server, 'start_brokers', lambda *args: [])
    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', AsyncMock(
        side_effect=lambda *args, **kwargs: SimpleNamespace(
            stdout=SimpleNamespace(readline=AsyncMock(return_value=b'{"type":"ready"}\n')),
            returncode=0,
        )))
    workers = []

    async def start(conversation_id):
        with directory(tmp_path, sub) as workspace_fd, directory(control) as control_fd:
            worker = await server.PersistentWorker.start(
                sub, conversation_id, False, 0, 'tokens', 0, workspace_fd, control_fd)
        workers.append(worker)
        return worker

    yield SimpleNamespace(sub=sub, control=control, start=start)
    for worker in workers:
        worker._cleanup()


@pytest.mark.parametrize('legacy_database', [False, True])
def test_new_session_does_not_inherit_another_database(session_storage, legacy_database):
    storage = session_storage
    existing_id, new_id = str(uuid4()), str(uuid4())
    write_database(storage.control / f'{existing_id}.db', 'another session')
    if legacy_database:
        write_database(storage.control / 'state.db', 'legacy agent-wide history')

    worker = asyncio.run(storage.start(new_id))

    # Hermes creates a fresh database when no checkpoint exists for this session.
    assert not (worker.state / 'state.db').exists()
    assert read_messages(storage.control / f'{existing_id}.db') == ['another session']


@pytest.mark.parametrize('terminal_event', ['complete', 'partial'])
def test_sessions_publish_and_restore_independent_databases(
        session_storage, monkeypatch, terminal_event):
    storage = session_storage
    first_id, second_id = str(uuid4()), str(uuid4())

    async def scenario():
        first = await storage.start(first_id)
        second = await storage.start(second_id)
        assert first.state != second.state

        async def publish(worker, message):
            write_database(worker.state / 'checkpoint.db', message)

            async def events(payload):
                yield {'type': terminal_event, 'text': message}

            worker.events = events
            monkeypatch.setattr(server, 'get_worker', AsyncMock(return_value=(worker, False)))
            payload = server.Invocation(
                conversation_id=worker.conversation_id, team_id=uuid4(), message=message)
            result = [json.loads(item.removeprefix('data: '))
                      async for item in server.execute(storage.sub, payload)]
            assert result[-1]['type'] == terminal_event

        await publish(first, 'first session')
        await publish(second, 'second session')
        # An older, idle worker must publish only to its own session's checkpoint.
        await publish(first, 'first session follow-up')
        assert not (storage.control / 'state.db').exists()
        assert read_messages(storage.control / f'{first_id}.db') == [
            'first session', 'first session follow-up']
        assert read_messages(storage.control / f'{second_id}.db') == ['second session']

        restored_first = await storage.start(first_id)
        restored_second = await storage.start(second_id)
        assert read_messages(restored_first.state / 'state.db') == [
            'first session', 'first session follow-up']
        assert read_messages(restored_second.state / 'state.db') == ['second session']
        write_database(restored_first.state / 'state.db', 'local uncheckpointed change')
        assert read_messages(storage.control / f'{first_id}.db') == [
            'first session', 'first session follow-up']

    asyncio.run(scenario())
