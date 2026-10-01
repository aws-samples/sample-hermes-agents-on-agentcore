import socket
import socketserver
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from backend.app import (
    create_conversation,
    current_user,
    owned_agent,
    path_parts,
    public_agent,
    public_user,
)
from backend.service import Services, agent_limits
from common.security import Tokens, directory, read_file
from runtime import server
from runtime.broker import Handler, UnixServer, public_target

AGENT_ACCESS_TOKEN_USE = ''.join(('ac', 'cess'))
HUMAN_ID_TOKEN_USE = ''.join(('i', 'd'))
UNSPECIFIED_ADDRESS = socket.inet_ntoa(bytes(4))


@pytest.fixture
def verifier():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = Tokens('https://issuer.example', 'agent-client', 'access', 'Agents')
    verifier.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key()))
    claims = {'sub': str(uuid4()), 'iss': 'https://issuer.example', 'iat': int(time.time()),
              'exp': int(time.time()) + 600, 'client_id': 'agent-client',
              'token_use': AGENT_ACCESS_TOKEN_USE, 'cognito:groups': ['Agents'], 'team_id': str(uuid4()),
              'security_test_mode': False, 'input_limit_value': 0,
              'input_limit_unit': 'tokens', 'max_output_tokens': 0}
    return verifier, private, claims


def test_valid_agent_token(verifier):
    verifier, private, claims = verifier
    assert verifier.verify(jwt.encode(claims, private, algorithm='RS256'))['sub'] == claims['sub']


@pytest.mark.parametrize('field,value', [('iss', 'https://attacker.example'), ('exp', 1),
    ('client_id', 'human-client'), ('token_use', 'id'), ('cognito:groups', ['Humans'])])
def test_wrong_tokens_rejected(verifier, field, value):
    verifier, private, claims = verifier
    claims[field] = value
    with pytest.raises((ValueError, jwt.PyJWTError)):
        verifier.verify(jwt.encode(claims, private, algorithm='RS256'))


def test_forged_signature_rejected(verifier):
    verifier, _, claims = verifier
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        verifier.verify(jwt.encode(claims, attacker, algorithm='RS256'))


def test_agent_token_cannot_claim_multiple_teams(verifier):
    verifier, private, claims = verifier
    claims['team_ids'] = [claims.pop('team_id'), str(uuid4())]
    with pytest.raises(ValueError, match='Agent token cannot span teams'):
        verifier.verify(jwt.encode(claims, private, algorithm='RS256'))


def test_agent_token_requires_boolean_security_test_claim(verifier):
    verifier, private, claims = verifier
    claims['security_test_mode'] = 'true'
    with pytest.raises(ValueError, match='security test mode claim'):
        verifier.verify(jwt.encode(claims, private, algorithm='RS256'))


def test_agent_token_requires_typed_input_and_output_limits(verifier):
    verifier, private, claims = verifier
    claims['max_output_tokens'] = '4096'
    with pytest.raises(ValueError, match='max_output_tokens claim'):
        verifier.verify(jwt.encode(claims, private, algorithm='RS256'))


@pytest.mark.parametrize('mode', ['sequential', 'concurrent'])
def test_agent_token_execution_mode_is_validated(verifier, mode):
    validator, private, claims = verifier
    claims['execution_mode'] = mode
    assert validator.verify(jwt.encode(claims, private, algorithm='RS256'))['execution_mode'] == mode
    claims['execution_mode'] = 'no-lock-please'
    with pytest.raises(ValueError, match='execution mode'):
        validator.verify(jwt.encode(claims, private, algorithm='RS256'))
    del claims['execution_mode']
    assert validator.verify(jwt.encode(claims, private, algorithm='RS256'))['execution_mode'] == 'sequential'


def test_mobile_bearer_identity_uses_current_membership():
    sub, team_id = str(uuid4()), str(uuid4())
    svc = SimpleNamespace(
        verifier=SimpleNamespace(verify=lambda token: {
            'sub': sub, 'cognito:username': 'mobile@example.com', 'email': 'mobile@example.com'}),
        membership=lambda username: {'username': username, 'sub': sub, 'enabled': True, 'groups': ['Humans'],
                                     'team_ids': [team_id]},
        team=lambda team: {'id': team},
    )
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer mobile-token')]})
    with patch('backend.app.services', return_value=svc):
        assert current_user(request)['team_ids'] == [team_id]


