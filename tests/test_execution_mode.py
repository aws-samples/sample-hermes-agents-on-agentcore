import asyncio
import fcntl
import json
import os
import runpy
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from backend import events
from backend.app import AgentSettings, CreateAgent, create_agent, public_agent
from backend.service import Services
from common.execution import execution_mode
from common.runs import Conflict, reservation_key
from runtime import server


def conversation(durable, *, other_user=False):
    user = {'sub': str(uuid4()), 'username': 'bob'} if other_user else durable.user
    item = {'id': str(uuid4()), 'owner_sub': user['sub'], 'runtime_session_id': str(uuid4())}
    durable.store.metadata.put_item(Item={'pk': 'AGENT#' + durable.agent['id'],
                                          'sk': 'CONV#' + item['id'], **item})
    return item, user


@pytest.mark.parametrize('mode', ['sequential', None])
def test_sequential_and_legacy_agents_block_other_conversations(durable, mode):
    if mode:
        durable.agent['execution_mode'] = mode
    run = durable.store.create(durable.agent, durable.conversation, durable.user, 'first', 'hello')
    other, user = conversation(durable, other_user=True)
    with pytest.raises(Conflict, match='agent already has an active run'):
        durable.store.create(durable.agent, other, user, 'second', 'hello')
    assert run['execution_mode'] == 'sequential'
    assert durable.store.get('AGENT#' + durable.agent['id'], 'LOCK')['run_id'] == run['id']


@pytest.mark.parametrize('status', ['complete', 'partial', 'failed', 'interrupted'])
def test_concurrent_agents_reserve_only_the_conversation_and_release_only_their_own_run(durable, status):
    durable.agent['execution_mode'] = 'concurrent'
    first = durable.store.create(durable.agent, durable.conversation, durable.user, 'first', 'hello')
    other, user = conversation(durable, other_user=True)
    second = durable.store.create(durable.agent, other, user, 'second', 'world')
    assert durable.store.get('AGENT#' + durable.agent['id'], 'LOCK') is None
    assert first['execution_mode'] == second['execution_mode'] == 'concurrent'
    assert reservation_key(first) != reservation_key(second)
    with pytest.raises(Conflict, match='conversation already has an active run'):
        durable.store.create(durable.agent, durable.conversation, durable.user, 'third', 'too soon')
    assert durable.store.create(durable.agent, durable.conversation, durable.user,
                                'first', 'hello')['id'] == first['id']
    first = durable.store.claim(first['id'], 'first-owner')
    second = durable.store.claim(second['id'], 'second-owner')
    assert first['status'] == second['status'] == 'running'
    assert durable.store.claim(first['id'], 'duplicate-dispatch') is None
    kwargs = ({'final_text': 'answer'}
              if status in {'complete', 'partial'} else {})
    durable.store.append(first, {'type': status}, status=status, **kwargs)
    assert durable.store.get(**reservation_key(first)) is None
    assert durable.store.get(**reservation_key(second))['run_id'] == second['id']
    assert durable.store.create(durable.agent, durable.conversation, durable.user,
                                'next', 'next turn')['id'] != first['id']


def test_reconciler_uses_frozen_run_mode_without_releasing_another_conversation(durable):
    durable.agent['execution_mode'] = 'concurrent'
    with patch('common.runs.time.time', return_value=time.time() - 240):
        run = durable.store.create(durable.agent, durable.conversation, durable.user, 'first', 'hello')
        run = durable.store.claim(run['id'], 'old-owner')
    other, user = conversation(durable)
    newer = durable.store.create(durable.agent, other, user, 'second', 'world')
    with patch('backend.events.run_store', return_value=durable.store):
        events.reconcile({}, None)
    assert durable.store.run(run['id'])['status'] == 'interrupted'
    assert durable.store.get(**reservation_key(run)) is None
    assert durable.store.get(**reservation_key(newer))['run_id'] == newer['id']


