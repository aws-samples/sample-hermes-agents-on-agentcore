import base64
import binascii
import hashlib
import json
import os
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config

from common.execution import execution_mode as agent_execution_mode
from common.security import Tokens, directory

# Shared across requests: concurrent admin loads must not each create their own pool.
_directory_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='directory')
HISTORY_PAGE_BYTES = 2 * 1024 * 1024


def normalize_teams(values) -> list[str]:
    if not isinstance(values, (list, tuple, set)) or not values:
        raise ValueError('At least one team is required')
    return sorted({str(UUID(value)) for value in values})


def encode_teams(values) -> str:
    return ','.join(normalize_teams(values))


def decode_teams(value: str | None) -> list[str]:
    return normalize_teams(value.split(',')) if value else []


def agent_limits(agent):
    legacy = int(agent.get('token_budget', 0))
    unit = agent.get('input_limit_unit', 'tokens')
    if unit not in {'tokens', 'mb'}:
        unit = 'tokens'
    return {
        'input_limit_value': int(agent.get('input_limit_value', legacy)),
        'input_limit_unit': unit,
        'max_output_tokens': int(agent.get('max_output_tokens', legacy)),
    }


class Services:
    def __init__(self):
        session = boto3.Session(region_name=os.environ.get('AWS_REGION'))
        config = Config(retries={'mode': 'adaptive', 'total_max_attempts': 3},
                        connect_timeout=10, read_timeout=30)
        self.table = session.resource('dynamodb', config=config).Table(os.environ['TABLE_NAME'])
        self.directory_index = os.environ.get('DIRECTORY_INDEX_NAME')
        self.cognito = session.client('cognito-idp', config=config)
        self.secrets = session.client('secretsmanager', config=config)
        self.pool = os.environ['USER_POOL_ID']
        self.human_client = os.environ['HUMAN_CLIENT_ID']
        self.agent_client = os.environ['AGENT_CLIENT_ID']
        self.region = session.region_name
        self.origin = os.environ['PUBLIC_URL'].rstrip('/')
        self.domain = os.environ['COGNITO_DOMAIN']
        self.root = Path(os.environ.get('AGENT_ROOT', '/mnt/agents'))
        self.secret_prefix = os.environ['SECRET_PREFIX']
        self.verifier = Tokens(os.environ['COGNITO_ISSUER'], self.human_client, 'id', 'Humans')
        self.mobile_verifier = (Tokens(os.environ['COGNITO_ISSUER'], os.environ['MOBILE_CLIENT_ID'],
                                       'id', 'Humans') if os.environ.get('MOBILE_CLIENT_ID') else None)

    def get(self, pk, sk):
        return self.table.get_item(Key={'pk': pk, 'sk': sk}, ConsistentRead=True).get('Item')

    def put(self, pk, sk, **values):
        self.table.put_item(Item={'pk': pk, 'sk': sk, **values})

    def batch_get(self, keys):
        keys = list(dict.fromkeys(keys))
        items = {}
        for start in range(0, len(keys), 100):
            pending = {self.table.name: {
                'Keys': [{'pk': pk, 'sk': sk} for pk, sk in keys[start:start + 100]],
                'ConsistentRead': True,
            }}
            for attempt in range(6):
                response = self.table.meta.client.batch_get_item(RequestItems=pending)
                for item in response.get('Responses', {}).get(self.table.name, []):
                    items[(item['pk'], item['sk'])] = item
                pending = response.get('UnprocessedKeys', {})
                if not pending:
                    break
                if attempt == 5:
                    raise RuntimeError('Directory reads were throttled. Please retry.')
                time.sleep(random.uniform(0, min(0.1 * 2 ** attempt, 2)))
        return items

    def query(self, pk, prefix):
        paginator = self.table.meta.client.get_paginator('query')
        return [item for page in paginator.paginate(
            TableName=self.table.name,
            KeyConditionExpression=Key('pk').eq(pk) & Key('sk').begins_with(prefix),
            ConsistentRead=True,
        ) for item in page['Items']]

    def history_page(self, agent_id, conversation_id, cursor=None):
        agent_id, conversation_id = str(agent_id), str(conversation_id)
        pk = f'MESSAGES#{agent_id}#{conversation_id}'
        start = {}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
                    raise ValueError()
                decoded = json.loads(base64.b64decode(
                    cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
                if (not isinstance(decoded, dict) or decoded.get('v') != 1
                        or decoded.get('agent_id') != agent_id
                        or decoded.get('conversation_id') != conversation_id
                        or not isinstance(decoded.get('after'), str)
                        or not re.fullmatch(r'MSG#[0-9]{20}#[01]', decoded['after'])):
                    raise ValueError()
                start['ExclusiveStartKey'] = {'pk': pk, 'sk': decoded['after']}
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise ValueError('Invalid history cursor for this conversation') from None
        result = self.table.query(
            KeyConditionExpression=Key('pk').eq(pk) & Key('sk').begins_with('MSG#'),
            ConsistentRead=True, ScanIndexForward=True, Limit=100, **start)
        messages = []
        encoded_bytes = 0
        last = None
        more = bool(result.get('LastEvaluatedKey'))
        for item in result.get('Items', []):
            message = {key: item[key] for key in ('role', 'text', 'partial', 'exit_reason', 'run_id')
                       if key in item}
            # Measure escaped JSON, not just text or DynamoDB item bytes. Reserve space for
            # commas/cursor/envelope; this also leaves headroom for Lambda's outer JSON body.
            size = len(json.dumps(message, ensure_ascii=True, separators=(',', ':')).encode())
            if encoded_bytes + size > HISTORY_PAGE_BYTES - 2048:
                if not messages:
                    raise OverflowError('Stored message exceeds the history page size limit')
                more = True
                break
            messages.append(message)
            encoded_bytes += size
            last = item['sk']
        next_cursor = None
        if more:
            after = last or result['LastEvaluatedKey']['sk']
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                'v': 1, 'agent_id': agent_id, 'conversation_id': conversation_id, 'after': after,
            }, separators=(',', ':')).encode()).decode().rstrip('=')
        return {'messages': messages, 'next_cursor': next_cursor}

    def scan(self, condition):
        paginator = self.table.meta.client.get_paginator('scan')
        return [item for page in paginator.paginate(
            TableName=self.table.name, FilterExpression=condition,
        ) for item in page['Items']]

    def team(self, team_id):
        return self.get('TEAM#' + str(UUID(team_id)), 'TEAM')

    def directory(self, record_type, prefix):
        # The index uses existing keys, so legacy records are indexed without a data migration.
        # Omit DIRECTORY_INDEX_NAME during the first rollout until backfill is ACTIVE.
        if not self.directory_index:
            return self.scan(Attr('sk').eq(record_type) & Attr('pk').begins_with(prefix))
        paginator = self.table.meta.client.get_paginator('query')
        return [item for page in paginator.paginate(
            TableName=self.table.name, IndexName=self.directory_index,
            KeyConditionExpression=Key('sk').eq(record_type) & Key('pk').begins_with(prefix),
        ) for item in page.get('Items', [])]

    def teams(self):
        return self.directory('TEAM', 'TEAM#')

    def require_teams(self, team_ids):
        team_ids = normalize_teams(team_ids)
        if any(not self.team(team_id) for team_id in team_ids):
            raise ValueError('One or more teams do not exist')
        return team_ids

    def create_team(self, name, team_id=None):
        team_id = str(UUID(team_id)) if team_id else str(uuid4())
        item = {'pk': 'TEAM#' + team_id, 'sk': 'TEAM', 'id': team_id,
                'name': name.strip(), 'created_at': int(time.time())}
        self.table.put_item(Item=item, ConditionExpression=Attr('pk').not_exists())
        return item

    def agent(self, agent_id):
        return self.get('AGENT#' + str(UUID(agent_id)), 'META')

    def conversation(self, agent_id, conversation_id):
        pk, sk = 'AGENT#' + str(UUID(agent_id)), 'CONV#' + str(UUID(conversation_id))
        record = self.get(pk, sk)
        if not record or record.get('runtime_session_id'):
            return record
        runtime_session_id = str(uuid4())
        try:
            self.table.update_item(
                Key={'pk': pk, 'sk': sk},
                UpdateExpression='SET runtime_session_id = :runtime_session_id',
                ConditionExpression=Attr('runtime_session_id').not_exists(),
                ExpressionAttributeValues={':runtime_session_id': runtime_session_id},
            )
            record['runtime_session_id'] = runtime_session_id
            return record
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            # Another request backfilled the same legacy conversation first.
            return self.get(pk, sk)

    def agents(self):
        # Public agent directory. Only non-sensitive metadata is returned by the API.
        summaries = [item for item in self.directory('META', 'AGENT#')
                     if item.get('entity') == 'agent']
        records = self.batch_get((item['pk'], item['sk']) for item in summaries)
        return [records[(item['pk'], item['sk'])] for item in summaries
                if (item['pk'], item['sk']) in records]

    def put_agent_membership(self, agent, previous=None):
        with self.table.batch_writer() as batch:
            if previous and previous != agent['team_id']:
                batch.delete_item(Key={'pk': 'TEAM#' + previous, 'sk': 'AGENT#' + agent['id']})
            batch.put_item(Item={
                'pk': 'TEAM#' + agent['team_id'], 'sk': 'AGENT#' + agent['id'],
                'id': agent['id'], 'name': agent['name'], 'status': agent['status'],
                'created_at': agent['created_at'],
            })

    def provision(self, team_id, creator, request_id, name, security_test_mode=False,
                  input_limit_value=0, input_limit_unit='tokens', max_output_tokens=0,
                  execution_mode='sequential'):
        execution_mode = agent_execution_mode({'execution_mode': execution_mode})
        if (input_limit_unit not in {'tokens', 'mb'}
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                       for value in (input_limit_value, max_output_tokens))):
            raise ValueError('Invalid input or output limit')
        team_id = self.require_teams([team_id])[0]
        agent_id = str(uuid5(UUID(team_id), request_id))
        record = self.agent(agent_id)
        if record and record['status'] == 'ready':
            return record
        if not record:
            record = {
                'pk': 'AGENT#' + agent_id, 'sk': 'META', 'entity': 'agent',
                'id': agent_id, 'name': name, 'team_id': team_id,
                'created_by': creator, 'status': 'provisioning', 'created_at': int(time.time()),
                'security_test_mode': bool(security_test_mode),
                'execution_mode': execution_mode,
                'input_limit_value': input_limit_value,
                'input_limit_unit': input_limit_unit,
                'max_output_tokens': max_output_tokens,
            }
            try:
                self.table.put_item(Item=record, ConditionExpression=Attr('pk').not_exists())
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                record = self.agent(agent_id)
        secret_name = f'{self.secret_prefix}/agents/{agent_id}'
        username = 'agent_' + agent_id
        try:
            secret = self.secrets.get_secret_value(SecretId=secret_name)
        except self.secrets.exceptions.ResourceNotFoundException:
            credential = {'username': username, 'password': 'Aa1!' + secrets.token_urlsafe(40)}
            try:
                created = self.secrets.create_secret(
                    Name=secret_name, SecretString=json.dumps(credential))
                # Avoid an immediate eventually-consistent read by name.
                secret = {'ARN': created['ARN'], 'SecretString': json.dumps(credential)}
            except self.secrets.exceptions.ResourceExistsException:
                # Another idempotent request created it first. Its name can take a
                # short interval to become readable across Secrets Manager replicas.
                for attempt in range(6):
                    try:
                        secret = self.secrets.get_secret_value(SecretId=secret_name)
                        break
                    except self.secrets.exceptions.ResourceNotFoundException:
                        if attempt == 5:
                            raise
                        time.sleep(random.uniform(0, min(0.1 * 2 ** attempt, 2)))
        credential = json.loads(secret['SecretString'])
        limits = agent_limits(record)
        attributes = [
            {'Name': 'custom:teams', 'Value': record['team_id']},
            {'Name': 'custom:security_test_mode',
             'Value': str(record.get('security_test_mode', False)).lower()},
            {'Name': 'custom:input_limit_value', 'Value': str(limits['input_limit_value'])},
            {'Name': 'custom:input_limit_unit', 'Value': limits['input_limit_unit']},
            {'Name': 'custom:max_output_tokens', 'Value': str(limits['max_output_tokens'])},
            {'Name': 'custom:execution_mode', 'Value': agent_execution_mode(record)},
        ]
        try:
            self.cognito.admin_create_user(
                UserPoolId=self.pool, Username=username, MessageAction='SUPPRESS',
                UserAttributes=attributes,
            )
        except self.cognito.exceptions.UsernameExistsException:
            pass
        self.cognito.admin_update_user_attributes(
            UserPoolId=self.pool, Username=username, UserAttributes=attributes)
        self.cognito.admin_set_user_password(
            UserPoolId=self.pool, Username=username, Password=credential['password'], Permanent=True)
        self.cognito.admin_add_user_to_group(
            UserPoolId=self.pool, Username=username, GroupName='Agents')
        user = self.cognito.admin_get_user(UserPoolId=self.pool, Username=username)
        sub = next(attr['Value'] for attr in user['UserAttributes'] if attr['Name'] == 'sub')
        with directory(self.root, sub, create=True):
            pass
        with directory(self.root, sub, 'workspace', create=True):
            pass
        record.update(status='ready', sub=sub, username=username, secret_arn=secret['ARN'])
        self.table.put_item(Item=record)
        self.put('IDENTITY#' + username, 'META', agent_id=agent_id)
        self.put_agent_membership(record)
        return record

    def update_agent_limits(self, agent, input_limit_value, input_limit_unit,
                            max_output_tokens):
        values = (input_limit_value, max_output_tokens)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
               for value in values):
            raise ValueError('Input and output limits must be non-negative integers')
        if input_limit_unit not in {'tokens', 'mb'}:
            raise ValueError('Input limit unit must be tokens or mb')
        self.cognito.admin_update_user_attributes(
            UserPoolId=self.pool, Username=agent['username'],
            UserAttributes=[
                {'Name': 'custom:input_limit_value', 'Value': str(input_limit_value)},
                {'Name': 'custom:input_limit_unit', 'Value': input_limit_unit},
                {'Name': 'custom:max_output_tokens', 'Value': str(max_output_tokens)},
            ],
        )
        try:
            self.cognito.admin_user_global_sign_out(
                UserPoolId=self.pool, Username=agent['username'])
        except self.cognito.exceptions.NotAuthorizedException:
            pass
        agent.update(input_limit_value=input_limit_value,
                     input_limit_unit=input_limit_unit,
                     max_output_tokens=max_output_tokens)
        agent.pop('token_budget', None)
        self.table.put_item(Item=agent)
        return agent

    def agent_token(self, agent):
        credential = json.loads(
            self.secrets.get_secret_value(SecretId=agent['secret_arn'])['SecretString'])
        result = self.cognito.admin_initiate_auth(
            UserPoolId=self.pool, ClientId=self.agent_client,
            AuthFlow='ADMIN_USER_PASSWORD_AUTH',
            AuthParameters={'USERNAME': credential['username'], 'PASSWORD': credential['password']},
        )
        return result['AuthenticationResult']['AccessToken']

    def membership(self, username):
        user = self.cognito.admin_get_user(UserPoolId=self.pool, Username=username)
        return self._membership(user, user.get('UserAttributes', []))

    def _membership(self, user, user_attributes):
        attributes = {item['Name']: item['Value'] for item in user_attributes}
        paginator = self.cognito.get_paginator('admin_list_groups_for_user')
        groups = [group['GroupName'] for page in paginator.paginate(
            UserPoolId=self.pool, Username=user['Username']) for group in page.get('Groups', [])]
        return {
            'username': user['Username'], 'sub': attributes['sub'],
            'email': attributes.get('email', ''),
            'team_ids': decode_teams(attributes.get('custom:teams')),
            'enabled': user['Enabled'], 'status': user['UserStatus'], 'groups': groups,
            'kind': 'agent' if 'Agents' in groups else 'human',
        }

    def _listed_membership(self, user):
        # Only the thread-safe Cognito client is used in the worker pool, not DynamoDB resources.
        return self._membership(user, user.get('Attributes', []))

    def team_has_users(self, team_id):
        def live_teams(username):
            user = self.cognito.admin_get_user(UserPoolId=self.pool, Username=username)
            attributes = {item['Name']: item['Value'] for item in user.get('UserAttributes', [])}
            return decode_teams(attributes.get('custom:teams'))

        paginator = self.cognito.get_paginator('list_users')
        for page in paginator.paginate(UserPoolId=self.pool):
            # Destructive checks use live membership, not eventually consistent ListUsers attributes.
            usernames = [user['Username'] for user in page.get('Users', [])]
            if any(team_id in teams for teams in _directory_workers.map(live_teams, usernames)):
                return True
        return False

    def users(self):
        paginator = self.cognito.get_paginator('list_users')
        users = []
        for page in paginator.paginate(UserPoolId=self.pool):
            # Limit queued work to one Cognito page and reuse ListUsers attributes.
            users.extend(_directory_workers.map(self._listed_membership, page.get('Users', [])))
        human_emails = {user['sub']: user['email'] for user in users if user['kind'] == 'human'}
        identities = self.batch_get([
            ('IDENTITY#' + user['username'], 'META') for user in users if user['kind'] == 'agent'
        ])
        agents = self.batch_get([
            ('AGENT#' + str(UUID(identity['agent_id'])), 'META') for identity in identities.values()
        ])
        for user in users:
            if user['kind'] != 'agent':
                continue
            identity = identities.get(('IDENTITY#' + user['username'], 'META'))
            agent = agents.get(('AGENT#' + str(UUID(identity['agent_id'])), 'META')) if identity else None
            if not agent:
                continue
            user.update(
                agent_id=agent['id'], agent_name=agent['name'],
                created_by=agent.get('created_by', ''),
                created_by_email=human_emails.get(agent.get('created_by', ''), ''),
                security_test_mode=bool(agent.get('security_test_mode', False)),
                **agent_limits(agent),
            )
        return users

    def create_human(self, email, team_ids, admin=False):
        team_ids = self.require_teams(team_ids)
        self.cognito.admin_create_user(
            UserPoolId=self.pool, Username=email, DesiredDeliveryMediums=['EMAIL'],
            TemporaryPassword='Aa1!' + secrets.token_urlsafe(30),
            UserAttributes=[{'Name': 'email', 'Value': email},
                            {'Name': 'email_verified', 'Value': 'true'},
                            {'Name': 'custom:teams', 'Value': encode_teams(team_ids)}],
        )
        self.cognito.admin_add_user_to_group(
            UserPoolId=self.pool, Username=email, GroupName='Humans')
        if admin:
            self.cognito.admin_add_user_to_group(
                UserPoolId=self.pool, Username=email, GroupName='Admins')
        return self.membership(email)

    def update_account(self, username, team_ids, admin, enabled, *, current=None):
        team_ids = self.require_teams(team_ids)
        current = current if current is not None else self.membership(username)
        if current['kind'] == 'agent':
            if len(team_ids) != 1:
                raise ValueError('Agent accounts must belong to exactly one team')
            if admin:
                raise ValueError('Agent accounts cannot be portal administrators')
        self.cognito.admin_update_user_attributes(
            UserPoolId=self.pool, Username=username,
            UserAttributes=[{'Name': 'custom:teams', 'Value': encode_teams(team_ids)}],
        )
        if current['kind'] == 'human':
            group_action = (self.cognito.admin_add_user_to_group if admin
                            else self.cognito.admin_remove_user_from_group)
            group_action(UserPoolId=self.pool, Username=username, GroupName='Admins')
        enable_action = self.cognito.admin_enable_user if enabled else self.cognito.admin_disable_user
        enable_action(UserPoolId=self.pool, Username=username)
        identity = self.get('IDENTITY#' + username, 'META')
        if identity:
            agent = self.agent(identity['agent_id'])
            previous = agent['team_id']
            agent['team_id'] = team_ids[0]
            self.table.put_item(Item=agent)
            self.put_agent_membership(agent, previous)
        try:
            self.cognito.admin_user_global_sign_out(UserPoolId=self.pool, Username=username)
        except self.cognito.exceptions.NotAuthorizedException:
            pass
        return self.membership(username)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@lru_cache
def services():
    return Services()
