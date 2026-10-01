from copy import deepcopy
from unittest.mock import patch

import pytest
from botocore.exceptions import ReadTimeoutError

from common.runs import LostOwnership


def claimed_run(durable):
    run = durable.store.create(durable.agent, durable.conversation, durable.user, 'key', 'hello')
    return durable.store.claim(run['id'], 'owner')


def cancellation(store, *codes):
    return store.client.exceptions.TransactionCanceledException({
        'Error': {'Code': 'TransactionCanceledException', 'Message': 'Injected cancellation'},
        'CancellationReasons': [{'Code': code} for code in codes],
    }, 'TransactWriteItems')


@pytest.mark.parametrize('terminal', [False, True])
def test_temporary_conflicts_retry_identical_fenced_transaction(durable, terminal):
    store = durable.store
    run = claimed_run(durable)
    transact = store.client.transact_write_items
    transactions = []

    def contend(**kwargs):
        transactions.append(deepcopy(kwargs['TransactItems']))
        if len(transactions) <= 2:
            raise cancellation(store, 'None', 'TransactionConflict')
        return transact(**kwargs)

    options = ({'status': 'complete', 'final_text': 'answer',
                'adapter_state': {'cursor': 'turn-1'}}
               if terminal else {})
    data = {'type': 'complete'} if terminal else {'type': 'delta', 'text': 'working'}
    with patch.object(store.client, 'transact_write_items', side_effect=contend), \
            patch.object(store.table, 'get_item', wraps=store.table.get_item) as reads, \
            patch('common.runs.random.uniform', side_effect=[0.05, 0.1]) as jitter, \
            patch('common.runs.time.sleep') as sleep:
        assert store.append(run, data, **options) == 1
        assert all(call.kwargs['ConsistentRead'] for call in reads.call_args_list)
    assert len(transactions) == 3 and transactions[0] == transactions[1] == transactions[2]
    assert [call.args for call in jitter.call_args_list] == [(0, 0.1), (0, 0.2)]
    assert [call.args for call in sleep.call_args_list] == [(0.05,), (0.1,)]
    assert store.page(run, 0)['events'] == [{'seq': 1, 'data': data}]
    if terminal:
        assert store.conversation(run)['adapter_state'] == options['adapter_state']
        assert store.get('AGENT#' + run['agent_id'], 'LOCK') is None
        messages = [item for item in store.metadata.scan()['Items'] if item.get('role') == 'assistant']
        assert len(messages) == 1 and messages[0]['text'] == 'answer'


def test_reconciler_winning_during_backoff_still_fences_retry(durable):
    store = durable.store
    run = claimed_run(durable)
    transact = store.client.transact_write_items
    transactions = []

    def contend(**kwargs):
        transactions.append(deepcopy(kwargs['TransactItems']))
        if len(transactions) == 1:
            raise cancellation(store, 'None', 'TransactionConflict')
        return transact(**kwargs)

    def reconcile(_delay):
        # The strongly consistent retry-decision read already happened. Reconciliation now
        # wins before the second transaction reaches DynamoDB.
        with patch.object(store.client, 'transact_write_items', side_effect=transact):
            store.append(deepcopy(run), {'type': 'interrupted'}, status='interrupted')

    with patch.object(store.client, 'transact_write_items', side_effect=contend), \
            patch('common.runs.time.sleep', side_effect=reconcile) as sleep, \
            pytest.raises(LostOwnership):
        store.append(run, {'type': 'complete'}, status='complete', final_text='answer',
                     adapter_state={'cursor': 'turn-1'})

    assert len(transactions) == 2 and transactions[0] == transactions[1]
    assert sleep.call_count == 1
    saved = store.run(run['id'])
    assert saved['status'] == 'interrupted'
    assert store.page(saved, 0)['events'] == [{'seq': 1, 'data': {'type': 'interrupted'}}]
    assert store.conversation(run).get('adapter_state') is None
    assert not any(item.get('role') == 'assistant' for item in store.metadata.scan()['Items'])


@pytest.mark.parametrize('field,value', [('owner', 'another-owner'), ('status', 'interrupted')])
def test_changed_owner_or_status_after_conflict_stops_without_retry(durable, field, value):
    store = durable.store
    run = claimed_run(durable)

    def contend(**_kwargs):
        store.table.put_item(Item={**run, field: value})
        raise cancellation(store, 'None', 'TransactionConflict')

    with patch.object(store.client, 'transact_write_items', side_effect=contend) as writes, \
            patch('common.runs.time.sleep') as sleep, pytest.raises(LostOwnership):
        store.append(run, {'type': 'delta', 'text': 'no longer ours'})
    assert writes.call_count == 1
    sleep.assert_not_called()
    assert store.run(run['id'])[field] == value
    assert store.get(run['pk'], 'EVENT#00000000000000000001') is None


def test_conflict_retries_are_bounded_and_exhaustion_preserves_actual_error(durable):
    store = durable.store
    run = claimed_run(durable)
    previous = deepcopy(run)
    error = cancellation(store, 'None', 'TransactionConflict')
    with patch.object(store.client, 'transact_write_items', side_effect=error) as writes, \
            patch('common.runs.time.sleep') as sleep, \
            pytest.raises(store.client.exceptions.TransactionCanceledException) as raised:
        store.append(run, {'type': 'delta', 'text': 'working'})
    assert raised.value is error
    assert writes.call_count == 4 and sleep.call_count == 3
    assert run == previous and store.run(run['id']) == previous
    assert store.page(run, 0)['events'] == []


@pytest.mark.parametrize('codes', [
    (), ('None',), ('ConditionalCheckFailed',), ('ValidationError',),
    ('ProvisionedThroughputExceeded',), ('ThrottlingError',),
    ('TransactionConflict', 'ValidationError'), ('TransactionConflict', 'ConditionalCheckFailed'),
    ('TransactionConflict', None),
])
def test_other_cancellations_are_not_retried_or_misreported_as_ownership_loss(durable, codes):
    store = durable.store
    run = claimed_run(durable)
    error = cancellation(store, *codes)
    with patch.object(store.client, 'transact_write_items', side_effect=error) as writes, \
            patch('common.runs.time.sleep') as sleep, \
            pytest.raises(store.client.exceptions.TransactionCanceledException) as raised:
        store.append(run, {'type': 'delta', 'text': 'working'})
    assert raised.value is error and writes.call_count == 1
    sleep.assert_not_called()


def test_exhausted_transport_timeout_behavior_is_unchanged(durable):
    store = durable.store
    run = claimed_run(durable)
    transact = store.client.transact_write_items
    error = ReadTimeoutError(endpoint_url='https://dynamodb.example')

    def commit_without_response(**kwargs):
        transact(**kwargs)
        raise error

    with patch.object(store.client, 'transact_write_items', side_effect=commit_without_response) as writes, \
            patch.object(store, 'get', wraps=store.get) as reads, \
            patch('common.runs.time.sleep') as sleep, pytest.raises(ReadTimeoutError) as raised:
        store.append(run, {'type': 'delta', 'text': 'committed'})
    assert raised.value is error and writes.call_count == 1
    reads.assert_not_called()
    sleep.assert_not_called()
    assert run['last_event_id'] == 0  # accepted 4A risk: the caller has not confirmed this write
    assert store.run(run['id'])['last_event_id'] == 1
