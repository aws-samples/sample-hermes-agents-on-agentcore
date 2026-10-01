"""Local admin UI regression checks with mocked APIs; no AWS credentials or live mutations.

Start Vite, then run: uv run --with playwright python scripts/admin_ui_smoke.py
"""

import argparse
import asyncio
from collections import Counter
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright, expect


async def exercise(browser, origin, *, fail_users=False, admin=True):
    context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = await context.new_page()
    calls, errors = Counter(), []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    users_ready, save_ready = asyncio.Event(), asyncio.Event()
    teams = [{'id': 'team-1', 'name': 'Studio', 'created_at': 1}]
    users = [
        {'username': 'admin', 'sub': 'admin-sub', 'email': 'admin@example.com',
         'team_ids': ['team-1'], 'enabled': True, 'status': 'CONFIRMED', 'admin': admin, 'kind': 'human'},
        {'username': 'agent_x', 'sub': 'agent-sub', 'email': '', 'team_ids': ['team-1'],
         'enabled': True, 'status': 'CONFIRMED', 'admin': False, 'kind': 'agent',
         'agent_id': 'agent-1', 'agent_name': 'Research', 'created_by': 'admin-sub',
         'created_by_email': 'admin@example.com', 'security_test_mode': True},
    ]

    async def api(route):
        nonlocal fail_users
        request = route.request
        path = unquote(urlsplit(request.url).path)
        method = request.method
        calls[(method, path)] += 1
        body = request.post_data_json if request.post_data else {}
        status = 200
        if path == '/api/me':
            current = users[0]
            result = {**current, 'teams': [team for team in teams if team['id'] in current['team_ids']]}
        elif path == '/api/agents':
            result = [{'id': 'agent-1', 'name': 'Research', 'status': 'ready', 'created_at': 1,
                       'team_id': 'team-1', 'can_access': True, 'security_test_mode': True}]
        elif path in {'/api/agents/agent-1/conversations', '/api/agents/agent-1/files'}:
            result = []
        elif path == '/api/admin/teams':
            if method == 'POST':
                result = {**body, 'created_at': 2}
                teams.append(result)
            else:
                result = teams
        elif path.startswith('/api/admin/teams/'):
            team = next(team for team in teams if team['id'] == path.rsplit('/', 1)[1])
            if method == 'DELETE':
                teams.remove(team)
                status, result = 204, None
            else:
                await save_ready.wait()
                team.update(body)
                result = team
        elif path == '/api/admin/users':
            if method == 'POST':
                result = {**body, 'username': body['email'], 'sub': 'invited-sub',
                          'enabled': True, 'status': 'FORCE_CHANGE_PASSWORD', 'kind': 'human'}
                users.append(result)
            else:
                await users_ready.wait()
                status, result = (500, {'detail': 'Identity directory unavailable'}) if fail_users else (200, users)
                fail_users = False
        elif path.startswith('/api/admin/users/'):
            user = next(user for user in users if user['username'] == path.rsplit('/', 1)[1])
            if method == 'DELETE':
                users.remove(user)
                status, result = 204, None
            else:
                user.update({key: body[key] for key in ('team_ids', 'admin', 'enabled')})
                result = {key: value for key, value in user.items() if key not in {
                    'agent_id', 'agent_name', 'created_by', 'created_by_email', 'security_test_mode',
                }}
        else:
            raise AssertionError(f'Unexpected API call: {method} {path}')
        if status == 204:
            await route.fulfill(status=status)
        else:
            await route.fulfill(status=status, json=result)

    await page.route('**/api/**', api)
    await page.goto(origin + '/#admin', wait_until='domcontentloaded')
    if not admin:
        await expect(page.get_by_role('heading', name='Research', exact=True)).to_be_visible()
        await expect(page.get_by_label('Message your agent')).to_be_enabled()
        assert not any('/admin/' in path for _, path in calls)
        assert not errors
        await context.close()
        return

    await expect(page.get_by_role('heading', name='Teams without security bridges.')).to_be_visible()
    assert calls[('GET', '/api/agents')] == 0
    assert not any('/conversations' in path or '/files' in path for _, path in calls)
    await page.get_by_role('button', name='Teams', exact=False).first.click()
    card = page.locator('.team-card').filter(has_text='team-1')
    await expect(card.get_by_label('TEAM NAME')).to_have_value('Studio')
    await expect(card.get_by_role('button', name='Save')).to_be_enabled()

    if fail_users:
        users_ready.set()
        await page.get_by_role('button', name='Identities', exact=False).first.click()
        await expect(page.get_by_role('alert')).to_contain_text('Identity directory unavailable')
        await page.get_by_role('button', name='Teams', exact=False).first.click()
        await expect(card.get_by_role('button', name='Save')).to_be_enabled()
        await page.get_by_role('button', name='Identities', exact=False).first.click()
        await page.get_by_role('button', name='Refresh identities').click()
        await expect(page.get_by_role('button', name='Save Research', exact=True)).to_be_enabled()
        assert calls[('GET', '/api/admin/teams')] == 1
        assert calls[('GET', '/api/admin/users')] == 2
        assert not errors
        await context.close()
        return

    # A slow identities request must not prevent a team mutation or delay its success notice.
    await card.get_by_label('TEAM NAME').fill('Renamed studio')
    await card.get_by_role('button', name='Save').click()
    await expect(page.get_by_text('Saving changes…', exact=True)).to_be_visible()
    await expect(card.get_by_role('button', name='Save')).to_be_disabled()
    await expect(page.get_by_role('button', name='Back to workspace')).to_be_disabled()
    save_ready.set()
    await expect(page.get_by_text('Team renamed.', exact=True)).to_be_visible()
    assert calls[('POST', '/api/admin/teams/team-1')] == 1
    assert calls[('GET', '/api/admin/teams')] == 1

    users_ready.set()
    await page.get_by_role('button', name='Identities', exact=False).first.click()
    await expect(page.get_by_role('button', name='Save Research', exact=True)).to_be_enabled()
    await expect(page.get_by_label('Teams for Research').locator('option')).to_have_text(['Renamed studio'])
    await page.get_by_role('button', name='Save Research', exact=True).click()
    await expect(page.get_by_text('Identity updated.', exact=True)).to_be_visible()
    await expect(page.get_by_text('Created by: admin@example.com', exact=True)).to_be_visible()
    await expect(page.get_by_text('Agent ID: agent-1', exact=True)).to_be_visible()

    await page.get_by_label('Email', exact=True).fill('invite@example.com')
    await page.get_by_role('button', name='Send invitation').click()
    await expect(page.get_by_text('Invitation sent by Cognito.', exact=True)).to_be_visible()
    await expect(page.get_by_role('button', name='Save invite@example.com', exact=True)).to_be_visible()
    await page.get_by_role('button', name='Delete invite@example.com', exact=True).click()
    await expect(page.get_by_text('User deleted.', exact=True)).to_be_visible()
    await expect(page.get_by_role('button', name='Save invite@example.com', exact=True)).to_have_count(0)

    await page.get_by_role('button', name='Teams', exact=False).first.click()
    await page.get_by_label('Team name', exact=True).first.fill('Disposable')
    await page.get_by_role('button', name='Create team', exact=True).click()
    await expect(page.get_by_text('Team created.', exact=True)).to_be_visible()
    await page.get_by_role('button', name='Delete Disposable', exact=True).click()
    await expect(page.get_by_text('Team deleted.', exact=True)).to_be_visible()
    assert calls[('GET', '/api/admin/teams')] == 1
    assert calls[('GET', '/api/admin/users')] == 1

    await page.set_viewport_size({'width': 390, 'height': 844})
    await expect(page.get_by_role('button', name='Refresh teams')).to_be_visible()
    assert await page.locator('.admin-tabs').evaluate(
        'element => element.scrollWidth <= element.clientWidth + 1')
    await page.set_viewport_size({'width': 1440, 'height': 1000})

    # Return to the workspace refreshes the caller's membership once, then loads workspace data.
    await page.get_by_role('button', name='Back to workspace').click()
    await expect(page.get_by_label('Message your agent')).to_be_enabled()
    assert calls[('GET', '/api/me')] == 2
    assert calls[('GET', '/api/agents')] == 1
    assert not errors
    await context.close()


async def run(origin):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await exercise(browser, origin)
            await exercise(browser, origin, fail_users=True)
            await exercise(browser, origin, admin=False)
        finally:
            await browser.close()
    print('Admin UI checks passed: independent loading, targeted mutations, pending controls, '
          'retry isolation, metadata retention, deferred workspace loading, non-admin routing.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5173')
    args = parser.parse_args()
    asyncio.run(run(args.url.rstrip('/')))


if __name__ == '__main__':
    main()
