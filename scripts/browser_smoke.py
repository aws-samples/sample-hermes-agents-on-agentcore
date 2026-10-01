"""Live browser acceptance test. Generated test credentials never leave this process.

Creates dedicated smoke-test humans and an agent. Deletes the humans on exit.
Retains test artifacts for inspection. Requires AWS credentials and Playwright Chromium.
"""

import argparse
import json
import secrets
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright


def create_human(cognito, pool, username, password, teams, admin=False):
    cognito.admin_create_user(
        UserPoolId=pool, Username=username, MessageAction='SUPPRESS',
        UserAttributes=[{'Name': 'custom:teams', 'Value': ','.join(teams)},
                        {'Name': 'email', 'Value': username + '@example.com'},
                        {'Name': 'email_verified', 'Value': 'true'}],
    )
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=username, Password=password, Permanent=True)
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName='Humans')
    if admin:
        cognito.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName='Admins')


def login(page, origin, username, password):
    page.goto(origin, wait_until='domcontentloaded')
    page.get_by_role('link', name='Enter your workspace').click()
    page.locator('input[name="username"]:visible').fill(username)
    page.locator('input[name="password"]:visible').fill(password)
    page.locator('input[type="submit"]:visible, button[type="submit"]:visible').first.click()
    page.wait_for_url(origin + '/', timeout=90000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='AgentSandboxPortal')
    parser.add_argument('--region', default='eu-west-1')
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    config = Config(retries={'mode': 'adaptive', 'total_max_attempts': 3})
    cfn = session.client('cloudformation', config=config)
    cognito = session.client('cognito-idp', config=config)
    outputs = {entry['OutputKey']: entry['OutputValue'] for entry in
               cfn.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    origin, pool = outputs['PortalUrl'], outputs['UserPoolId']
    default_team = '253e60ee-2188-58c1-9358-87b6e086dfae'
    resources = [item for page in cfn.get_paginator('list_stack_resources').paginate(StackName=args.stack)
                 for item in page['StackResourceSummaries']]
    table_name = next(item['PhysicalResourceId'] for item in resources
                      if item['LogicalResourceId'] == 'MetadataBDB8F4DB')
    table = session.resource('dynamodb', config=config).Table(table_name)
    suffix = uuid.uuid4().hex[:12]
    username, teammate, outsider = (prefix + suffix for prefix in ('smoke_admin_', 'smoke_team_', 'smoke_out_'))
    passwords = {name: 'Aa1!' + secrets.token_urlsafe(32) for name in (username, teammate, outsider)}
    isolated_team = str(uuid.uuid4())
    table.put_item(Item={'pk': 'TEAM#' + isolated_team, 'sk': 'TEAM', 'id': isolated_team,
                         'name': 'Smoke isolated ' + suffix, 'created_at': 1})
    created_users = []
    stage = 'create account'
    try:
        for name, teams, admin in ((username, [default_team], True),
                                   (teammate, [default_team, isolated_team], False),
                                   (outsider, [isolated_team], False)):
            create_human(cognito, pool, name, passwords[name], teams, admin)
            created_users.append(name)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.set_default_timeout(45000)
            failures = []
            page.on('pageerror', lambda error: failures.append(str(error)))
            stage = 'open portal'
            Path('.deployment').mkdir(exist_ok=True)
            login(page, origin, username, passwords[username])
            page.screenshot(path='.deployment/login.png')
            stage = 'create agent'
            page.get_by_role('button', name='Create agent', exact=True).click()
            dialog = page.get_by_role('dialog')
            dialog.get_by_label('AGENT NAME').fill('Smoke research')
            dialog.get_by_label('INPUT LIMIT').fill('4096')
            dialog.get_by_label('MAX OUTPUT TOKENS').fill('4096')
            dialog.get_by_label('Security test mode').check()
            dialog.get_by_role('button', name='Create agent', exact=True).click()
            expect(dialog).not_to_be_visible(timeout=90000)
            expect(page.get_by_role('heading', name='Smoke research', exact=True)).to_be_visible(timeout=90000)
            agents = page.request.get(origin + '/api/agents').json()
            agent = max((item for item in agents if item['name'] == 'Smoke research'),
                        key=lambda item: item['created_at'])
            assert agent['status'] == 'ready'
            assert agent['security_test_mode'] is True
            assert agent['input_limit_value'] == 4096
            assert agent['input_limit_unit'] == 'tokens'
            assert agent['max_output_tokens'] == 4096
            owner = page.request.get(origin + '/api/me').json()['sub']
            Path('.deployment/browser-current.json').write_text(json.dumps({
                'test_user': username, 'agent_id': agent['id'], 'portal': origin,
                'owner': owner,
            }, indent=2))
            stage = 'security test mode verification'
            prompt = 'Use the terminal to read SECURITY_TEST_MODE and report its value.'
            composer = page.get_by_role('textbox', name='Message your agent')
            send = page.get_by_role('button', name='Send message', exact=True)
            composer.fill(prompt)
            page.wait_for_timeout(250)
            if send.is_disabled():
                raise AssertionError({'prompt': composer.input_value(), 'agents': agents,
                                      'heading': page.locator('.workspace-header h2').inner_text()})
            send.click()
            expect(page.locator('.message.assistant')).to_contain_text('true', timeout=330000)
            expect(page.get_by_role('button', name='Agent settings', exact=True)).to_be_enabled(timeout=60000)
            stage = 'first tool execution'
            composer.fill('Create proof.txt in the current workspace containing persistence-canary-2026. Confirm when the file is saved.')
            send.click()
            expect(page.get_by_role('button', name='Agent settings', exact=True)).to_be_enabled(timeout=330000)
            artifact = page.request.get(origin + f'/api/agents/{agent["id"]}/download?path=proof.txt')
            assert artifact.status == 200, {
                'download_status': artifact.status,
                'files': page.request.get(origin + f'/api/agents/{agent["id"]}/files').json(),
                'assistant': page.locator('.message.assistant').inner_text(),
            }
            assert artifact.text().strip() == 'persistence-canary-2026'
            stage = 'same conversation runtime session reuse'
            conversations = page.request.get(
                origin + f'/api/agents/{agent["id"]}/conversations').json()
            active_conversation = max(conversations, key=lambda item: item['created_at'])
            key = {'pk': 'AGENT#' + agent['id'], 'sk': 'CONV#' + active_conversation['id']}
            runtime_session = table.get_item(Key=key, ConsistentRead=True)['Item']['runtime_session_id']
            first_worker = table.get_item(Key=key, ConsistentRead=True)['Item']['last_worker_instance_id']
            composer.fill('Reply exactly SESSION_REUSED.')
            send.click()
            expect(page.locator('.message.assistant').last).to_contain_text('SESSION_REUSED', timeout=330000)
            persisted_session = table.get_item(Key=key, ConsistentRead=True)['Item']['runtime_session_id']
            assert persisted_session == runtime_session
            persisted_worker = table.get_item(Key=key, ConsistentRead=True)['Item']['last_worker_instance_id']
            assert persisted_worker == first_worker
            stage = 'new conversation persistence'
            # A fresh conversation must retain the same agent's workspace.
            page.get_by_role('button', name='New conversation', exact=True).click()
            page.get_by_role('textbox', name='Message your agent').fill(
                'Read proof.txt from your current workspace using a tool and return its exact content.')
            page.get_by_role('button', name='Send message', exact=True).click()
            expect(page.locator('.message.assistant')).to_contain_text('persistence-canary-2026', timeout=330000)
            stage = 'update input and output limits'
            page.get_by_role('button', name='Agent settings').click()
            settings = page.get_by_role('dialog')
            settings.get_by_label('INPUT LIMIT').fill('1')
            settings.get_by_label('UNIT').select_option('mb')
            settings.get_by_label('MAX OUTPUT TOKENS').fill('0')
            settings.get_by_role('button', name='Save settings').click()
            expect(settings).not_to_be_visible(timeout=60000)
            updated_agents = page.request.get(origin + '/api/agents').json()
            updated = next(item for item in updated_agents if item['id'] == agent['id'])
            assert (updated['input_limit_value'], updated['input_limit_unit'],
                    updated['max_output_tokens']) == (1, 'mb', 0)
            stage = 'admin portal'
            page.get_by_role('button', name='Admin portal').click()
            expect(page.get_by_role('heading', name='Teams without security bridges.')).to_be_visible()
            expect(page.get_by_text('agent · confirmed').first).to_be_visible(timeout=60000)
            expect(page.get_by_text('Smoke research', exact=True).last).to_be_visible(timeout=60000)
            expect(page.get_by_text(f'Agent ID: {agent["id"]}', exact=True)).to_be_visible(timeout=60000)
            expect(page.get_by_text(f'Created by: {username}@example.com', exact=True)).to_be_visible(timeout=60000)
            page.get_by_role('button', name='Back to workspace').click()

            stage = 'same-team session authorization'
            teammate_context = browser.new_context()
            teammate_page = teammate_context.new_page()
            login(teammate_page, origin, teammate, passwords[teammate])
            teammate_agents = teammate_page.request.get(origin + '/api/agents').json()
            teammate_agent = next(item for item in teammate_agents if item['id'] == agent['id'])
            assert teammate_agent['can_access'] is True
            private_conversations = teammate_page.request.get(
                origin + f'/api/agents/{agent["id"]}/conversations').json()
            assert private_conversations == []
            assert teammate_page.request.get(origin +
                f'/api/agents/{agent["id"]}/conversations/{active_conversation["id"]}/messages').status == 404
            shared_artifact = teammate_page.request.get(
                origin + f'/api/agents/{agent["id"]}/download?path=proof.txt')
            assert shared_artifact.status == 200
            allowed = teammate_page.request.post(
                origin + f'/api/agents/{agent["id"]}/conversations',
                headers={'Origin': origin, 'Content-Type': 'application/json'}, data='{}')
            assert allowed.status == 200, allowed.status
            teammate_context.close()

            stage = 'global visibility without team access'
            outsider_context = browser.new_context()
            outsider_page = outsider_context.new_page()
            login(outsider_page, origin, outsider, passwords[outsider])
            outsider_agents = outsider_page.request.get(origin + '/api/agents').json()
            outsider_agent = next(item for item in outsider_agents if item['id'] == agent['id'])
            assert outsider_agent['can_access'] is False
            denied = outsider_page.request.post(
                origin + f'/api/agents/{agent["id"]}/conversations',
                headers={'Origin': origin, 'Content-Type': 'application/json'}, data='{}')
            assert denied.status == 404, denied.status
            outsider_context.close()
            Path('.deployment').mkdir(exist_ok=True)
            page.screenshot(path='.deployment/portal-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path='.deployment/portal-mobile.png', full_page=True)
            assert not failures, failures
            evidence = {'portal': origin, 'test_user': username, 'agent_id': agent['id'],
                        'login': True, 'provisioning': True, 'chat_tools': True,
                        'artifact_download': True, 'new_conversation_persistence': True,
                        'admin_portal': True, 'same_team_session': True,
                        'same_team_conversations_private': True, 'same_team_artifacts_shared': True,
                        'global_agent_visibility': True, 'cross_team_session_denied': True,
                        'runtime_session_reused': True,
                        'hermes_process_reused': True,
                        'security_test_mode_injected': True,
                        'input_and_output_limits_created_and_updated': True,
                        'browser_errors': failures}
            Path('.deployment/browser-result.json').write_text(json.dumps(evidence, indent=2) + '\n')
            print(json.dumps(evidence, indent=2))
            browser.close()
    except (PlaywrightError, AssertionError, ValueError, KeyError, RuntimeError) as error:
        message = str(error)
        for password in passwords.values():
            message = message.replace(password, '<redacted>')
        raise RuntimeError(stage + ': ' + message) from None
    finally:
        for name in created_users:
            cognito.admin_delete_user(UserPoolId=pool, Username=name)
        table.delete_item(Key={'pk': 'TEAM#' + isolated_team, 'sk': 'TEAM'})


if __name__ == '__main__':
    main()
