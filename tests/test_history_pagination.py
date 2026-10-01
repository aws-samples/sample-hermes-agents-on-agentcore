import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend import app as module
from backend.service import HISTORY_PAGE_BYTES, Services


@pytest.fixture
def history(durable):
    svc = object.__new__(Services)
    svc.table = durable.store.metadata
    agent_id, conversation_id = durable.agent['id'], durable.conversation['id']
    pk = f'MESSAGES#{agent_id}#{conversation_id}'

    def put(index, text, *, partition=pk):
        svc.table.put_item(Item={
            'pk': partition, 'sk': f'MSG#{index:020d}#0', 'role': 'user',
            'text': text, 'run_id': f'run-{index}',
        })

    return SimpleNamespace(svc=svc, agent_id=agent_id, conversation_id=conversation_id, put=put)


def read_pages(history):
    cursor = None
    messages = []
    seen = set()
    while True:
        page = history.svc.history_page(history.agent_id, history.conversation_id, cursor)
        encoded = json.dumps(page, ensure_ascii=True).encode()
        assert len(encoded) <= HISTORY_PAGE_BYTES
        # Conservatively account for Lambda serializing the JSON response body again.
        envelope = json.dumps({'statusCode': 200, 'body': encoded.decode(),
                               'isBase64Encoded': False, 'headers': {}}, ensure_ascii=True).encode()
        assert len(envelope) < 6 * 1024 * 1024
        messages.extend(page['messages'])
        cursor = page['next_cursor']
        if cursor is None:
            return messages, len(seen) + 1
        assert cursor not in seen
        seen.add(cursor)


def test_history_larger_than_lambda_limit_is_complete_across_bounded_pages(history):
    count = 30
    texts = [f'{index}:'.ljust(250_000, 'x') for index in range(count)]
    for index, text in enumerate(texts):
        history.put(index, text)
    with patch.object(history.svc.table, 'query', wraps=history.svc.table.query) as query:
        messages, pages = read_pages(history)
    assert len(json.dumps(messages).encode()) > 6 * 1024 * 1024
    assert [item['text'] for item in messages] == texts
    assert pages > 1
    assert all(call.kwargs['ConsistentRead'] for call in query.call_args_list)


def test_json_escaping_is_counted_and_byte_cutoff_does_not_skip_items(history):
    texts = ['\0' * 300_000, '\x01' * 300_000, 'final message']
    for index, text in enumerate(texts):
        history.put(index, text)
    first = history.svc.history_page(history.agent_id, history.conversation_id)
    assert len(first['messages']) == 1 and first['next_cursor']
    messages, pages = read_pages(history)
    assert pages == 2
    assert [item['text'] for item in messages] == texts


def test_count_limit_and_unicode_history_remain_ordered(history):
    for index in range(201):
        history.put(index, f'{index} 漢字 🌍')
    first = history.svc.history_page(history.agent_id, history.conversation_id)
    assert len(first['messages']) == 100
    messages, pages = read_pages(history)
    assert len(messages) == 201 and pages == 3
    assert [item['run_id'] for item in messages] == [f'run-{index}' for index in range(201)]


def test_empty_history_returns_explicit_terminal_cursor(history):
    assert history.svc.history_page(history.agent_id, history.conversation_id) == {
        'messages': [], 'next_cursor': None}


def test_cursor_is_scoped_to_authorized_conversation(history):
    for index in range(101):
        history.put(index, str(index))
    cursor = history.svc.history_page(history.agent_id, history.conversation_id)['next_cursor']
    with pytest.raises(ValueError, match='Invalid history cursor'):
        history.svc.history_page(str(uuid4()), history.conversation_id, cursor)
    with pytest.raises(ValueError, match='Invalid history cursor'):
        history.svc.history_page(history.agent_id, str(uuid4()), cursor)


@pytest.mark.parametrize('cursor', ['', 'not base64!', 'x' * 1025,
                                   base64.urlsafe_b64encode(b'[]').decode(),
                                   base64.urlsafe_b64encode(b'{broken').decode()])
def test_invalid_cursors_are_rejected(history, cursor):
    with pytest.raises(ValueError, match='Invalid history cursor'):
        history.svc.history_page(history.agent_id, history.conversation_id, cursor)


def test_history_route_checks_authorization_on_each_page(history, durable):
    for index in range(101):
        history.put(index, str(index))
    svc = SimpleNamespace(history_page=history.svc.history_page,
                          agent=Mock(return_value=durable.agent),
                          conversation=Mock(return_value=durable.conversation))
    user = {**durable.user, 'team_ids': [durable.agent['team_id']]}
    module.app.dependency_overrides[module.current_user] = lambda: user
    try:
        with patch('backend.app.services', return_value=svc):
            client = TestClient(module.app)
            url = f'/api/agents/{history.agent_id}/conversations/{history.conversation_id}/messages'
            first = client.get(url)
            assert first.status_code == 200 and first.json()['next_cursor']
            user['team_ids'] = []
            assert client.get(url, params={'cursor': first.json()['next_cursor']}).status_code == 404
    finally:
        module.app.dependency_overrides.clear()
