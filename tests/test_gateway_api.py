import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from backend import app as module


@pytest.fixture
def gateway(durable, monkeypatch):
    svc = SimpleNamespace(
        agent=Mock(return_value=durable.agent),
        conversation=Mock(return_value=durable.conversation),
    )
    user = {**durable.user, 'team_ids': [durable.agent['team_id']]}
    module.app.dependency_overrides[module.current_user] = lambda: user
    monkeypatch.setattr(module, 'services', lambda: svc)
    monkeypatch.setattr(module, 'run_store', lambda: durable.store)
    monkeypatch.setenv('PUBLIC_URL', 'https://portal.example')
    monkeypatch.setenv('EVENTS_HTTP_DOMAIN', 'events.example')
    monkeypatch.setenv('EVENTS_REALTIME_DOMAIN', 'realtime.example')
    prefix = f"/api/agents/{durable.agent['id']}/conversations/{durable.conversation['id']}"
    yield SimpleNamespace(client=TestClient(module.app), prefix=prefix, durable=durable,
                          svc=svc, user=user, headers={'Origin': 'https://portal.example'})
    module.app.dependency_overrides.clear()


def test_commands_retry_and_cross_run_path_denial(gateway):
    g = gateway
    headers = {**g.headers, 'Idempotency-Key': 'same-message'}
    first = g.client.post(g.prefix + '/runs', json={'message': 'hello'}, headers=headers)
    assert first.status_code == 200
    second = g.client.post(g.prefix + '/runs', json={'message': 'hello'}, headers=headers)
    assert second.json() == first.json()
    mismatch = g.client.post(g.prefix + '/runs', json={'message': 'different'}, headers=headers)
    assert mismatch.status_code == 409
    url = g.prefix + '/runs/' + first.json()['id']
    assert g.client.get(url + '/events').json()['events'] == []
    assert g.client.get(url + '/events?after=100').status_code == 400
    assert g.client.get(url.replace(g.durable.conversation['id'], str(uuid4()))).status_code == 404
    g.user['team_ids'] = []
    assert g.client.get(url).status_code == 404


def test_subscription_ticket_and_replay_expiry(gateway):
    g = gateway
    response = g.client.post(g.prefix + '/runs', json={'message': 'hi'},
                             headers={**g.headers, 'Idempotency-Key': 'key'})
    url = g.prefix + '/runs/' + response.json()['id']
    ticket = g.client.post(url + '/subscription', headers=g.headers).json()
    assert ticket['channel'] == '/runs/' + response.json()['id']
    assert len(ticket['token']) == 64
    run = g.durable.store.run(response.json()['id'])
    run['expires'] = 1
    g.durable.store.table.put_item(Item=run)
    assert g.client.get(url + '/events').status_code == 410


def test_native_bearer_origin_rules_do_not_allow_cookie_csrf(gateway):
    g = gateway
    url = g.prefix + '/runs'
    # Authentication itself is tested separately; middleware permits a cookie-free native client.
    headers = {'Authorization': 'Bearer native', 'Idempotency-Key': 'key'}
    assert g.client.post(url, json={'message': 'native'}, headers=headers).status_code == 200
    g.client.cookies.set(module.COOKIE, 'cookie')
    assert g.client.post(url, json={'message': 'csrf'}, headers=headers).status_code == 403
    assert g.client.post(url, json={'message': 'csrf'}, headers={
        **headers, 'Origin': 'https://attacker.example'}).status_code == 403


def test_lambda_http_v2_preserves_cookie_and_json_contract(gateway, monkeypatch):
    from backend import lambda_handler
    from backend.lambda_handler import handler
    g = gateway
    monkeypatch.setattr(lambda_handler, 'origin_secret', lambda: 'cloudfront-secret')
    request = {
        'version': '2.0', 'routeKey': 'ANY /api/{proxy+}', 'rawPath': g.prefix + '/runs',
        'rawQueryString': '', 'headers': {'host': 'portal.example', 'origin': 'https://portal.example',
                                       'content-type': 'application/json', 'idempotency-key': 'native',
                                       'x-origin-verify': 'cloudfront-secret'},
        'requestContext': {'http': {'method': 'POST', 'path': g.prefix + '/runs',
                                    'sourceIp': '127.0.0.1', 'protocol': 'HTTP/1.1'}},
        'body': json.dumps({'message': 'lambda'}), 'isBase64Encoded': False,
    }
    # Other tests use asyncio.run(), which clears the thread's default loop. Mangum's
    # synchronous adapter needs its own explicit loop, independent of test execution order.
    with asyncio.Runner() as runner:
        runner.get_loop()
        response = handler(request, SimpleNamespace())
    assert response['statusCode'] == 200
    body = response['body']
    if response.get('isBase64Encoded'):
        body = base64.b64decode(body)
    assert json.loads(body)['status'] == 'pending'


