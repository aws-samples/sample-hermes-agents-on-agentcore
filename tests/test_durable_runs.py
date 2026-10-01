import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import boto3
import pytest
from starlette.requests import Request

from backend import events
from common.runs import Conflict, LostOwnership, RunStore
from runtime import server


def create(durable, key='key', message='hello'):
    return durable.store.create(durable.agent, durable.conversation, durable.user, key, message)


def test_cross_instance_idempotency_and_conflicting_message(durable):
    run = create(durable)
    other = RunStore(boto3.resource('dynamodb', region_name='us-east-1'), 'runs', 'metadata')
    assert other.create(durable.agent, durable.conversation, durable.user, 'key', 'hello') == run
    with pytest.raises(Conflict, match='different message'):
        create(durable, message='different')
    with pytest.raises(Conflict, match='active run'):
        create(durable, key='other')
    assert len(durable.store.metadata.scan()['Items']) == 3  # conversation, user message, client map


def test_claim_and_event_sequence_fencing(durable):
    run = create(durable)
    first = durable.store.claim(run['id'], 'first')
    assert durable.store.claim(run['id'], 'second') is None
    stale = deepcopy(first)
    durable.store.append(first, {'type': 'delta', 'text': 'A'})
    with pytest.raises(LostOwnership):
        durable.store.append(stale, {'type': 'delta', 'text': 'B'})
    stale['owner'] = 'imposter'
    with pytest.raises(LostOwnership):
        durable.store.heartbeat(run['id'], stale['owner'])
    assert durable.store.page(first, 0)['events'] == [
        {'seq': 1, 'data': {'type': 'delta', 'text': 'A'}}]


def test_final_transaction_releases_lock_and_writes_exactly_one_message(durable):
    run = durable.store.claim(create(durable)['id'], 'owner')
    expected = deepcopy(run)
    data = {'type': 'complete', 'text': ''}
    durable.store.append(run, data, status='complete', final_text='answer')
    # Simulates the response being lost after DynamoDB commits.
    assert durable.store.append(expected, data, status='complete', final_text='answer') == 1
    assert durable.store.get('AGENT#' + durable.agent['id'], 'LOCK') is None
    messages = [item for item in durable.store.metadata.scan()['Items'] if item.get('role')]
    assert [item['text'] for item in messages] == ['hello', 'answer']
    assert create(durable)['id'] == run['id']  # retry after completion
    assert create(durable, key='next')['id'] != run['id']


def test_failed_transaction_does_not_publish_event_or_partial_message(durable):
    run = durable.store.claim(create(durable)['id'], 'owner')
    durable.store.table.delete_item(Key={'pk': 'AGENT#' + durable.agent['id'], 'sk': 'LOCK'})
    with pytest.raises(LostOwnership):
        durable.store.append(run, {'type': 'complete'}, status='complete', final_text='answer')
    assert durable.store.page(run, 0)['events'] == []
    assert durable.store.run(run['id'])['status'] == 'running'
    assert not any(item.get('role') == 'assistant'
                   for item in durable.store.metadata.scan()['Items'])


@pytest.mark.parametrize('state', ['not a mapping', {'value': 'x' * 4097}])
def test_invalid_adapter_state_is_rejected_before_any_completion_write(durable, state):
    run = durable.store.claim(create(durable)['id'], 'owner')
    with pytest.raises(ValueError, match='Invalid adapter state'):
        durable.store.append(run, {'type': 'complete'}, status='complete', final_text='answer',
                             adapter_state=state)
    assert durable.store.run(run['id'])['status'] == 'running'
    assert durable.store.page(run, 0)['events'] == []


def test_unsuccessful_runs_cannot_publish_adapter_state(durable):
    run = durable.store.claim(create(durable)['id'], 'owner')
    with pytest.raises(ValueError, match='Only successful turns'):
        durable.store.append(run, {'type': 'interrupted'}, status='interrupted',
                             adapter_state={'cursor': 'uncommitted'})
    assert durable.store.conversation(run).get('adapter_state') is None


def test_stale_sweep_loses_to_heartbeat(durable):
    with patch('common.runs.time.time', return_value=time.time() - 240):
        run = durable.store.claim(create(durable)['id'], 'owner')
    stale = deepcopy(run)
    renewed = durable.store.heartbeat(run['id'], run['owner'])
    with pytest.raises(LostOwnership):
        durable.store.append(stale, {'type': 'interrupted'}, status='interrupted')
    assert durable.store.run(run['id'])['updated_at'] == renewed['updated_at']


def test_reconciler_marks_lost_runtime_interrupted_and_fences_it(durable):
    with patch('common.runs.time.time', return_value=time.time() - 240):
        run = durable.store.claim(create(durable)['id'], 'owner')
    with patch('backend.events.run_store', return_value=durable.store):
        events.reconcile({}, None)
    assert durable.store.run(run['id'])['status'] == 'interrupted'
    with pytest.raises(LostOwnership):
        durable.store.heartbeat(run['id'], run['owner'])
    assert durable.store.get('AGENT#' + durable.agent['id'], 'LOCK') is None


