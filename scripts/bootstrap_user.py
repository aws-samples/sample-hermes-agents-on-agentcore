"""Invite the first human portal account; never print or store its password."""

import argparse
import secrets
import time

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True)
    parser.add_argument('--stack', default='AgentSandboxPortal')
    parser.add_argument('--region')
    parser.add_argument('--profile')
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    config = Config(retries={'mode': 'adaptive', 'total_max_attempts': 3})
    cfn = session.client('cloudformation', config=config)
    cognito = session.client('cognito-idp', config=config)
    outputs = {item['OutputKey']: item['OutputValue'] for item in
               cfn.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    pool = outputs['UserPoolId']
    resources = cfn.list_stack_resources(StackName=args.stack)['StackResourceSummaries']
    table_name = next(item['PhysicalResourceId'] for item in resources
                      if item['ResourceType'] == 'AWS::DynamoDB::Table')
    default_team = '253e60ee-2188-58c1-9358-87b6e086dfae'
    session.resource('dynamodb', config=config).Table(table_name).put_item(Item={
        'pk': 'TEAM#' + default_team, 'sk': 'TEAM', 'id': default_team,
        'name': 'Default team', 'created_at': int(time.time()),
    })
    try:
        cognito.admin_create_user(
            UserPoolId=pool, Username=args.email, DesiredDeliveryMediums=['EMAIL'],
            TemporaryPassword='Aa1!' + secrets.token_urlsafe(30),
            UserAttributes=[{'Name': 'email', 'Value': args.email},
                            {'Name': 'email_verified', 'Value': 'true'}],
        )
        print('Invitation requested. Cognito sends the temporary password by email.')
    except cognito.exceptions.UsernameExistsException:
        print('Account already exists; password was not changed.')
    cognito.admin_update_user_attributes(
        UserPoolId=pool, Username=args.email,
        UserAttributes=[{'Name': 'custom:teams', 'Value': default_team}],
    )
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=args.email, GroupName='Humans')
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=args.email, GroupName='Admins')
    try:
        cognito.admin_user_global_sign_out(UserPoolId=pool, Username=args.email)
    except cognito.exceptions.NotAuthorizedException:
        pass
    print('Assigned Default team and administrator access.')
    print('Portal:', outputs['PortalUrl'])


if __name__ == '__main__':
    main()
