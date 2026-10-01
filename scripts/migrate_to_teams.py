"""Move legacy user-owned records into global agents with one team each."""

import argparse

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='AgentSandboxPortal')
    parser.add_argument('--region', default='eu-west-1')
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    config = Config(retries={'mode': 'adaptive', 'total_max_attempts': 3})
    cfn = session.client('cloudformation', config=config)
    outputs = {item['OutputKey']: item['OutputValue'] for item in
               cfn.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    resources = cfn.list_stack_resources(StackName=args.stack)['StackResourceSummaries']
    table_name = next(item['PhysicalResourceId'] for item in resources
                      if item['ResourceType'] == 'AWS::DynamoDB::Table')
    table = session.resource('dynamodb', config=config).Table(table_name)
    cognito = session.client('cognito-idp', config=config)
    memberships = {}
    paginator = cognito.get_paginator('list_users')
    for page in paginator.paginate(UserPoolId=outputs['UserPoolId']):
        for user in page.get('Users', []):
            attrs = {item['Name']: item['Value'] for item in user.get('Attributes', [])}
            if attrs.get('sub') and attrs.get('custom:teams'):
                memberships[attrs['sub']] = sorted(set(attrs['custom:teams'].split(',')))
    scan = table.meta.client.get_paginator('scan')
    items = [item for page in scan.paginate(TableName=table.name) for item in page['Items']]
    conversations = {}
    moved = 0
    with table.batch_writer() as batch:
        for item in items:
            if not item['pk'].startswith('USER#') or not item['sk'].startswith('AGENT#'):
                continue
            owner, agent_id = item['pk'][5:], item['id']
            teams = memberships.get(owner)
            if not teams:
                continue
            team_id = teams[0]
            username = 'agent_' + agent_id
            item.update(pk='AGENT#' + agent_id, sk='META', entity='agent', team_id=team_id,
                        created_by=owner, username=username)
            item.pop('team_ids', None)
            batch.put_item(Item=item)
            batch.put_item(Item={'pk': 'TEAM#' + team_id, 'sk': 'AGENT#' + agent_id,
                                 'id': agent_id, 'name': item['name'], 'status': item['status'],
                                 'created_at': item['created_at']})
            batch.put_item(Item={'pk': 'IDENTITY#' + username, 'sk': 'META', 'agent_id': agent_id})
            batch.delete_item(Key={'pk': 'USER#' + owner, 'sk': 'AGENT#' + agent_id})
            try:
                cognito.admin_update_user_attributes(
                    UserPoolId=outputs['UserPoolId'], Username=username,
                    UserAttributes=[{'Name': 'custom:teams', 'Value': team_id}],
                )
            except cognito.exceptions.UserNotFoundException:
                pass
            moved += 1
        for item in items:
            if not item['pk'].startswith('USER#') or not item['sk'].startswith('CONV#'):
                continue
            owner = item['pk'][5:]
            _, agent_id, conversation_id = item['sk'].split('#', 2)
            if not memberships.get(owner):
                continue
            item.update(pk='AGENT#' + agent_id, sk='CONV#' + conversation_id)
            conversations[(owner, conversation_id)] = agent_id
            batch.put_item(Item=item)
            batch.delete_item(Key={'pk': 'USER#' + owner,
                                   'sk': f'CONV#{agent_id}#{conversation_id}'})
            moved += 1
        for item in items:
            if not item['pk'].startswith('MESSAGES#'):
                continue
            _, owner, conversation_id = item['pk'].split('#', 2)
            agent_id = conversations.get((owner, conversation_id))
            if agent_id:
                old_pk = item['pk']
                item['pk'] = f'MESSAGES#{agent_id}#{conversation_id}'
                batch.put_item(Item=item)
                batch.delete_item(Key={'pk': old_pk, 'sk': item['sk']})
                moved += 1
    print(f'Migrated {moved} records. Unassigned legacy test users were left isolated.')


if __name__ == '__main__':
    main()
