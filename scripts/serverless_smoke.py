"""Live reconnect/replay/authorization acceptance using a retained synthetic smoke agent.

Temporary human passwords and OAuth/agent tokens remain inside this process. The selected
agent must have a Smoke-prefixed name. Its test conversations and artifacts are retained.
"""

import argparse
import base64
import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError
from browser_smoke import create_human, login
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright


def wait_for(check, page, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        page.wait_for_timeout(1000)
    raise TimeoutError('Live acceptance condition not reached')


def select_conversation(page, origin, agent_id, conversation_id):
    profile = page.request.get(origin + '/api/me').json()
    page.evaluate('''([user, agent, conversation]) => {
        localStorage.setItem('agent-sandbox-selection:' + JSON.stringify([user, null]), agent);
        localStorage.setItem('agent-sandbox-selection:' + JSON.stringify([user, agent]), conversation);
    }''', [profile['sub'], agent_id, conversation_id])
    page.reload(wait_until='domcontentloaded')


def native_token(browser, origin, domain, client_id, username, password):
    """Exercise public-client authorization-code + PKCE without a portal session cookie."""
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    callback = origin + '/api/auth/callback'
    code = {}
    context = browser.new_context()
    page = context.new_page()

    def capture(request):
        if not request.url.startswith(callback + '?'):
            return
        query = parse_qs(urlparse(request.url).query)
        if query.get('state') != [state] or not query.get('code'):
            raise ValueError('OAuth callback state mismatch')
        code['value'] = query['code'][0]

    # Redirected navigation may bypass route interception. Observe the callback request;
    # the portal rejects its independent state before consuming the native client's code.
    page.on('request', capture)
    try:
        page.goto(domain + '/oauth2/authorize?' + urlencode({
            'client_id': client_id, 'response_type': 'code', 'redirect_uri': callback,
            'scope': 'openid email profile', 'state': state, 'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }))
        page.locator('input[name="username"]:visible').fill(username)
        page.locator('input[name="password"]:visible').fill(password)
        page.locator('input[type="submit"]:visible, button[type="submit"]:visible').first.click()
        try:
            wait_for(lambda: code.get('value'), page, 45)
        except TimeoutError:
            page.screenshot(path='.deployment/native-login-failure.png')
            location = urlparse(page.url)
            detail = page.locator('body').inner_text()[:1500].replace(password, '<redacted>')
            raise RuntimeError(f'OAuth did not return a code at {location.netloc}{location.path}: {detail}') from None
        result = httpx.post(domain + '/oauth2/token', data={
            'grant_type': 'authorization_code', 'client_id': client_id, 'redirect_uri': callback,
            'code': code['value'], 'code_verifier': verifier,
        }, timeout=30)
        result.raise_for_status()
        return result.json()['id_token']
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='AgentSandboxPortal')
    parser.add_argument('--region', default='eu-west-1')
    parser.add_argument('--agent-id', required=True)
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 3})
    cfn = session.client('cloudformation', config=config)
    outputs = {item['OutputKey']: item['OutputValue'] for item in
               cfn.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    resources = [item for page in cfn.get_paginator('list_stack_resources').paginate(StackName=args.stack)
                 for item in page['StackResourceSummaries']]
    metadata_name = next(item['PhysicalResourceId'] for item in resources
                         if item['LogicalResourceId'] == 'MetadataBDB8F4DB')
    metadata = session.resource('dynamodb', config=config).Table(metadata_name)
    agent = metadata.get_item(Key={'pk': 'AGENT#' + str(uuid.UUID(args.agent_id)),
                                  'sk': 'META'}, ConsistentRead=True)['Item']
    if not agent['name'].startswith('Smoke'):
        raise ValueError('Only a retained synthetic Smoke agent can be used')
    cognito = session.client('cognito-idp', config=config)
    pool, origin = outputs['UserPoolId'], outputs['PortalUrl']
    domain = 'https://' + cognito.describe_user_pool(UserPoolId=pool)['UserPool']['Domain'] + \
        f'.auth.{args.region}.amazoncognito.com'
    names = ['smoke_reconnect_' + uuid.uuid4().hex[:10], 'smoke_observer_' + uuid.uuid4().hex[:10]]
    passwords = {name: 'Aa1!' + secrets.token_urlsafe(32) for name in names}
    created = []
    runtime_session = None
    agent_token = None
    evidence = {'portal': origin, 'agent_id': agent['id']}
    stage = 'create accounts'

    def stop_runtime():
        response = httpx.post(
            f'https://bedrock-agentcore.{args.region}.amazonaws.com/runtimes/' +
            quote(outputs['RuntimeArn'], safe='') + '/stopruntimesession',
            headers={'Authorization': 'Bearer ' + agent_token,
                     'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': runtime_session},
            json={}, timeout=30)
        response.raise_for_status()

    try:
        for name in names:
            create_human(cognito, pool, name, passwords[name], [agent['team_id']], admin=name == names[1])
            created.append(name)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            first_context, second_context = browser.new_context(), browser.new_context()
            first, second = first_context.new_page(), second_context.new_page()
            login(first, origin, names[0], passwords[names[0]])
            login(second, origin, names[0], passwords[names[0]])
            other_context = browser.new_context()
            other = other_context.new_page()
            login(other, origin, names[1], passwords[names[1]])
            stage = 'native PKCE login'
            token = native_token(browser, origin, domain, outputs['HumanClientId'], names[0], passwords[names[0]])
            with httpx.Client(base_url=origin, headers={'Authorization': 'Bearer ' + token}, timeout=40) as native:
                def api(method, path, **kwargs):
                    response = native.request(method, path, **kwargs)
                    response.raise_for_status()
                    return response.json()

                conversation = api('POST', f'/api/agents/{agent["id"]}/conversations', json={})['id']
                base = f'/api/agents/{agent["id"]}/conversations/{conversation}'
                stage = 'private conversation authorization'
                assert other.request.get(origin + f'/api/agents/{agent["id"]}/conversations').json() == []
                assert other.request.get(origin + base + '/messages').status == 404
                conv_key = {'pk': 'AGENT#' + agent['id'], 'sk': 'CONV#' + conversation}
                runtime_session = metadata.get_item(Key=conv_key, ConsistentRead=True)['Item']['runtime_session_id']
                secret = json.loads(session.client('secretsmanager', config=config).get_secret_value(
                    SecretId=agent['secret_arn'])['SecretString'])
                agent_token = cognito.admin_initiate_auth(
                    UserPoolId=pool, ClientId=outputs['AgentClientId'], AuthFlow='ADMIN_USER_PASSWORD_AUTH',
                    AuthParameters={'USERNAME': secret['username'], 'PASSWORD': secret['password']},
                )['AuthenticationResult']['AccessToken']
                observed = {'subscriptions': 0, 'hints': 0}

                def watch(socket):
                    def received(frame):
                        data = json.loads(frame)
                        if data.get('type') == 'subscribe_success':
                            observed['subscriptions'] += 1
                        if data.get('type') == 'data':
                            for encoded in data.get('event', []):
                                hint = json.loads(encoded)
                                assert set(hint) == {'run_id', 'seq'}
                                observed['hints'] += 1
                    socket.on('framereceived', received)

                first.on('websocket', watch)
                second.on('websocket', watch)
                stage = 'start delayed run'
                key = str(uuid.uuid4())
                message = ('Use the terminal to execute this Python code: '
                           'import time; from pathlib import Path; '
                           'Path("reconnect-large.bin").write_bytes(b"R" * (3 * 1024 * 1024)); '
                           'time.sleep(45); print("RECONNECT_CANARY"). '
                           'Wait for the terminal command to finish, then reply exactly RECONNECT_CANARY.')
                run = api('POST', base + '/runs', json={'message': message}, headers={'Idempotency-Key': key})
                run_path = base + '/runs/' + run['id']
                for suffix in ('', '/events'):
                    assert other.request.get(origin + run_path + suffix).status == 404
                assert other.request.post(origin + run_path + '/subscription',
                                           headers={'Origin': origin}).status == 404
                assert api('POST', base + '/runs', json={'message': message},
                           headers={'Idempotency-Key': key})['id'] == run['id']
                select_conversation(first, origin, agent['id'], conversation)
                wait_for(lambda: observed['subscriptions'] >= 1, first)
                wait_for(lambda: api('GET', run_path)['status'] == 'running', first)
                first_context.close()
                stage = 'reconnect on second client'
                select_conversation(second, origin, agent['id'], conversation)
                wait_for(lambda: observed['subscriptions'] >= 2 and observed['hints'] > 0, second)
                assert api('GET', run_path)['status'] == 'running'
                stage = 'membership revocation'
                cognito.admin_remove_user_from_group(UserPoolId=pool, Username=names[0], GroupName='Humans')
                wait_for(lambda: second.request.get(origin + run_path + '/events').status == 401, second)
                denied = second.request.post(origin + run_path + '/subscription', headers={'Origin': origin})
                assert denied.status == 401
                cognito.admin_add_user_to_group(UserPoolId=pool, Username=names[0], GroupName='Humans')
                wait_for(lambda: second.request.get(origin + '/api/me').status == 200, second)
                second.reload(wait_until='domcontentloaded')
                stage = 'durable completion and replay'
                wait_for(lambda: api('GET', run_path)['status'] not in {'pending', 'running'}, second, 180)
                assert api('GET', run_path)['status'] == 'complete'
                expect(second.locator('.message.assistant').last).to_contain_text('RECONNECT_CANARY', timeout=45000)
                history = api('GET', base + '/messages')['messages']
                assert len([item for item in history if item.get('run_id') == run['id'] and item['role'] == 'assistant']) == 1
                replay = api('GET', run_path + '/events')['events']
                assert [int(event['seq']) for event in replay] == list(range(1, len(replay) + 1))
                assert replay[-1]['data']['type'] == 'complete'
                checkpoint = metadata.get_item(Key=conv_key, ConsistentRead=True)['Item']['checkpoint_name']
                first_worker = metadata.get_item(Key=conv_key, ConsistentRead=True)['Item']['last_worker_instance_id']
                stage = 'range download'
                download = f'/api/agents/{agent["id"]}/download?path=reconnect-large.bin'
                one = native.get(download, headers={'Range': 'bytes=0-2097151'})
                two = native.get(download, headers={'Range': 'bytes=2097152-4194303', 'If-Range': one.headers['etag']})
                assert one.status_code == two.status_code == 206
                assert one.content + two.content == b'R' * (3 * 1024 * 1024)
                shared = other.request.get(origin + download, headers={'Range': 'bytes=0-7'})
                assert shared.status == 206 and shared.body() == b'R' * 8
                evidence.update(native_bearer=True, idempotent_send=True, second_client_reconnect=True,
                                appsync=observed, revocation_denied=True, durable_replay=True,
                                single_assistant_message=True, range_download=True,
                                same_team_admin_conversation_denied=True, artifacts_shared=True)
                stage = 'runtime interruption'
                stopped = api('POST', base + '/runs', json={'message':
                    'Use the terminal to run python -c "import time; time.sleep(90)". '
                    'Wait for it to finish before replying.'}, headers={'Idempotency-Key': str(uuid.uuid4())})
                stopped_path = base + '/runs/' + stopped['id']
                wait_for(lambda: api('GET', stopped_path)['status'] == 'running', second)
                second.wait_for_timeout(10000)
                assert api('GET', stopped_path)['status'] == 'running'
                stop_runtime()
                stage = 'reconcile interrupted run'
                outcome = wait_for(lambda: (state if (state := api('GET', stopped_path)['status'])
                                           not in {'pending', 'running'} else None), second, 360)
                assert outcome == 'interrupted'
                assert metadata.get_item(Key=conv_key, ConsistentRead=True)['Item']['checkpoint_name'] == checkpoint
                stage = 'restore committed checkpoint'
                recovered = api('POST', base + '/runs', json={'message':
                    'Use the terminal to read the first 8 bytes of reconnect-large.bin. '
                    'If they are all R, reply exactly RESTORED_CANARY.'},
                    headers={'Idempotency-Key': str(uuid.uuid4())})
                recovered_path = base + '/runs/' + recovered['id']
                wait_for(lambda: api('GET', recovered_path)['status'] not in {'pending', 'running'}, second, 180)
                assert api('GET', recovered_path)['status'] == 'complete'
                recovered_history = api('GET', base + '/messages')['messages']
                assert any(item.get('run_id') == recovered['id'] and item['role'] == 'assistant'
                           and 'RESTORED_CANARY' in item['text'] for item in recovered_history)
                restored = metadata.get_item(Key=conv_key, ConsistentRead=True)['Item']
                assert restored['last_worker_instance_id'] != first_worker
                assert restored['checkpoint_name'] != checkpoint
                evidence.update(runtime_interruption_reconciled=True, previous_checkpoint_retained=True,
                                restart_with_new_worker=True, conversation_id=conversation)
                Path('.deployment').mkdir(exist_ok=True)
                Path('.deployment/serverless-result.json').write_text(json.dumps(evidence, indent=2) + '\n')
                print(json.dumps(evidence, indent=2))
            browser.close()
    except (PlaywrightError, AssertionError, ValueError, KeyError, RuntimeError,
            TimeoutError, httpx.HTTPError, ClientError) as error:
        message = str(error)
        for password in passwords.values():
            message = message.replace(password, '<redacted>')
        raise RuntimeError(stage + ': ' + message) from None
    finally:
        try:
            if runtime_session and agent_token:
                stop_runtime()
        finally:
            for name in created:
                cognito.admin_delete_user(UserPoolId=pool, Username=name)


if __name__ == '__main__':
    main()
