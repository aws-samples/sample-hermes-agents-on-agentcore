import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend import app as module
from backend import events
from backend.service import token_hash
from common.security import directory
from runtime import server


@pytest.fixture
def private_api(durable, monkeypatch):
    svc = SimpleNamespace(
        agent=Mock(return_value={**durable.agent, 'status': 'ready'}),
        conversation=Mock(return_value=durable.conversation),
        query=Mock(return_value=[]), put=Mock(), history_page=Mock(return_value={'messages': [], 'next_cursor': None}))
    user = {**durable.user, 'team_ids': [durable.agent['team_id']], 'admin': True}
    module.app.dependency_overrides[module.current_user] = lambda: user
    monkeypatch.setattr(module, 'services', lambda: svc)
    monkeypatch.setattr(module, 'run_store', lambda: durable.store)
    monkeypatch.setenv('PUBLIC_URL', 'https://portal.example')
    yield SimpleNamespace(client=TestClient(module.app), svc=svc, user=user, durable=durable,
                          base=f"/api/agents/{durable.agent['id']}/conversations",
                          headers={'Origin': 'https://portal.example', 'Idempotency-Key': 'key'})
    module.app.dependency_overrides.clear()


def test_listing_returns_only_requesters_conversations_and_hides_unowned_legacy(private_api):
    p = private_api
    p.svc.query.return_value = [
        {'id': 'mine', 'created_at': 1, 'owner_sub': p.user['sub']},
        {'id': 'theirs', 'created_at': 1, 'owner_sub': str(uuid4())},
        {'id': 'unowned-legacy', 'created_at': 1},
    ]
    assert p.client.get(p.base).json() == [{'id': 'mine', 'created_at': 1}]


def test_creator_comes_from_identity_not_body(private_api):
    p = private_api
    response = p.client.post(p.base, json={'owner_sub': 'somebody-else'}, headers=p.headers)
    assert response.status_code == 200
    assert p.svc.put.call_args.kwargs['owner_sub'] == p.user['sub']


@pytest.mark.parametrize('owner', [None, 'different-human'])
def test_same_team_even_admin_cannot_read_or_write_others_conversation(private_api, owner):
    p = private_api
    p.svc.conversation.return_value = {**p.durable.conversation, 'owner_sub': owner}
    base = p.base + '/' + p.durable.conversation['id']
    run = str(uuid4())
    for suffix in ('/messages', '/runs/active', f'/runs/{run}', f'/runs/{run}/events'):
        assert p.client.get(base + suffix).status_code == 404, suffix
    for suffix in ('/runs', '/chat', f'/runs/{run}/subscription'):
        assert p.client.post(base + suffix, json={'message': 'no'}, headers=p.headers).status_code == 404, suffix
    p.svc.history_page.assert_not_called()
    assert p.durable.store.table.scan()['Items'] == []


def test_owner_can_read_history_but_still_requires_current_team(private_api):
    p = private_api
    url = p.base + '/' + p.durable.conversation['id'] + '/messages'
    assert p.client.get(url).status_code == 200
    p.user['team_ids'] = []
    assert p.client.get(url).status_code == 404


def test_run_creation_transaction_rechecks_owner_after_api_read(durable):
    changed = {**durable.conversation, 'owner_sub': str(uuid4())}
    durable.store.metadata.put_item(Item={'pk': 'AGENT#' + durable.agent['id'],
                                          'sk': 'CONV#' + changed['id'], **changed})
    with pytest.raises(durable.store.client.exceptions.TransactionCanceledException):
        durable.store.create(durable.agent, durable.conversation, durable.user, 'key', 'no')
    assert durable.store.table.scan()['Items'] == []


def test_ticket_cannot_cross_human_ownership_even_with_shared_team(durable):
    token = 'ticket' * 10
    durable.store.table.put_item(Item={
        'pk': 'TICKET#' + token_hash(token), 'sk': 'META', 'expires': 9999999999,
        'sub': durable.user['sub'], 'username': durable.user['username'],
        'agent_id': durable.agent['id'], 'conversation_id': durable.conversation['id'],
        'channel': '/runs/private',
    })
    svc = SimpleNamespace(
        membership=Mock(return_value={'sub': durable.user['sub'], 'enabled': True,
                                     'groups': ['Humans'], 'team_ids': [durable.agent['team_id']]}),
        agent=Mock(return_value=durable.agent),
        conversation=Mock(return_value={**durable.conversation, 'owner_sub': 'someone-else'}))
    with patch('backend.events.run_store', return_value=durable.store), \
            patch('backend.events.services', return_value=svc):
        assert not events.authorize({'authorizationToken': token, 'requestContext': {
            'operation': 'EVENT_SUBSCRIBE', 'channel': '/runs/private'}}, None)['isAuthorized']


def test_sandbox_shares_workspace_skills_and_agent_memory_not_private_hermes_home(tmp_path, monkeypatch):
    import asyncio
    root = tmp_path / 'agent'
    (root / 'workspace').mkdir(parents=True)
    (root / 'hermes' / 'logs').mkdir(parents=True)
    (root / 'hermes' / 'logs' / 'agent.log').write_text('private old transcript')
    monkeypatch.setattr(server, 'bedrock', object(), raising=False)
    monkeypatch.setenv('MODEL_ALIAS', 'test')
    monkeypatch.setenv('BEDROCK_MODEL_ID', 'test')
    monkeypatch.setattr(server, 'start_brokers', lambda *args: [])
    captured = {}

    async def launch(*args, **kwargs):
        captured['args'] = args
        captured['fds'] = [os.fstat(fd).st_ino for fd in kwargs['pass_fds']]
        return SimpleNamespace(returncode=0, stdout=SimpleNamespace(
            readline=AsyncMock(return_value=b'{"type":"ready"}\n')))

    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', launch)
    with directory(root) as fd, directory(tmp_path) as control:
        worker = asyncio.run(server.PersistentWorker.start(
            str(uuid4()), str(uuid4()), False, 0, 'tokens', 0, fd, control))
    try:
        args = captured['args']
        assert captured['fds'] == [(root / 'workspace').stat().st_ino,
                                   (root / 'hermes' / 'skills').stat().st_ino,
                                   (root / 'hermes' / 'agent').stat().st_ino]
        binds = [(args[i + 1], args[i + 2]) for i, arg in enumerate(args) if arg == '--bind']
        assert not any(destination == '/workspace' for _, destination in binds)
        assert not any(destination == '/workspace/hermes' for _, destination in binds)
        assert any(destination == '/shared/agent' for _, destination in binds)
        assert ('HOME', '/state/home') in [(args[i + 1], args[i + 2]) for i, arg in enumerate(args) if arg == '--setenv']
        # Framework-specific environment/configuration is now created by the adapter
        # inside the sandbox, after seccomp, rather than by the supervisor.
        assert 'HERMES_HOME' not in args
        assert not (worker.state / 'config.yaml').exists()
        assert any(args[i + 2] == '/shared/skills/.usage.json'
                   for i, arg in enumerate(args) if arg == '--ro-bind')
        assert not (worker.state / 'logs' / 'agent.log').exists()
    finally:
        worker._cleanup()