def test_human_token_accepts_multiple_teams():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = Tokens('https://issuer.example', 'human-client', 'id', 'Humans')
    verifier.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key()))
    claims = {'sub': str(uuid4()), 'iss': 'https://issuer.example', 'iat': int(time.time()),
              'exp': int(time.time()) + 600, 'aud': 'human-client', 'token_use': HUMAN_ID_TOKEN_USE,
              'cognito:groups': ['Humans'], 'team_ids': [str(uuid4()), str(uuid4())]}
    result = verifier.verify(jwt.encode(claims, private, algorithm='RS256'))
    assert len(result['team_ids']) == 2


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '169.254.170.2', '192.168.0.1', UNSPECIFIED_ADDRESS])
def test_egress_rejects_private_and_metadata_addresses(address):
    with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]), pytest.raises(ValueError):
        public_target('example.org:443')


def test_egress_pins_validated_address():
    with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]) as resolve:
        assert public_target('example.org:443') == ('93.184.216.34', 443)
    assert resolve.call_count == 1


def test_broker_suppresses_expected_client_disconnect_tracebacks():
    broker = object.__new__(UnixServer)
    with patch('runtime.broker.sys.exception', return_value=BrokenPipeError()), \
            patch.object(socketserver.UnixStreamServer, 'handle_error') as parent:
        broker.handle_error(None, None)
        parent.assert_not_called()
    with patch('runtime.broker.sys.exception', return_value=ValueError('unexpected')), \
            patch.object(socketserver.UnixStreamServer, 'handle_error') as parent:
        broker.handle_error(None, None)
        parent.assert_called_once_with(None, None)


@pytest.mark.parametrize('error', [BrokenPipeError(), ConnectionResetError()])
def test_broker_handler_closes_expected_client_disconnects(error):
    handler = object.__new__(Handler)
    handler.close_connection = False
    with patch.object(BaseHTTPRequestHandler, 'handle_one_request', side_effect=error):
        handler.handle_one_request()
    assert handler.close_connection is True


@pytest.mark.parametrize('target', ['example.org:2049', 'user:pass@example.org:443', 'example.org:443/path'])
def test_egress_rejects_unsafe_authorities(target):
    with pytest.raises(ValueError):
        public_target(target)


def test_artifact_symlink_cannot_escape(tmp_path):
    (tmp_path / 'workspace').mkdir()
    (tmp_path / 'secret').write_text('canary')
    (tmp_path / 'workspace' / 'escape').symlink_to(tmp_path / 'secret')
    with directory(tmp_path, 'workspace') as fd, pytest.raises(OSError):
        read_file(fd, 'escape')


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'a/../../secret', 'a//b'])
def test_artifact_path_traversal_rejected(path):
    with pytest.raises(HTTPException):
        path_parts(path)


def test_missing_agent_is_not_authorized():
    svc = SimpleNamespace(agent=lambda agent_id: None)
    with patch('backend.app.services', return_value=svc), pytest.raises(HTTPException) as error:
        owned_agent({'team_ids': [str(uuid4())]}, str(uuid4()))
    assert error.value.status_code == 404


def test_same_team_uses_same_agent_partition():
    team_id, agent_id = str(uuid4()), str(uuid4())
    calls = []
    svc = SimpleNamespace(agent=lambda agent: calls.append(agent) or {'id': agent, 'team_id': team_id})
    with patch('backend.app.services', return_value=svc):
        assert owned_agent({'sub': str(uuid4()), 'team_ids': [str(uuid4()), team_id]}, agent_id)['id'] == agent_id
        assert owned_agent({'sub': str(uuid4()), 'team_ids': [team_id]}, agent_id)['id'] == agent_id
    assert calls == [agent_id, agent_id]


def test_different_team_cannot_resolve_agent():
    team_a, team_b, agent_id = str(uuid4()), str(uuid4()), str(uuid4())
    svc = SimpleNamespace(agent=lambda agent: {'id': agent, 'team_id': team_a})
    with patch('backend.app.services', return_value=svc), pytest.raises(HTTPException) as error:
        owned_agent({'team_ids': [team_b]}, agent_id)
    assert error.value.status_code == 404