def test_legacy_run_without_mode_still_releases_agent_reservation(durable):
    run = durable.store.create(durable.agent, durable.conversation, durable.user, 'first', 'hello')
    run = durable.store.claim(run['id'], 'owner')
    del run['execution_mode']
    durable.store.table.put_item(Item=run)
    durable.store.append(run, {'type': 'interrupted'}, status='interrupted')
    assert durable.store.get('AGENT#' + run['agent_id'], 'LOCK') is None


def test_creation_schema_defaults_to_sequential_and_settings_cannot_mutate_mode():
    fields = {'id': uuid4(), 'team_id': uuid4(), 'name': 'test'}
    assert CreateAgent(**fields).execution_mode == 'sequential'
    assert CreateAgent(**fields, execution_mode='concurrent').execution_mode == 'concurrent'
    for value in ('parallel', '', None, True):
        with pytest.raises(ValidationError):
            CreateAgent(**fields, execution_mode=value)
        with pytest.raises(ValueError):
            execution_mode({'execution_mode': value})
    with pytest.raises(ValidationError):
        AgentSettings(input_limit_value=0, input_limit_unit='tokens', max_output_tokens=0,
                      execution_mode='concurrent')


def test_creation_route_passes_selected_mode_to_provisioning():
    team = str(uuid4())
    payload = CreateAgent(id=uuid4(), team_id=team, name='Parallel', execution_mode='concurrent')
    record = {'id': str(uuid4()), 'name': 'Parallel', 'status': 'ready', 'created_at': 1,
              'team_id': team, 'execution_mode': 'concurrent'}
    svc = SimpleNamespace(provision=Mock(return_value=record))
    with patch('backend.app.services', return_value=svc):
        result = create_agent(payload, {'sub': str(uuid4()), 'team_ids': [team]})
    assert result['execution_mode'] == 'concurrent'
    assert svc.provision.call_args.kwargs['execution_mode'] == 'concurrent'
    del record['execution_mode']
    assert public_agent(record)['execution_mode'] == 'sequential'


@pytest.mark.parametrize('mode', ['sequential', 'concurrent'])
def test_provisioning_persists_mode_in_metadata_and_cognito_and_retries_keep_it(durable, tmp_path, mode):
    fixture_value = uuid4().hex
    svc = object.__new__(Services)
    svc.table = durable.store.metadata
    svc.root = tmp_path
    svc.pool = 'pool'
    svc.secret_prefix = fixture_value
    svc.require_teams = lambda values: values
    svc.secrets = Mock()
    svc.secrets.get_secret_value.return_value = {
        'ARN': f'fixture-{fixture_value}',
        'SecretString': json.dumps({'username': 'test-agent', 'password': uuid4().hex})}
    svc.cognito = Mock()
    svc.cognito.admin_get_user.return_value = {'UserAttributes': [{'Name': 'sub', 'Value': str(uuid4())}]}
    args = (durable.agent['team_id'], durable.user['sub'], str(uuid4()), 'Test')
    agent = svc.provision(*args, execution_mode=mode)
    assert svc.agent(agent['id'])['execution_mode'] == mode
    for call in (svc.cognito.admin_create_user.call_args, svc.cognito.admin_update_user_attributes.call_args):
        attributes = {item['Name']: item['Value'] for item in call.kwargs['UserAttributes']}
        assert attributes['custom:execution_mode'] == mode
    opposite = 'concurrent' if mode == 'sequential' else 'sequential'
    assert svc.provision(*args, execution_mode=opposite)['execution_mode'] == mode
    assert svc.cognito.admin_create_user.call_count == 1


