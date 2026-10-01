from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import boto3
import pytest
from botocore.stub import Stubber

from backend.app import UserUpdate, current_user, me, update_user
from backend.service import Services


@pytest.fixture
def service():
    fixture_credential = uuid4().hex
    service = object.__new__(Services)
    service.pool = 'eu-west-1_test'
    service.directory_index = 'DirectoryByType'
    service.table = boto3.resource(
        'dynamodb', region_name='eu-west-1', aws_access_key_id=fixture_credential,
        aws_secret_access_key=fixture_credential,
    ).Table('test-directory')
    return service


def test_directory_queries_existing_keys_and_consumes_all_pages(service):
    paginator = Mock()
    paginator.paginate.return_value = [
        {'Items': [{'pk': 'TEAM#old', 'sk': 'TEAM', 'id': 'old'}]},
        {'Items': [{'pk': 'TEAM#new', 'sk': 'TEAM', 'id': 'new'}]},
    ]
    service.table.meta.client.get_paginator = Mock(return_value=paginator)
    service.scan = Mock(side_effect=AssertionError('Indexed directory must not scan'))
    assert [team['id'] for team in service.teams()] == ['old', 'new']
    kwargs = paginator.paginate.call_args.kwargs
    assert kwargs['IndexName'] == 'DirectoryByType'
    assert 'ConsistentRead' not in kwargs  # GSIs are eventually consistent.
    expression = kwargs['KeyConditionExpression'].get_expression()
    partition, sort = expression['values']
    assert partition.get_expression()['values'][0].name == 'sk'
    assert partition.get_expression()['values'][1] == 'TEAM'
    assert sort.get_expression()['values'][0].name == 'pk'
    assert sort.get_expression()['values'][1] == 'TEAM#'

    paginator.paginate.return_value = [{'Items': [
        {'pk': 'AGENT#agent', 'sk': 'META', 'id': 'agent', 'entity': 'agent'},
        {'pk': 'AGENT#other', 'sk': 'META', 'id': 'other'},
    ]}]
    agent = {'pk': 'AGENT#agent', 'sk': 'META', 'id': 'agent', 'entity': 'agent'}
    service.batch_get = Mock(return_value={('AGENT#agent', 'META'): agent})
    assert service.agents() == [agent]
    expression = paginator.paginate.call_args.kwargs['KeyConditionExpression'].get_expression()
    assert expression['values'][0].get_expression()['values'][1] == 'META'
    assert expression['values'][1].get_expression()['values'][1] == 'AGENT#'


def test_directory_supports_staged_index_rollout(service):
    service.directory_index = None
    service.scan = Mock(return_value=[{'id': 'legacy'}])
    assert service.teams() == [{'id': 'legacy'}]
    service.scan.assert_called_once()


def test_batch_get_chunks_deduplicates_and_retries_only_unprocessed_keys(service):
    keys = [(f'AGENT#{i}', 'META') for i in range(101)]
    first = {'test-directory': {
        'Keys': [{'pk': pk, 'sk': sk} for pk, sk in keys[:100]], 'ConsistentRead': True,
    }}
    retry = {'test-directory': {'Keys': [{'pk': 'AGENT#1', 'sk': 'META'}], 'ConsistentRead': True}}
    final = {'test-directory': {'Keys': [{'pk': 'AGENT#100', 'sk': 'META'}], 'ConsistentRead': True}}
    with Stubber(service.table.meta.client) as stub, patch('backend.service.time.sleep'):
        stub.add_response('batch_get_item', {
            'Responses': {'test-directory': [{'pk': {'S': 'AGENT#0'}, 'sk': {'S': 'META'}}]},
            'UnprocessedKeys': {'test-directory': {
                'Keys': [{'pk': {'S': 'AGENT#1'}, 'sk': {'S': 'META'}}], 'ConsistentRead': True,
            }},
        }, {'RequestItems': first})
        stub.add_response('batch_get_item', {
            'Responses': {'test-directory': [{'pk': {'S': 'AGENT#1'}, 'sk': {'S': 'META'}}]},
        }, {'RequestItems': retry})
        stub.add_response('batch_get_item', {'Responses': {'test-directory': []}},
                          {'RequestItems': final})
        assert set(service.batch_get(keys + keys)) == {keys[0], keys[1]}
        stub.assert_no_pending_responses()


def test_batch_get_fails_instead_of_returning_partial_directory(service):
    pending = {'test-directory': {'Keys': [{'pk': 'missing', 'sk': 'META'}], 'ConsistentRead': True}}
    service.table.meta.client.batch_get_item = Mock(return_value={'UnprocessedKeys': pending})
    with patch('backend.service.time.sleep'), pytest.raises(RuntimeError, match='throttled'):
        service.batch_get([('missing', 'META')])
    assert service.table.meta.client.batch_get_item.call_count == 6
    service.table.meta.client.batch_get_item.reset_mock()
    assert service.batch_get([]) == {}
    service.table.meta.client.batch_get_item.assert_not_called()


