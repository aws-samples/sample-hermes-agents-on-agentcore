"""Short-lived stream consumers, reconciler, and AppSync ticket authorizer."""

import json
import logging
import os
import time
from urllib.parse import quote

import boto3
import httpx
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from backend.service import agent_limits, services, token_hash
from common.execution import execution_mode
from common.runs import ACTIVE, LostOwnership, run_store

logger = logging.getLogger(__name__)


def image(record):
    decoder = TypeDeserializer()
    return {key: decoder.deserialize(value)
            for key, value in record['dynamodb']['NewImage'].items()}


def allowed(svc, username, sub, agent_id, conversation_id):
    try:
        membership = svc.membership(username)
    except svc.cognito.exceptions.UserNotFoundException:
        return False
    agent = svc.agent(agent_id)
    if not (agent and membership['sub'] == sub and membership['enabled']
            and 'Humans' in membership['groups'] and agent['team_id'] in membership['team_ids']):
        return False
    conversation = svc.conversation(agent_id, conversation_id) if conversation_id else None
    return bool(conversation and conversation.get('owner_sub') == sub)


def authorize(event, _context):
    token = event.get('authorizationToken', '')
    denied = {'isAuthorized': False, 'ttlOverride': 0}
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return denied
    context = event.get('requestContext', {})
    operation = context.get('operation')
    if operation not in {'EVENT_CONNECT', 'EVENT_SUBSCRIBE'}:
        return denied
    ticket = run_store().get('TICKET#' + token_hash(token))
    if not ticket or int(ticket['expires']) <= time.time():
        return denied
    if operation == 'EVENT_SUBSCRIBE' and context.get('channel') != ticket['channel']:
        return denied
    if not allowed(services(), ticket['username'], ticket['sub'], ticket['agent_id'],
                   ticket.get('conversation_id')):
        return denied
    return {'isAuthorized': True, 'ttlOverride': 0}


def dispatch_one(item):
    store = run_store()
    run = store.run(item['id'])
    if not run or run['status'] != 'pending':
        return
    svc = services()
    if not allowed(svc, run['creator_username'], run['creator_sub'], run['agent_id'], run['conversation_id']):
        try:
            store.append(run, {'type': 'error', 'message': 'Agent access is no longer available'},
                         status='failed')
        except LostOwnership:
            pass  # another dispatcher or reconciler won the conditional transition
        return
    agent = svc.agent(run['agent_id'])
    if execution_mode(run) != execution_mode(agent):
        raise ValueError('Agent execution mode no longer matches its run')
    token = svc.agent_token(agent)
    url = (f'https://bedrock-agentcore.{svc.region}.amazonaws.com/runtimes/'
           f'{quote(os.environ["RUNTIME_ARN"], safe="")}/invocations?qualifier=DEFAULT')
    # Read timeout is for a short acknowledgement, never an hour-long stream.
    response = httpx.post(url, headers={
        'Authorization': 'Bearer ' + token,
        'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': run['runtime_session_id'],
    }, json={'operation': 'start', 'run_id': run['id'], 'conversation_id': run['conversation_id'],
             'team_id': agent['team_id'],
             'security_test_mode': bool(agent.get('security_test_mode', False)),
             **({'execution_mode': 'concurrent'} if execution_mode(run) == 'concurrent' else {}),
             **agent_limits(agent), 'message': run['message']},
        timeout=httpx.Timeout(20, connect=5))
    response.raise_for_status()
    ack = response.json()
    if ack.get('id') != run['id']:
        raise ValueError('Invalid runtime acknowledgement')


def dispatch(event, context):
    failures = []
    for record in event.get('Records', []):
        try:
            item = image(record)
            if record.get('eventName') == 'INSERT' and item.get('kind') == 'run':
                dispatch_one(item)
        except Exception:  # record boundary: retry the same run ID, never replay a claimed run
            logger.exception('Run dispatch failed for stream record %s', record.get('eventID'))
            failures.append({'itemIdentifier': record['dynamodb']['SequenceNumber']})
    return {'batchItemFailures': failures}


def publish_hint(run_id, seq):
    url = f'https://{os.environ["EVENTS_HTTP_DOMAIN"]}/event'
    payload = json.dumps({'channel': '/runs/' + run_id,
                          'events': [json.dumps({'run_id': run_id, 'seq': seq})]})
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(method='POST', url=url, data=payload,
                         headers={'Content-Type': 'application/json'})
    SigV4Auth(credentials, 'appsync', os.environ['AWS_REGION']).add_auth(request)
    response = httpx.post(url, headers=dict(request.headers), content=payload, timeout=5)
    response.raise_for_status()
    result = response.json()
    if result.get('failed') or result.get('errors'):
        raise RuntimeError('AppSync rejected notification')


def publish(event, context):
    groups = {}
    for record in event.get('Records', []):
        if record.get('eventName') != 'INSERT':
            continue
        item = image(record)
        if item.get('kind') == 'event':
            groups.setdefault(item['run_id'], []).append((record, int(item['seq'])))
    failures = []
    for run_id, records in groups.items():
        try:
            if context and context.get_remaining_time_in_millis() < 6000:
                raise TimeoutError('Retry remaining notifications in the next invocation')
            publish_hint(run_id, max(seq for _, seq in records))
        except Exception:
            logger.exception('AppSync notification failed for run %s', run_id)
            failures.extend({'itemIdentifier': record['dynamodb']['SequenceNumber']}
                            for record, _ in records)
    return {'batchItemFailures': failures}


def reconcile(_event, context):
    store = run_store()
    now = int(time.time())
    for status, age in (('pending', 300), ('running', 180)):
        for candidate in store.stale(status, now - age):
            if context and context.get_remaining_time_in_millis() < 5000:
                return  # next minute's sweep continues from the durable index
            run = store.run(candidate['id'])
            if not run or run['status'] not in ACTIVE or int(run['updated_at']) >= now - age:
                continue
            try:
                store.append(run, {'type': 'interrupted',
                                   'message': 'Execution stopped. The last checkpoint is retained.'},
                             status='interrupted')
            except LostOwnership:
                pass  # a heartbeat, completion or another reconciler won
