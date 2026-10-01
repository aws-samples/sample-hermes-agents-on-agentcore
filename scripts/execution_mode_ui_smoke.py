"""Local browser regression for agent execution-mode creation (all API calls are mocked)."""

import argparse
import json
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5173')
    args = parser.parse_args()
    created = []
    requests = []

    def api(route):
        path = urlparse(route.request.url).path
        if path == '/api/me':
            body = {'sub': 'user', 'username': 'user', 'email': 'test@example.com',
                    'team_ids': ['team'], 'teams': [{'id': 'team', 'name': 'Test'}], 'admin': False}
        elif path == '/api/agents' and route.request.method == 'POST':
            request = route.request.post_data_json
            requests.append(request)
            body = {**request, 'id': f'agent-{len(requests)}', 'status': 'ready',
                    'created_at': len(requests), 'can_access': True}
            created.append(body)
        elif path == '/api/agents':
            body = created
        elif path.endswith(('/files', '/conversations')):
            body = []
        else:
            raise AssertionError('Unexpected mocked API path: ' + path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(body))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1200})
            page.route('**/api/**', api)
            page.goto(args.url, wait_until='networkidle')
            page.get_by_role('button', name='Create agent', exact=True).click()
            dialog = page.get_by_role('dialog')
            expect(dialog.get_by_label('EXECUTION MODE')).to_have_value('sequential')
            dialog.get_by_label('AGENT NAME').fill('Concurrent test')
            dialog.get_by_label('EXECUTION MODE').select_option('concurrent')
            expect(dialog.get_by_text('Different conversations can run at the same time', exact=False)).to_be_visible()
            dialog.get_by_role('button', name='Create agent', exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text('Concurrent execution · no agent-wide lock', exact=False)).to_be_visible()
            page.get_by_role('button', name='Create agent', exact=True).click()
            dialog = page.get_by_role('dialog')
            expect(dialog.get_by_label('EXECUTION MODE')).to_have_value('sequential')
            dialog.get_by_label('AGENT NAME').fill('Sequential test')
            dialog.get_by_role('button', name='Create agent', exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text('Sequential execution · locked', exact=False)).to_be_visible()
            assert [request['execution_mode'] for request in requests] == ['concurrent', 'sequential']
            print(json.dumps({'default_sequential': True, 'concurrent_creation_payload': True,
                              'selection_resets': True, 'mode_displayed': True}))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