def test_users_reuse_list_attributes_and_enrich_agents_in_batches(service):
    team_id, agent_id = str(uuid4()), str(uuid4())
    listed = [
        {'Username': 'creator', 'Enabled': True, 'UserStatus': 'CONFIRMED', 'Attributes': [
            {'Name': 'sub', 'Value': 'human-sub'}, {'Name': 'email', 'Value': 'creator@example.com'},
            {'Name': 'custom:teams', 'Value': team_id},
        ]},
        {'Username': 'agent_x', 'Enabled': False, 'UserStatus': 'FORCE_CHANGE_PASSWORD', 'Attributes': [
            {'Name': 'sub', 'Value': 'agent-sub'}, {'Name': 'custom:teams', 'Value': team_id},
        ]},
        {'Username': 'agent_unmapped', 'Enabled': True, 'UserStatus': 'CONFIRMED', 'Attributes': [
            {'Name': 'sub', 'Value': 'unmapped-sub'}, {'Name': 'custom:teams', 'Value': team_id},
        ]},
    ]
    user_paginator = Mock()
    user_paginator.paginate.return_value = [{'Users': listed[:1]}, {'Users': listed[1:]}]

    def paginator(name):
        if name == 'list_users':
            return user_paginator
        assert name == 'admin_list_groups_for_user'
        groups = Mock()
        groups.paginate.side_effect = lambda **kw: (
            [{'Groups': [{'GroupName': 'Humans'}]}, {'Groups': [{'GroupName': 'Admins'}]}]
            if kw['Username'] == 'creator' else [{'Groups': [{'GroupName': 'Agents'}]}]
        )
        return groups

    service.cognito = Mock()
    service.cognito.get_paginator.side_effect = paginator
    service.batch_get = Mock(side_effect=[
        {('IDENTITY#agent_x', 'META'): {'agent_id': agent_id}},
        {('AGENT#' + agent_id, 'META'): {
            'id': agent_id, 'name': 'Research', 'created_by': 'human-sub', 'security_test_mode': True,
        }},
    ])
    users = service.users()
    service.cognito.admin_get_user.assert_not_called()
    assert len(users) == 3
    assert users[0]['groups'] == ['Humans', 'Admins']
    assert users[1]['agent_name'] == 'Research'
    assert users[1]['created_by_email'] == 'creator@example.com'
    assert users[1]['security_test_mode'] is True
    assert users[1]['enabled'] is False
    assert users[1]['team_ids'] == [team_id]
    assert 'agent_id' not in users[2]
    assert service.batch_get.call_count == 2


def test_membership_fetches_overlap_with_bounded_workers(service):
    barrier, lock = Barrier(4), Lock()
    active = maximum = 0

    def membership(user):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return {'kind': 'human', 'sub': user['Username'], 'email': ''}

    service._listed_membership = membership
    paginator = Mock()
    paginator.paginate.return_value = [{'Users': [{'Username': str(i)} for i in range(4)]}]
    service.cognito = Mock()
    service.cognito.get_paginator.return_value = paginator
    service.batch_get = Mock(return_value={})
    with ThreadPoolExecutor(max_workers=4) as workers, patch('backend.service._directory_workers', workers):
        assert len(service.users()) == 4
    assert maximum == 4


def test_team_deletion_check_uses_live_attributes_without_groups_and_stops_early(service):
    team_id = str(uuid4())
    def pages(**kwargs):
        yield {'Users': [{'Username': 'recently-transferred', 'Attributes': []}]}
        raise AssertionError('Membership found; should not fetch another page')

    paginator = Mock()
    paginator.paginate.side_effect = pages
    service.cognito = Mock()
    service.cognito.get_paginator.return_value = paginator
    service.cognito.admin_get_user.return_value = {
        'UserAttributes': [{'Name': 'custom:teams', 'Value': team_id}],
    }
    assert service.team_has_users(team_id) is True
    service.cognito.get_paginator.assert_called_once_with('list_users')
    service.cognito.admin_get_user.assert_called_once_with(
        UserPoolId=service.pool, Username='recently-transferred')


def test_me_reuses_validated_teams_but_authorization_is_live_each_request():
    team = {'id': str(uuid4()), 'name': 'Team', 'created_at': 1}
    service = SimpleNamespace(
        get=Mock(return_value={'username': 'alice', 'sub': 'alice-sub', 'expires': 9999999999}),
        membership=Mock(return_value={
            'username': 'alice', 'sub': 'alice-sub', 'email': 'alice@example.com',
            'team_ids': [team['id']], 'enabled': True, 'groups': ['Humans', 'Admins'],
        }),
        team=Mock(return_value=team),
    )
    request = SimpleNamespace(cookies={'__Host-agent-sandbox-session': 'session'})
    with patch('backend.app.services', return_value=service):
        assert me(current_user(request))['teams'] == [team]
        assert service.team.call_count == 1
        current_user(request)
        assert service.membership.call_count == 2
        assert service.team.call_count == 2


def test_identity_save_reuses_the_membership_already_loaded_by_the_endpoint(service):
    team_id = str(uuid4())
    account = {
        'username': 'alice', 'sub': 'alice-sub', 'email': 'alice@example.com',
        'team_ids': [team_id], 'enabled': True, 'status': 'CONFIRMED',
        'groups': ['Humans'], 'kind': 'human',
    }
    service.membership = Mock(side_effect=[account, {**account, 'enabled': False}])
    service.require_teams = Mock(return_value=[team_id])
    service.get = Mock(return_value=None)
    service.cognito = Mock()
    payload = UserUpdate(team_ids=[team_id], admin=False, enabled=False)
    with patch('backend.app.services', return_value=service):
        result = update_user('alice', payload, {'username': 'admin'})
    assert result['enabled'] is False
    assert service.membership.call_count == 2  # One precondition read and one live response read.
    service.cognito.admin_disable_user.assert_called_once_with(UserPoolId=service.pool, Username='alice')