def test_paginated_replay_and_invalid_cursor(durable):
    run = durable.store.claim(create(durable)['id'], 'owner')
    for i in range(102):
        durable.store.append(run, {'type': 'delta', 'text': str(i)})
    first = durable.store.page(run, 0)
    assert first['has_more'] and len(first['events']) == 100
    second = durable.store.page(run, first['events'][-1]['seq'])
    assert [item['seq'] for item in second['events']] == [101, 102]
    assert not second['has_more']
    with pytest.raises(ValueError):
        durable.store.page(run, 103)


def test_command_returns_before_producer_finishes_and_duplicates_do_not_execute(durable, monkeypatch):
    run = create(durable)
    claims = {'sub': durable.agent['sub'], 'team_id': durable.agent['team_id'],
              'security_test_mode': False, 'input_limit_value': 0,
              'input_limit_unit': 'tokens', 'max_output_tokens': 0}
    monkeypatch.setattr(server, 'verifier', SimpleNamespace(verify=lambda _: claims), raising=False)
    monkeypatch.setattr(server, 'bound_subject', None)
    monkeypatch.setattr(server, 'bound_conversation', None)
    monkeypatch.setattr(server, 'busy', False)
    monkeypatch.setattr(server, 'run_tasks', set())
    monkeypatch.setattr(server, 'run_store', lambda: durable.store)
    payload = server.Invocation(operation='start', run_id=run['id'],
                                conversation_id=run['conversation_id'],
                                team_id=claims['team_id'], message='hello')
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer test')]})

    async def scenario():
        release = asyncio.Event()
        calls = []

        async def execute(*args, **kwargs):
            calls.append(True)
            yield server.event({'type': 'delta', 'text': 'working'})
            await release.wait()
            await kwargs['finalize']({'type': 'complete', 'text': 'answer'}, None)
            yield server.event({'type': 'complete', 'text': 'answer'})

        monkeypatch.setattr(server, 'execute', execute)
        ack = await server.invoke(request, payload)
        assert ack['id'] == run['id'] and ack['status'] == 'running'
        tasks = list(server.run_tasks)
        assert tasks and not tasks[0].done()
        assert (await server.invoke(request, payload))['id'] == run['id']
        release.set()
        await asyncio.gather(*tasks)
        assert calls == [True]
        finished = durable.store.run(run['id'])
        assert finished['status'] == 'complete'
        assert durable.store.page(finished, 0)['events'][-1]['data']['type'] == 'complete'

    asyncio.run(scenario())


def test_publisher_retries_failed_records_and_only_emits_hints():
    from boto3.dynamodb.types import TypeSerializer
    serializer = TypeSerializer()
    records = [{'eventName': 'INSERT', 'dynamodb': {
        'SequenceNumber': str(i), 'NewImage': {key: serializer.serialize(value) for key, value in {
            'kind': 'event', 'run_id': 'run', 'seq': i, 'payload': 'secret text'}.items()}}}
               for i in (1, 2)]
    with patch('backend.events.publish_hint') as publish:
        assert events.publish({'Records': records}, None) == {'batchItemFailures': []}
        publish.assert_called_once_with('run', 2)
    with patch('backend.events.publish_hint', side_effect=RuntimeError):
        assert len(events.publish({'Records': records}, None)['batchItemFailures']) == 2


def test_ticket_scopes_authorization_and_checks_current_membership(durable):
    from backend.service import token_hash
    token = 't' * 64
    durable.store.table.put_item(Item={
        'pk': 'TICKET#' + token_hash(token), 'sk': 'META', 'expires': int(time.time()) + 60,
        'channel': '/runs/run', 'username': 'alice', 'sub': durable.user['sub'],
        'conversation_id': durable.conversation['id'],
        'agent_id': durable.agent['id']})
    membership = {'sub': durable.user['sub'], 'enabled': True,
                  'groups': ['Humans'], 'team_ids': [durable.agent['team_id']]}
    svc = SimpleNamespace(membership=Mock(return_value=membership),
                          conversation=Mock(return_value=durable.conversation),
                          agent=Mock(return_value=durable.agent))
    with patch('backend.events.run_store', return_value=durable.store), \
            patch('backend.events.services', return_value=svc):
        for operation, channel, expected in [('EVENT_CONNECT', None, True),
                                             ('EVENT_SUBSCRIBE', '/runs/run', True),
                                             ('EVENT_SUBSCRIBE', '/runs/*', False),
                                             ('EVENT_SUBSCRIBE', '/runs/other', False),
                                             ('EVENT_PUBLISH', '/runs/run', False)]:
            event = {'authorizationToken': token,
                     'requestContext': {'operation': operation, 'channel': channel}}
            assert events.authorize(event, None)['isAuthorized'] is expected
        membership['team_ids'] = []
        assert not events.authorize({'authorizationToken': token,
                                     'requestContext': {'operation': 'EVENT_CONNECT'}}, None)['isAuthorized']