def test_global_directory_exposes_metadata_without_session_access():
    budget = 2048
    agent = {'id': str(uuid4()), 'name': 'Visible', 'status': 'ready',
             'created_at': 1, 'team_id': str(uuid4()), 'security_test_mode': True,
             'token_budget': budget}
    result = public_agent(agent, {'team_ids': [str(uuid4())]})
    assert result['name'] == 'Visible'
    assert result['can_access'] is False
    assert result['security_test_mode'] is True
    assert result['input_limit_value'] == 2048
    assert result['input_limit_unit'] == 'tokens'
    assert result['max_output_tokens'] == 2048


def test_admin_cannot_assign_agent_to_multiple_teams():
    service = object.__new__(Services)
    service.team = lambda _team_id: {'id': _team_id}
    service.membership = lambda _username: {'kind': 'agent'}
    with pytest.raises(ValueError, match='exactly one team'):
        service.update_account('agent_x', [str(uuid4()), str(uuid4())], False, True)


def test_admin_agent_identity_includes_safe_mapping_metadata():
    fixture_secret_arn = f'arn:aws:secretsmanager:example:{uuid4()}:secret:fixture'
    identity = {
        'username': 'agent_123', 'sub': str(uuid4()), 'email': '',
        'team_ids': [str(uuid4())], 'enabled': True, 'status': 'CONFIRMED',
        'kind': 'agent', 'groups': ['Agents'], 'agent_id': str(uuid4()),
        'agent_name': 'Research teammate', 'created_by': str(uuid4()),
        'created_by_email': 'creator@example.com', 'secret_arn': fixture_secret_arn,
        'security_test_mode': True,
        'input_limit_value': 1024, 'input_limit_unit': 'mb',
        'max_output_tokens': 4096,
    }
    result = public_user(identity)
    assert result['agent_name'] == 'Research teammate'
    assert result['created_by_email'] == 'creator@example.com'
    assert result['security_test_mode'] is True
    assert result['input_limit_value'] == 1024
    assert result['input_limit_unit'] == 'mb'
    assert result['max_output_tokens'] == 4096
    assert 'secret_arn' not in result


def test_legacy_combined_budget_maps_to_both_new_limits():
    budget = 1234
    assert agent_limits({'token_budget': budget}) == {
        'input_limit_value': budget,
        'input_limit_unit': 'tokens',
        'max_output_tokens': budget,
    }


def test_new_conversation_persists_a_runtime_session_id():
    agent_id, team_id = uuid4(), str(uuid4())
    writes = []
    service = SimpleNamespace(
        agent=lambda _agent_id: {'id': str(agent_id), 'team_id': team_id, 'status': 'ready'},
        put=lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    with patch('backend.app.services', return_value=service):
        owner = str(uuid4())
        result = create_conversation(agent_id, {'sub': owner, 'team_ids': [team_id]})
    assert writes[0][1]['owner_sub'] == owner
    runtime_session_id = writes[0][1]['runtime_session_id']
    assert str(UUID(runtime_session_id)) == runtime_session_id
    assert result['id'] != runtime_session_id


def test_legacy_conversation_runtime_session_backfill_is_stable():
    agent_id, conversation_id = str(uuid4()), str(uuid4())
    record = {'pk': 'AGENT#' + agent_id, 'sk': 'CONV#' + conversation_id,
              'id': conversation_id}
    updates = []

    class ConditionalFailure(Exception):
        pass

    class Table:
        meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(
            ConditionalCheckFailedException=ConditionalFailure)))

        def update_item(self, **kwargs):
            updates.append(kwargs)
            record['runtime_session_id'] = kwargs['ExpressionAttributeValues'][':runtime_session_id']

    service = object.__new__(Services)
    service.table = Table()
    service.get = lambda _pk, _sk: record.copy()
    first = service.conversation(agent_id, conversation_id)['runtime_session_id']
    second = service.conversation(agent_id, conversation_id)['runtime_session_id']
    assert first == second
    assert len(updates) == 1


def test_ping_reports_busy_as_soon_as_execution_is_reserved():
    server.busy = False
    try:
        assert server.ping() == {'status': 'Healthy'}
        assert server.reserve_execution() is True
        assert server.ping() == {'status': 'HealthyBusy'}
        assert server.reserve_execution() is False
    finally:
        server.busy = False
