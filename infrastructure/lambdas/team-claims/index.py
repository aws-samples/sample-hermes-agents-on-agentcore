"""Cognito pre-token V2 trigger: put UUID team claims in ID and access tokens."""

from uuid import UUID


def handler(event, _context):
    value = event.get('request', {}).get('userAttributes', {}).get('custom:teams')
    if value:
        team_ids = sorted(set(value.split(',')))
        if not team_ids or any(str(UUID(team_id)) != team_id for team_id in team_ids):
            raise ValueError('Invalid team membership')
        groups = event.get('request', {}).get('groupConfiguration', {}).get('groupsToOverride', [])
        if 'Agents' in groups:
            if len(team_ids) != 1:
                raise ValueError('Agent accounts must belong to exactly one team')
            attributes = event.get('request', {}).get('userAttributes', {})
            legacy_budget = attributes.get('custom:token_budget', '0')
            input_limit_value = attributes.get('custom:input_limit_value', legacy_budget)
            input_limit_unit = attributes.get('custom:input_limit_unit', 'tokens')
            max_output_tokens = attributes.get('custom:max_output_tokens', legacy_budget)
            execution_mode = attributes.get('custom:execution_mode', 'sequential')
            if execution_mode not in {'sequential', 'concurrent'}:
                raise ValueError('Invalid agent execution mode')
            if (not input_limit_value.isdigit() or not max_output_tokens.isdigit()
                    or input_limit_unit not in {'tokens', 'mb'}):
                raise ValueError('Invalid agent input or output limit')
            claims = {
                'team_id': team_ids[0],
                'security_test_mode': (
                    event.get('request', {}).get('userAttributes', {}).get(
                        'custom:security_test_mode', 'false').lower() == 'true'
                ),
                'input_limit_value': int(input_limit_value),
                'input_limit_unit': input_limit_unit,
                'max_output_tokens': int(max_output_tokens),
                'execution_mode': execution_mode,
            }
        else:
            claims = {'team_ids': team_ids}
        override = {'claimsToAddOrOverride': claims}
        event['response']['claimsAndScopeOverrideDetails'] = {
            'idTokenGeneration': override,
            'accessTokenGeneration': override,
        }
    return event
