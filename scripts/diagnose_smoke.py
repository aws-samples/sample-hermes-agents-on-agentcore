"""Inspect only the synthetic smoke-test agent recorded by browser_smoke.py."""

import json
import uuid
from pathlib import Path
from urllib.parse import quote

import boto3
import httpx
from botocore.config import Config


def main():
    current = json.loads(Path('.deployment/browser-current.json').read_text())
    if not current['test_user'].startswith('smoke_'):
        raise ValueError('Only synthetic smoke-test agents may be diagnosed')
    session = boto3.Session(region_name='eu-west-1')
    config = Config(retries={'mode': 'adaptive', 'total_max_attempts': 3})
    cfn = session.client('cloudformation', config=config)
    outputs = {entry['OutputKey']: entry['OutputValue'] for entry in
               cfn.describe_stacks(StackName='AgentSandboxPortal')['Stacks'][0]['Outputs']}
    resources = cfn.list_stack_resources(StackName='AgentSandboxPortal')['StackResourceSummaries']
    table_name = next(item['PhysicalResourceId'] for item in resources if item['ResourceType'] == 'AWS::DynamoDB::Table')
    table = session.resource('dynamodb', config=config).Table(table_name)
    agent = table.get_item(Key={'pk': 'AGENT#' + current['agent_id'], 'sk': 'META'})['Item']
    sm = session.client('secretsmanager', config=config)
    credentials = json.loads(sm.get_secret_value(SecretId=agent['secret_arn'])['SecretString'])
    cognito = session.client('cognito-idp', config=config)
    token = cognito.admin_initiate_auth(
        UserPoolId=outputs['UserPoolId'], ClientId=outputs['AgentClientId'], AuthFlow='ADMIN_USER_PASSWORD_AUTH',
        AuthParameters={'USERNAME': credentials['username'], 'PASSWORD': credentials['password']},
    )['AuthenticationResult']['AccessToken']
    conversation = str(uuid.uuid4())
    try:
        result = httpx.post(
            'https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/' + quote(outputs['RuntimeArn'], safe='') + '/invocations',
            headers={'Authorization': 'Bearer ' + token,
                     'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': conversation},
            json={'conversation_id': conversation, 'team_id': agent['team_id'], 'message':
                  'Use the terminal tool to run pwd and ls -la /workspace /workspace/workspace. '
                  'Then use the terminal to locate proof.txt beneath /workspace. Report the actual tool outputs. '
                  'Do not edit files. If the terminal is unavailable, explicitly say that.'},
            timeout=330,
        )
        print(result.status_code, result.text)
    finally:
        stop = httpx.post(
            'https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/' + quote(outputs['RuntimeArn'], safe='') + '/stopruntimesession',
            headers={'Authorization': 'Bearer ' + token,
                     'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': conversation}, json={}, timeout=30)
        stop.raise_for_status()


if __name__ == '__main__':
    main()