@pytest.mark.parametrize('mode', [None, 'sequential', 'concurrent'])
def test_cognito_trigger_signs_execution_mode_with_legacy_default(mode):
    handler = runpy.run_path(str(Path(__file__).parents[1] / 'infrastructure/lambdas/team-claims/index.py'))['handler']
    attributes = {'custom:teams': str(uuid4())}
    if mode is not None:
        attributes['custom:execution_mode'] = mode
    result = handler({'request': {'userAttributes': attributes,
                                  'groupConfiguration': {'groupsToOverride': ['Agents']}}, 'response': {}}, None)
    for kind in ('idTokenGeneration', 'accessTokenGeneration'):
        assert result['response']['claimsAndScopeOverrideDetails'][kind]['claimsToAddOrOverride']['execution_mode'] == (mode or 'sequential')


def test_runtime_cannot_disable_locks_against_signed_identity(monkeypatch):
    claims = {'sub': str(uuid4()), 'team_id': str(uuid4()), 'security_test_mode': False,
              'input_limit_value': 0, 'input_limit_unit': 'tokens', 'max_output_tokens': 0}
    monkeypatch.setattr(server, 'verifier', SimpleNamespace(verify=lambda _: claims), raising=False)
    payload = server.Invocation(conversation_id=uuid4(), team_id=claims['team_id'],
                                message='hello', execution_mode='concurrent')
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer test')]})
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.invoke(request, payload))
    assert error.value.status_code == 403 and 'execution mode' in error.value.detail


@pytest.mark.parametrize('mode', ['sequential', 'concurrent'])
def test_dispatch_carries_frozen_mode_and_rejects_agent_mode_mismatch(durable, monkeypatch, mode):
    durable.agent['execution_mode'] = mode
    run = durable.store.create(durable.agent, durable.conversation, durable.user, 'first', 'hello')
    svc = SimpleNamespace(
        region='eu-west-1', agent=Mock(return_value=durable.agent),
        conversation=Mock(return_value=durable.conversation), agent_token=Mock(return_value='synthetic'),
        membership=Mock(return_value={'sub': durable.user['sub'], 'enabled': True,
                                     'groups': ['Humans'], 'team_ids': [durable.agent['team_id']]}))
    monkeypatch.setenv('RUNTIME_ARN', 'test-runtime')
    response = Mock()
    response.json.return_value = {'id': run['id']}
    with patch('backend.events.services', return_value=svc), \
            patch('backend.events.run_store', return_value=durable.store), \
            patch('backend.events.httpx.post', return_value=response) as invoke:
        events.dispatch_one(run)
        assert invoke.call_args.kwargs['json'].get('execution_mode', 'sequential') == mode
        durable.agent['execution_mode'] = 'sequential' if mode == 'concurrent' else 'concurrent'
        with pytest.raises(ValueError, match='no longer matches'):
            events.dispatch_one(run)
        assert invoke.call_count == 1


@pytest.mark.parametrize('mode', ['sequential', 'concurrent'])
def test_runtime_skips_agent_efs_lock_only_for_concurrent_mode(tmp_path, monkeypatch, mode):
    sub = str(uuid4())
    (tmp_path / sub).mkdir()
    control = tmp_path / '.control' / sub
    control.mkdir(parents=True)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'busy', False)

    async def events(_payload):
        yield {'type': 'error', 'fatal': False, 'message': 'synthetic input rejection'}

    start = AsyncMock(return_value=(SimpleNamespace(events=events), True))
    monkeypatch.setattr(server, 'get_worker', start)
    payload = server.Invocation(conversation_id=uuid4(), team_id=uuid4(), message='hello', execution_mode=mode)
    fd = os.open(control / 'execution.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        async def consume():
            return [json.loads(wire.removeprefix('data: ')) async for wire in server.execute(sub, payload)]

        with patch('runtime.server.fcntl.flock', wraps=fcntl.flock) as locks:
            output = asyncio.run(consume())
        if mode == 'sequential':
            start.assert_not_called()
            assert locks.call_count == 1
            assert 'Another session' in output[-1]['message']
        else:
            start.assert_awaited_once()
            locks.assert_not_called()
            assert output[0]['type'] == 'status'
    finally:
        os.close(fd)
