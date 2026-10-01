"""Verify live creation and admission for both execution modes with synthetic agents.

Creates one temporary human, deleted on exit. Synthetic agents/conversations are retained.
Passwords remain in-process; no credentials or tokens are included in the evidence.
"""

import argparse
import json
import secrets
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from browser_smoke import create_human, login
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='AgentSandboxPortal')
    parser.add_argument('--region', default='eu-west-1')
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 3})
    cfn = session.client('cloudformation', config=config)
    cognito = session.client('cognito-idp', config=config)
    outputs = {item['OutputKey']: item['OutputValue'] for item in
               cfn.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    pool, origin = outputs['UserPoolId'], outputs['PortalUrl']
    suffix = uuid.uuid4().hex[:10]
    username = 'smoke_execution_' + suffix
    password = 'Aa1!' + secrets.token_urlsafe(32)
    evidence = {'portal': origin, 'agents': {}}
    created = False
    try:
        create_human(cognito, pool, username, password, ['253e60ee-2188-58c1-9358-87b6e086dfae'])
        created = True
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1200})
                page.set_default_timeout(60000)
                login(page, origin, username, password)
                for mode in ('sequential', 'concurrent'):
                    name = f'Smoke {mode} {suffix}'
                    page.get_by_role('button', name='Create agent', exact=True).click()
                    dialog = page.get_by_role('dialog')
                    expect(dialog.get_by_label('EXECUTION MODE')).to_have_value('sequential')
                    dialog.get_by_label('AGENT NAME').fill(name)
                    dialog.get_by_label('EXECUTION MODE').select_option(mode)
                    dialog.get_by_label('MAX OUTPUT TOKENS').fill('1024')
                    dialog.get_by_role('button', name='Create agent', exact=True).click()
                    expect(dialog).not_to_be_visible(timeout=90000)
                    agent = next(item for item in page.request.get(origin + '/api/agents').json()
                                 if item['name'] == name)
                    assert agent['execution_mode'] == mode and agent['status'] == 'ready'
                    headers = {'Origin': origin, 'Content-Type': 'application/json'}
                    bases = []
                    for _ in range(2):
                        response = page.request.post(
                            origin + f'/api/agents/{agent["id"]}/conversations', headers=headers, data='{}')
                        assert response.status == 200
                        bases.append(origin + f'/api/agents/{agent["id"]}/conversations/{response.json()["id"]}')
                    prompt = ('Use the terminal to run python -c "import time; time.sleep(20); '
                              'print(12345)" with a timeout of 60 seconds. Wait for the command to '
                              'finish, then reply exactly EXECUTION_MODE_OK. Do not create files.')
                    run_ids = []
                    for index, base in enumerate(bases):
                        response = page.request.post(base + '/runs',
                            headers={**headers, 'Idempotency-Key': str(uuid.uuid4())},
                            data=json.dumps({'message': prompt}))
                        if index == 1 and mode == 'sequential':
                            assert response.status == 409, response.status
                        else:
                            assert response.status == 200, (response.status, response.text())
                            run_ids.append(response.json()['id'])
                    overlap = False
                    deadline = time.monotonic() + 240
                    while True:
                        states = [page.request.get(base + '/runs/' + run_id).json()
                                  for base, run_id in zip(bases, run_ids, strict=False)]
                        overlap |= len(states) == 2 and all(item['status'] == 'running' for item in states)
                        if all(item['status'] not in {'pending', 'running'} for item in states):
                            assert all(item['status'] == 'complete' for item in states), states
                            break
                        if time.monotonic() > deadline:
                            raise TimeoutError('Mode validation did not finish: ' + mode)
                        page.wait_for_timeout(1000)
                    for base, run_id in zip(bases, run_ids, strict=False):
                        history = page.request.get(base + '/messages').json()['messages']
                        answer = [item for item in history if item.get('run_id') == run_id and item['role'] == 'assistant']
                        assert len(answer) == 1 and 'EXECUTION_MODE_OK' in answer[0]['text']
                    if mode == 'concurrent':
                        assert overlap, 'Both concurrent runs must be observed running together'
                    evidence['agents'][mode] = {
                        'id': agent['id'], 'creation_mode_verified': True,
                        'completed_runs': len(run_ids), 'overlapping_running': overlap,
                        'second_run_rejected': mode == 'sequential',
                    }
                Path('.deployment').mkdir(exist_ok=True)
                page.screenshot(path='.deployment/execution-mode-live.png', full_page=True)
                Path('.deployment/execution-mode-result.json').write_text(json.dumps(evidence, indent=2) + '\n')
                print(json.dumps(evidence, indent=2))
            finally:
                browser.close()
    finally:
        if created:
            cognito.admin_delete_user(UserPoolId=pool, Username=username)


if __name__ == '__main__':
    main()