def test_lambda_rejects_requests_that_bypass_cloudfront(monkeypatch):
    from backend import lambda_handler
    app_calls = []
    monkeypatch.setattr(lambda_handler, '_app', lambda event, context: app_calls.append(event) or {
        'statusCode': 200})
    monkeypatch.setattr(lambda_handler, 'origin_secret', lambda: 'cloudfront-secret')
    for headers in ({}, {'x-origin-verify': ''}, {'x-origin-verify': 'wrong'},
                    {'x-origin-verify': 'cloudfront-secret-suffix'}):
        response = lambda_handler.handler({'headers': headers}, SimpleNamespace())
        assert response['statusCode'] == 403
    assert lambda_handler.handler({'headers': None}, SimpleNamespace())['statusCode'] == 403
    assert app_calls == []
    response = lambda_handler.handler({'headers': {'X-Origin-Verify': 'cloudfront-secret', 'a': 'b'}},
                                      SimpleNamespace())
    assert response['statusCode'] == 200
    assert app_calls == [{'headers': {'a': 'b'}}]


def test_origin_secret_fails_closed_without_configuration(monkeypatch):
    from backend import lambda_handler
    lambda_handler.origin_secret.cache_clear()
    monkeypatch.delenv('ORIGIN_SECRET_ARN', raising=False)
    try:
        with pytest.raises(RuntimeError):
            lambda_handler.handler({'headers': {'x-origin-verify': 'anything'}}, SimpleNamespace())
    finally:
        lambda_handler.origin_secret.cache_clear()


def test_file_ranges_fit_lambda_and_reject_mixed_versions(gateway, tmp_path):
    g = gateway
    root = tmp_path / g.durable.agent['sub'] / 'workspace'
    root.mkdir(parents=True)
    data = b'x' * (3 * 1024 * 1024)
    (root / 'large.bin').write_bytes(data)
    g.svc.root = tmp_path
    url = f"/api/agents/{g.durable.agent['id']}/download?path=large.bin"
    assert g.client.get(url).status_code == 413
    first = g.client.get(url, headers={'Range': 'bytes=0-2097151'})
    assert first.status_code == 206 and len(first.content) == 2 * 1024 * 1024
    second = g.client.get(url, headers={'Range': 'bytes=2097152-4194303',
                                       'If-Range': first.headers['etag']})
    assert first.content + second.content == data
    (root / 'large.bin').write_bytes(b'y' * len(data))
    assert g.client.get(url, headers={'Range': 'bytes=2097152-4194303',
                                     'If-Range': first.headers['etag']}).status_code == 412


@pytest.mark.parametrize('authentication', ['cookie', 'bearer', 'mobile_bearer'])
def test_authenticated_subject_cannot_change_when_username_is_reused(monkeypatch, authentication):
    sub = str(uuid4())
    username = 'alice@example.com'
    team = {'id': str(uuid4()), 'name': 'Current team', 'created_at': 1}
    session = {'sub': sub, 'username': username, 'expires': int(time.time()) + 3600,
               'team_ids': [str(uuid4())]}
    claims = {'sub': sub, 'cognito:username': username, 'email': username}
    membership = {'sub': sub, 'username': username, 'email': username, 'enabled': True,
                  'groups': ['Humans', 'Admins'], 'team_ids': [team['id']]}
    svc = SimpleNamespace(
        get=Mock(return_value=session), membership=Mock(return_value=membership),
        verifier=SimpleNamespace(verify=Mock(return_value=claims)),
        mobile_verifier=SimpleNamespace(verify=Mock(return_value=claims)),
        team=Mock(return_value=team), teams=Mock(return_value=[team]),
    )
    if authentication == 'mobile_bearer':
        svc.verifier.verify.side_effect = jwt.InvalidAudienceError
    monkeypatch.setattr(module, 'services', lambda: svc)
    with TestClient(module.app, base_url='https://portal.example') as client:
        if authentication == 'cookie':
            client.cookies.set(module.COOKIE, 'original-session')
        else:
            client.headers['Authorization'] = 'Bearer original-token'

        # The same account still receives live membership, rather than stale session teams.
        response = client.get('/api/me')
        assert response.status_code == 200
        assert response.json()['sub'] == sub
        assert response.json()['team_ids'] == [team['id']]
        assert client.get('/api/admin/teams').status_code == 200

        # Recreating the username must not substitute the replacement administrator's identity.
        membership['sub'] = str(uuid4())
        svc.team.reset_mock()
        svc.teams.reset_mock()
        for route in ('/api/me', '/api/admin/teams'):
            response = client.get(route)
            assert response.status_code == 401
        svc.team.assert_not_called()
        svc.teams.assert_not_called()
        if authentication != 'cookie':
            svc.get.assert_not_called()
        if authentication == 'mobile_bearer':
            svc.mobile_verifier.verify.assert_called_with('original-token')


def test_invalid_bearer_never_falls_back_to_valid_cookie(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    svc = SimpleNamespace(verifier=SimpleNamespace(verify=Mock(side_effect=jwt.InvalidTokenError)),
                          get=Mock(return_value={'username': 'victim', 'expires': 9999999999}))
    request = Request({'type': 'http', 'headers': [
        (b'authorization', b'Bearer invalid'), (b'cookie', b'__Host-agent-sandbox-session=valid')]})
    with patch('backend.app.services', return_value=svc), pytest.raises(HTTPException) as error:
        module.current_user(request)
    assert error.value.status_code == 401
    svc.get.assert_not_called()
