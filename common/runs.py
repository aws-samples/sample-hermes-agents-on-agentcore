"""Durable run coordination shared by command Lambdas and the trusted supervisor.

No process-local ownership. All side-effecting transitions use compare-and-swap transactions.
The table stream is the transactional outbox for dispatch and notification delivery.
"""

import hashlib
import json
import os
import random
import time
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid4, uuid5

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

from common.execution import execution_mode

ACTIVE = {'pending', 'running'}
TERMINAL = {'complete', 'partial', 'failed', 'interrupted'}
RETENTION = 7 * 86400
TRANSACTION_CONFLICT_RETRIES = 3


class Conflict(Exception):
    pass


class LostOwnership(Exception):
    pass


def public_run(run):
    return {key: run[key] for key in ('id', 'status', 'last_event_id')}


def reservation_key(run):
    if execution_mode(run) == 'concurrent':
        # Parallel conversations are allowed, but turns within one conversation remain ordered.
        return {'pk': f"CONVERSATION#{run['agent_id']}#{run['conversation_id']}", 'sk': 'LOCK'}
    return {'pk': 'AGENT#' + run['agent_id'], 'sk': 'LOCK'}


class RunStore:
    def __init__(self, resource, run_table, metadata_table):
        self.table = resource.Table(run_table)
        self.metadata = resource.Table(metadata_table)
        self.client = resource.meta.client

    def get(self, pk, sk='META'):
        return self.table.get_item(Key={'pk': pk, 'sk': sk}, ConsistentRead=True).get('Item')

    def run(self, run_id):
        return self.get('RUN#' + run_id)

    def conversation(self, run):
        conversation = self.metadata.get_item(
            Key={'pk': 'AGENT#' + run['agent_id'], 'sk': 'CONV#' + run['conversation_id']},
            ConsistentRead=True).get('Item')
        if not conversation or conversation.get('owner_sub') != run['creator_sub']:
            raise ValueError('Conversation no longer exists')
        return conversation

    def create(self, agent, conversation, user, key, message):
        if conversation.get('owner_sub') != user['sub']:
            raise Conflict('Conversation is not owned by this user')
        now = int(time.time())
        idempotency_pk = 'IDEMP#' + str(uuid5(NAMESPACE_URL, json.dumps([
            agent['id'], conversation['id'], user['sub'], key])))
        run_id = str(uuid4())
        digest = hashlib.sha256(message.encode()).hexdigest()

        def existing():
            previous = self.get(idempotency_pk)
            if previous and int(previous['expires']) > now:
                if previous['message_hash'] != digest:
                    raise Conflict('Idempotency-Key was already used for a different message')
                saved = self.run(previous['run_id'])
                if not saved:
                    raise Conflict('Run retention expired; use a new Idempotency-Key')
                return saved
            return None

        previous = existing()
        if previous:
            return previous
        run = {
            'pk': 'RUN#' + run_id, 'sk': 'META', 'kind': 'run', 'id': run_id,
            'agent_id': agent['id'], 'agent_sub': agent['sub'],
            'execution_mode': execution_mode(agent),
            'conversation_id': conversation['id'],
            'runtime_session_id': conversation['runtime_session_id'],
            'creator_sub': user['sub'], 'creator_username': user['username'],
            'message': message, 'message_hash': digest, 'status': 'pending',
            'last_event_id': 0, 'created_at': now, 'updated_at': now,
            'sequence': f'{time.time_ns():020d}', 'expires': now + RETENTION,
        }
        try:
            self.client.transact_write_items(TransactItems=[
                {'Put': {'TableName': self.table.name, 'Item': run,
                         'ConditionExpression': 'attribute_not_exists(pk)'}},
                {'Put': {'TableName': self.table.name, 'Item': {
                    'pk': idempotency_pk, 'sk': 'META', 'run_id': run_id,
                    'message_hash': digest, 'expires': now + RETENTION},
                         'ConditionExpression': 'attribute_not_exists(pk) OR expires <= :now',
                         'ExpressionAttributeValues': {':now': now}}},
                {'Put': {'TableName': self.table.name,
                         'Item': {**reservation_key(run), 'run_id': run_id},
                         'ConditionExpression': 'attribute_not_exists(pk)'}},
                {'Update': {'TableName': self.metadata.name,
                            'Key': {'pk': 'AGENT#' + agent['id'], 'sk': 'CONV#' + conversation['id']},
                            'ConditionExpression': 'owner_sub = :owner',
                            'UpdateExpression': 'SET latest_run_id = :id',
                            'ExpressionAttributeValues': {':id': run_id, ':owner': user['sub']}}},
                {'Put': {'TableName': self.metadata.name, 'Item': {
                    'pk': f"MESSAGES#{agent['id']}#{conversation['id']}",
                    'sk': f"MSG#{run['sequence']}#0", 'role': 'user', 'text': message,
                    'run_id': run_id}}},
                {'Put': {'TableName': self.metadata.name, 'Item': {
                    'pk': 'CLIENT#' + user['sub'], 'sk': 'SESSION#' + conversation['id'],
                    'agent_id': agent['id'], 'conversation_id': conversation['id'],
                    'latest_run_id': run_id}}},
            ])
        except self.client.exceptions.TransactionCanceledException:
            previous = existing()
            if previous:
                return previous
            reservation = reservation_key(run)
            if self.get(reservation['pk'], reservation['sk']):
                scope = 'conversation' if execution_mode(run) == 'concurrent' else 'agent'
                raise Conflict(f'This {scope} already has an active run') from None
            raise
        return run

    def claim(self, run_id, owner):
        now = int(time.time())
        try:
            return self.table.update_item(
                Key={'pk': 'RUN#' + run_id, 'sk': 'META'},
                ConditionExpression='#s = :pending AND expires > :now',
                UpdateExpression='SET #s = :running, #o = :owner, updated_at = :now, deadline = :end',
                ExpressionAttributeNames={'#s': 'status', '#o': 'owner'},
                ExpressionAttributeValues={':pending': 'pending', ':running': 'running',
                                           ':owner': owner, ':now': now, ':end': now + 3720},
                ReturnValues='ALL_NEW')['Attributes']
        except self.client.exceptions.ConditionalCheckFailedException:
            return None

    def heartbeat(self, run_id, owner):
        now = int(time.time())
        try:
            return self.table.update_item(
                Key={'pk': 'RUN#' + run_id, 'sk': 'META'},
                ConditionExpression='#s = :running AND #o = :owner AND deadline > :now',
                UpdateExpression='SET updated_at = :now',
                ExpressionAttributeNames={'#s': 'status', '#o': 'owner'},
                ExpressionAttributeValues={':running': 'running', ':owner': owner, ':now': now},
                ReturnValues='ALL_NEW')['Attributes']
        except self.client.exceptions.ConditionalCheckFailedException:
            raise LostOwnership(run_id) from None

    def append(self, run, data, *, status=None, final_text=None, adapter_state=None):
        """Atomic event + cursor; terminal event also materializes the message and releases lock.

        `run` is the expected state (including owner and cursor), updated only on success.
        Explicit transaction conflicts are retried only while the expected fencing state
        remains unchanged. Transport-timeout recovery is intentionally not added here.
        """
        seq = int(run['last_event_id']) + 1
        if status is not None and status not in TERMINAL:
            raise ValueError('Invalid terminal state')
        payload = json.dumps(data, ensure_ascii=False)
        if len(payload.encode()) > 200_000:
            raise ValueError('Event exceeds storage limit')
        if final_text is not None and len(final_text.encode()) > 300_000:
            raise ValueError('Final response exceeds storage limit')
        if status in {'complete', 'partial'}:
            if final_text is None:
                raise ValueError('Completion requires final text')
        elif adapter_state is not None:
            raise ValueError('Only successful turns can commit adapter state')
        if adapter_state is not None and (
                not isinstance(adapter_state, dict) or len(json.dumps(adapter_state).encode()) > 4096):
            raise ValueError('Invalid adapter state')
        event = {
            'pk': run['pk'], 'sk': f'EVENT#{seq:020d}', 'kind': 'event',
            'run_id': run['id'], 'seq': seq, 'payload': payload,
            'expires': run['expires'],
        }
        now = int(time.time())
        updated = {**run, 'last_event_id': seq, 'updated_at': now}
        if status:
            updated.update(status=status, ended_at=now)
        if status in {'complete', 'partial'}:
            updated['adapter_state'] = adapter_state
        names = {'#s': 'status'}
        values = {':s': run['status'], ':seq': run['last_event_id'], ':time': run['updated_at']}
        condition = '#s = :s AND last_event_id = :seq AND updated_at = :time'
        if 'owner' in run:
            condition += ' AND #o = :owner'
            names['#o'] = 'owner'
            values[':owner'] = run['owner']
        transactions = [
            {'Put': {'TableName': self.table.name, 'Item': event,
                     'ConditionExpression': 'attribute_not_exists(pk)'}},
            {'Put': {'TableName': self.table.name, 'Item': updated,
                     'ConditionExpression': condition, 'ExpressionAttributeNames': names,
                     'ExpressionAttributeValues': values}},
        ]
        if status:
            transactions.append({'Delete': {
                'TableName': self.table.name, 'Key': reservation_key(run),
                'ConditionExpression': 'run_id = :id',
                'ExpressionAttributeValues': {':id': run['id']}}})
        if final_text is not None and status in {'complete', 'partial'}:
            transactions.append({'Put': {'TableName': self.metadata.name, 'Item': {
                'pk': f"MESSAGES#{run['agent_id']}#{run['conversation_id']}",
                'sk': f"MSG#{run['sequence']}#1", 'role': 'assistant', 'text': final_text,
                'run_id': run['id'], 'partial': status == 'partial',
                'exit_reason': str(data.get('reason', ''))[:200]},
                'ConditionExpression': 'attribute_not_exists(pk)'}})
            update = 'SET adapter_state = :state'
            state_values = {':state': adapter_state, ':human': run['creator_sub']}
            if data.get('worker_instance_id'):
                update += ', last_worker_instance_id = :worker'
                state_values[':worker'] = str(data['worker_instance_id'])
            transactions.append({'Update': {
                'TableName': self.metadata.name,
                'Key': {'pk': 'AGENT#' + run['agent_id'], 'sk': 'CONV#' + run['conversation_id']},
                'UpdateExpression': update,
                'ConditionExpression': 'owner_sub = :human',
                'ExpressionAttributeValues': state_values}})
        for attempt in range(TRANSACTION_CONFLICT_RETRIES + 1):
            try:
                # Preserve the original conditional transaction on every retry; the read
                # below is only a retry decision, never permission to bypass a fence.
                self.client.transact_write_items(TransactItems=transactions)
                break
            except self.client.exceptions.TransactionCanceledException as error:
                saved = self.get(event['pk'], event['sk'])
                current = self.run(run['id'])
                if (saved and saved['payload'] == payload and current
                        and current.get('owner') == run.get('owner')
                        and int(current['last_event_id']) == seq
                        and current.get('adapter_state') == updated.get('adapter_state')):
                    run.update(current)
                    return seq
                if not current or any(current.get(field) != run.get(field) for field in (
                        'owner', 'status', 'last_event_id', 'updated_at')):
                    raise LostOwnership(run['id']) from None
                if status:
                    key = reservation_key(run)
                    reservation = self.get(key['pk'], key['sk'])
                    if not reservation or reservation.get('run_id') != run['id']:
                        raise LostOwnership(run['id']) from None
                reasons = {reason.get('Code') for reason in error.response.get('CancellationReasons', [])}
                reasons.discard('None')
                # Unknown, mixed, validation and conditional failures are not evidence of
                # temporary contention. Preserve the actual service error in those cases.
                if reasons != {'TransactionConflict'} or attempt == TRANSACTION_CONFLICT_RETRIES:
                    raise
                time.sleep(random.uniform(0, 0.1 * (2 ** attempt)))
        run.update(updated)
        return seq

    def page(self, run, after):
        if after < 0 or after > int(run['last_event_id']):
            raise ValueError('Invalid event cursor')
        if after == int(run['last_event_id']):
            return {'events': [], 'last_event_id': after, 'status': run['status'], 'has_more': False}
        result = self.table.query(
            KeyConditionExpression=Key('pk').eq(run['pk']) & Key('sk').between(
                f'EVENT#{after + 1:020d}', f"EVENT#{int(run['last_event_id']):020d}"),
            ConsistentRead=True, Limit=100)
        events = [{'seq': int(item['seq']), 'data': json.loads(item['payload'])}
                  for item in result.get('Items', [])]
        if not events or any(item['seq'] != after + index + 1 for index, item in enumerate(events)):
            raise LookupError('Replay events expired; reload conversation history')
        return {'events': events, 'last_event_id': int(run['last_event_id']),
                'status': run['status'], 'has_more': bool(result.get('LastEvaluatedKey'))}

    def stale(self, status, cutoff):
        paginator = self.client.get_paginator('query')
        for page in paginator.paginate(
                TableName=self.table.name, IndexName='WorkByStatus',
                KeyConditionExpression=Key('status').eq(status) & Key('updated_at').lt(cutoff)):
            yield from page.get('Items', [])


@lru_cache
def run_store():
    resource = boto3.resource('dynamodb', config=Config(
        retries={'mode': 'standard', 'total_max_attempts': 3}, connect_timeout=3, read_timeout=5))
    return RunStore(resource, os.environ['RUN_TABLE_NAME'], os.environ['TABLE_NAME'])
