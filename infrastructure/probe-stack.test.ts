import { test } from 'node:test';
import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { ProbeStack } from './probe-stack.js';
import { SKIP_BUNDLING } from './portal-stack.test.js';

test('runtime uses VPC, native EFS and bounded sessions', () => {
  const stack = new ProbeStack(new App(), 'Test', {
    env: { account: '123456789012', region: 'eu-west-1' },
  });
  const template = Template.fromStack(stack);
  template.resourceCountIs('AWS::BedrockAgentCore::Runtime', 1);
  template.resourceCountIs('AWS::EFS::MountTarget', 2);
  template.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
    ProtocolConfiguration: 'HTTP',
    NetworkConfiguration: { NetworkMode: 'VPC', NetworkModeConfig: Match.anyValue() },
    FilesystemConfigurations: [{ EfsAccessPoint: {
      AccessPointArn: Match.anyValue(), MountPath: '/mnt/agents',
    } }],
    LifecycleConfiguration: { IdleRuntimeSessionTimeout: 60, MaxLifetime: 600 },
  });
  template.hasResource('AWS::EFS::FileSystem', {
    Properties: Match.objectLike({ Encrypted: true }), DeletionPolicy: 'Retain',
  });
  template.hasResourceProperties('AWS::EFS::AccessPoint', {
    PosixUser: { Uid: '1000', Gid: '1000' },
    RootDirectory: { Path: '/agents', CreationInfo: {
      OwnerUid: '1000', OwnerGid: '1000', Permissions: '0700',
    } },
  });
});

test('portal user pool emits team claims and has administrative groups', () => {
  // Portal assertions live here to keep infrastructure tests using one fast node:test entry point.
  const app = new App({ context: { ...SKIP_BUNDLING, stage: 'portal' } });
  const { PortalStack } = require('./portal-stack.js') as typeof import('./portal-stack.js');
  const stack = new PortalStack(app, 'PortalTest', {
    env: { account: '123456789012', region: 'eu-west-1' },
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::Cognito::UserPool', {
    Schema: Match.arrayWith([
      Match.objectLike({ Name: 'teams', Mutable: true }),
      Match.objectLike({ Name: 'security_test_mode', Mutable: true }),
      Match.objectLike({ Name: 'execution_mode', Mutable: true,
        StringAttributeConstraints: { MinLength: '10', MaxLength: '10' } }),
      Match.objectLike({ Name: 'token_budget', Mutable: true }),
      Match.objectLike({ Name: 'input_limit_value', Mutable: true }),
      Match.objectLike({ Name: 'input_limit_unit', Mutable: true }),
      Match.objectLike({ Name: 'max_output_tokens', Mutable: true }),
    ]),
  });
  for (const group of ['Humans', 'Agents', 'Admins']) {
    template.hasResourceProperties('AWS::Cognito::UserPoolGroup', { GroupName: group });
  }
  template.hasResourceProperties('AWS::Lambda::Function', { Handler: 'index.handler' });
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    GlobalSecondaryIndexes: [{
      IndexName: 'DirectoryByType',
      KeySchema: [{ AttributeName: 'sk', KeyType: 'HASH' }, { AttributeName: 'pk', KeyType: 'RANGE' }],
      Projection: { ProjectionType: 'INCLUDE', NonKeyAttributes: [
        'id', 'name', 'created_at', 'team_id', 'status', 'entity', 'security_test_mode',
      ] },
    }],
  });
  template.hasResourceProperties('AWS::Lambda::Function', {
    Handler: 'backend.lambda_handler.handler',
    Environment: { Variables: Match.objectLike({ DIRECTORY_INDEX_NAME: 'DirectoryByType' }) },
  });
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: Match.arrayWith(['dynamodb:BatchGetItem', 'dynamodb:Query']),
      Resource: Match.arrayWith([{ 'Fn::Join': ['', Match.arrayWith(['/index/*'])] }]),
    })]) },
  });
});

test('portal can backfill the directory index before enabling indexed reads', () => {
  const app = new App({ context: { ...SKIP_BUNDLING, stage: 'portal', directoryIndexEnabled: 'false' } });
  const { PortalStack } = require('./portal-stack.js') as typeof import('./portal-stack.js');
  const stack = new PortalStack(app, 'PortalRolloutTest', {
    env: { account: '123456789012', region: 'eu-west-1' },
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    GlobalSecondaryIndexes: Match.arrayWith([Match.objectLike({ IndexName: 'DirectoryByType' })]),
  });
  template.hasResourceProperties('AWS::Lambda::Function', {
    Handler: 'backend.lambda_handler.handler',
    Environment: { Variables: Match.objectLike({ DIRECTORY_INDEX_NAME: '' }) },
  });
});
