import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';
import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { lambdaInstallCommands } from './lambda-bundling.js';
import { PortalStack } from './portal-stack.js';

// Unit tests assert template shape only; building the dependency layer needs uv and PyPI.
export const SKIP_BUNDLING = { 'aws:cdk:bundling-stacks': [] };

function synth(context: Record<string, unknown> = {}) {
  return Template.fromStack(new PortalStack(new App({ context: { ...SKIP_BUNDLING, ...context } }), 'PortalTest', {
    env: { account: '123456789012', region: 'eu-west-1' },
  }));
}

// Template.fromStack also checks the entire CloudFormation dependency graph for
// cycles, including the authorizer -> OAuth/CloudFront -> HTTP API path.
const template = synth();
const resources = template.toJSON().Resources;

test('deployment selects another agent adapter without replacing runtime or storage identities', () => {
  const echoResources = synth({ agentImplementation: 'echo' }).toJSON().Resources;
  assert.notDeepEqual(echoResources.Runtime.Properties.AgentRuntimeArtifact,
    resources.Runtime.Properties.AgentRuntimeArtifact);
  assert.deepEqual(Object.keys(echoResources).sort(), Object.keys(resources).sort());
  assert.equal(echoResources.Runtime.Properties.AgentRuntimeName, 'agent_sandbox_portal');
  assert.throws(() => synth({ agentImplementation: '../escape' }), /agentImplementation/);
});

const functions = Object.entries(template.findResources('AWS::Lambda::Function'));
function handler(name: string) {
  const entry = functions.find(([, resource]) => resource.Properties.Handler === name);
  assert.ok(entry, `Missing Lambda handler ${name}`);
  return { id: entry[0], ...entry[1].Properties };
}
function policyFor(functionId: string) {
  const role = resources[functionId].Properties.Role['Fn::GetAtt'][0];
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter(resource => resource.Properties.Roles.some((ref: { Ref: string }) => ref.Ref === role))
    .flatMap(resource => resource.Properties.PolicyDocument.Statement);
}

test('portal retains persistent logical IDs from the ECS stack', () => {
  const expected: Record<string, string> = {
    AgentFiles4C94AFE5: 'AWS::EFS::FileSystem',
    AgentFilesRoot90F4AABE: 'AWS::EFS::AccessPoint',
    AgentFilesEfsMountTarget16DB12CB3: 'AWS::EFS::MountTarget',
    AgentFilesEfsMountTarget25AF8B66C: 'AWS::EFS::MountTarget',
    Users0A0EEA89: 'AWS::Cognito::UserPool',
    UsersAgentClient396CE365: 'AWS::Cognito::UserPoolClient',
    UsersHumanClient1BFE3CA3: 'AWS::Cognito::UserPoolClient',
    UsersDomainC6632324: 'AWS::Cognito::UserPoolDomain',
    MetadataBDB8F4DB: 'AWS::DynamoDB::Table',
    Runtime: 'AWS::BedrockAgentCore::Runtime',
    RuntimeRoleFD8790A4: 'AWS::IAM::Role',
    Frontend23D93C55: 'AWS::S3::Bucket',
  };
  for (const [id, type] of Object.entries(expected)) assert.equal(resources[id]?.Type, type, id);
  for (const id of ['AgentFiles4C94AFE5', 'Users0A0EEA89', 'MetadataBDB8F4DB', 'Frontend23D93C55']) {
    assert.equal(resources[id].DeletionPolicy, 'Retain');
    assert.equal(resources[id].UpdateReplacePolicy, 'Retain');
  }
  assert.deepEqual(resources.MetadataBDB8F4DB.Properties.KeySchema, [
    { AttributeName: 'pk', KeyType: 'HASH' }, { AttributeName: 'sk', KeyType: 'RANGE' },
  ]);
  assert.deepEqual(resources.AgentFilesRoot90F4AABE.Properties.RootDirectory, {
    Path: '/agents', CreationInfo: { OwnerUid: '1000', OwnerGid: '1000', Permissions: '0700' },
  });
  assert.equal(resources.Runtime.Properties.AgentRuntimeName, 'agent_sandbox_portal');
});

test('all API routes use the short ARM64 commands Lambda and the existing EFS access point', () => {
  for (const type of ['AWS::ECS::Cluster', 'AWS::ECS::Service', 'AWS::ECS::TaskDefinition',
    'AWS::ElasticLoadBalancingV2::LoadBalancer', 'AWS::CloudFront::VpcOrigin']) {
    template.resourceCountIs(type, 0);
  }
  template.hasResourceProperties('AWS::ApiGatewayV2::Api', { ProtocolType: 'HTTP' });
  const commands = handler('backend.lambda_handler.handler');
  const integrationIds = new Set(Object.keys(template.findResources('AWS::ApiGatewayV2::Integration', {
    Properties: Match.objectLike({ IntegrationType: 'AWS_PROXY', PayloadFormatVersion: '2.0',
      IntegrationUri: { 'Fn::GetAtt': [commands.id, 'Arn'] } }),
  })));
  for (const route of ['ANY /api', 'ANY /api/{proxy+}']) {
    const matches = Object.values(template.findResources('AWS::ApiGatewayV2::Route', {
      Properties: Match.objectLike({ RouteKey: route, AuthorizationType: 'NONE' }),
    }));
    assert.equal(matches.length, 1);
    assert.ok([...integrationIds].some(id => JSON.stringify(matches[0].Properties.Target).includes(id)));
  }
  assert.deepEqual(commands.Architectures, ['arm64']);
  assert.equal(commands.Timeout, 29);
  assert.equal(commands.FileSystemConfigs[0].LocalMountPath, '/mnt/agents');
  assert.deepEqual(commands.FileSystemConfigs[0].Arn,
    resources.Runtime.Properties.FilesystemConfigurations[0].EfsAccessPoint.AccessPointArn);
  assert.equal(commands.VpcConfig.SubnetIds.length, 2);
  assert.ok(resources[commands.id].DependsOn.includes('AgentFilesEfsMountTarget16DB12CB3'));
  const mountPolicy = policyFor(commands.id).find(statement =>
    JSON.stringify(statement.Action).includes('elasticfilesystem:ClientWrite') && statement.Condition);
  assert.ok(mountPolicy);
  assert.deepEqual(mountPolicy.Condition.ArnEquals['elasticfilesystem:AccessPointArn'], commands.FileSystemConfigs[0].Arn);
  template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
    IpProtocol: 'tcp', FromPort: 2049, ToPort: 2049,
    SourceSecurityGroupId: commands.VpcConfig.SecurityGroupIds[0],
  });
});

test('CloudFront preserves cookies, Origin and Authorization on both API behaviors', () => {
  const behaviors = resources.PortalDF61C365.Properties.DistributionConfig.CacheBehaviors;
  assert.deepEqual(behaviors.map((behavior: { PathPattern: string }) => behavior.PathPattern), ['/api', '/api/*']);
  for (const behavior of behaviors) {
    // Managed AllViewerExceptHostHeader forwards Origin, Authorization, all
    // cookies and query strings. CachingDisabled prevents private response caching.
    assert.equal(behavior.OriginRequestPolicyId, 'b689b0a8-53d0-40ab-baf2-68738e2966ac');
    assert.equal(behavior.CachePolicyId, '4135ea2d-6df8-44a3-9df3-4b5a84be39ad');
    assert.ok(behavior.AllowedMethods.includes('POST'));
    assert.equal(behavior.ViewerProtocolPolicy, 'https-only');
  }
});

test('CloudFront proves itself to the API origin with a secret only Commands can read', () => {
  const [secretId] = Object.keys(template.findResources('AWS::SecretsManager::Secret', {
    Properties: Match.objectLike({ Description: Match.stringLikeRegexp('CloudFront') }),
  }));
  assert.ok(secretId);
  const distribution = resources.PortalDF61C365.Properties.DistributionConfig;
  const apiOrigins = distribution.Origins.filter((origin: { CustomOriginConfig?: unknown }) => origin.CustomOriginConfig);
  assert.equal(apiOrigins.length, 1);
  const headers = apiOrigins[0].OriginCustomHeaders;
  assert.equal(headers.length, 1);
  assert.equal(headers[0].HeaderName, 'X-Origin-Verify');
  // A dynamic reference: the secret value is never written into the template.
  const value = JSON.stringify(headers[0].HeaderValue);
  assert.match(value, /\{\{resolve:secretsmanager:/);
  assert.ok(value.includes(secretId));
  const commands = handler('backend.lambda_handler.handler');
  assert.deepEqual(commands.Environment.Variables.ORIGIN_SECRET_ARN, { Ref: secretId });
  assert.ok(policyFor(commands.id).some(statement =>
    JSON.stringify(statement.Action).includes('secretsmanager:GetSecretValue')
    && JSON.stringify(statement.Resource).includes(secretId)));
  for (const name of ['backend.events.authorize', 'backend.events.dispatch', 'backend.events.publish']) {
    const other = handler(name);
    assert.equal(other.Environment.Variables.ORIGIN_SECRET_ARN, undefined, name);
    assert.ok(!JSON.stringify(policyFor(other.id)).includes(secretId), name);
  }
});

test('run storage has durable replay, TTL, INSERT streams and the work index', () => {
  template.resourceCountIs('AWS::DynamoDB::Table', 2);
  template.hasResource('AWS::DynamoDB::Table', {
    DeletionPolicy: 'Retain', Properties: Match.objectLike({
      KeySchema: [{ AttributeName: 'pk', KeyType: 'HASH' }, { AttributeName: 'sk', KeyType: 'RANGE' }],
      TimeToLiveSpecification: { AttributeName: 'expires', Enabled: true },
      StreamSpecification: { StreamViewType: 'NEW_IMAGE' },
      AttributeDefinitions: Match.arrayWith([{ AttributeName: 'updated_at', AttributeType: 'N' }]),
      GlobalSecondaryIndexes: [{ IndexName: 'WorkByStatus',
        KeySchema: [{ AttributeName: 'status', KeyType: 'HASH' }, { AttributeName: 'updated_at', KeyType: 'RANGE' }],
        Projection: { ProjectionType: 'ALL' } }],
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    }),
  });
});

test('Events permits only Lambda connect/subscribe and IAM publish with no auth cache or dependency cycle', () => {
  const auth = handler('backend.events.authorize');
  template.hasResourceProperties('AWS::AppSync::Api', {
    EventConfig: {
      AuthProviders: [
        { AuthType: 'AWS_IAM' },
        { AuthType: 'AWS_LAMBDA', LambdaAuthorizerConfig: {
          AuthorizerUri: { 'Fn::GetAtt': [auth.id, 'Arn'] }, AuthorizerResultTtlInSeconds: 0,
        } },
      ],
      ConnectionAuthModes: [{ AuthType: 'AWS_LAMBDA' }],
      DefaultPublishAuthModes: [{ AuthType: 'AWS_IAM' }],
      DefaultSubscribeAuthModes: [{ AuthType: 'AWS_LAMBDA' }],
    },
  });
  template.hasResourceProperties('AWS::AppSync::ChannelNamespace', {
    Name: 'runs', PublishAuthModes: [{ AuthType: 'AWS_IAM' }], SubscribeAuthModes: [{ AuthType: 'AWS_LAMBDA' }],
  });
  template.hasResourceProperties('AWS::Lambda::Permission', {
    Action: 'lambda:InvokeFunction', Principal: 'appsync.amazonaws.com',
    FunctionName: { 'Fn::GetAtt': [auth.id, 'Arn'] }, SourceArn: Match.anyValue(),
  });
  assert.equal(auth.Environment.Variables.EVENTS_HTTP_DOMAIN, undefined);
  assert.equal(auth.Environment.Variables.EVENTS_REALTIME_DOMAIN, undefined);
  assert.deepEqual(auth.Environment.Variables.TABLE_NAME, { Ref: 'MetadataBDB8F4DB' });
  assert.deepEqual(auth.Environment.Variables.USER_POOL_ID, { Ref: 'Users0A0EEA89' });
  assert.ok(policyFor(auth.id).some(statement => JSON.stringify(statement.Action).includes('cognito-idp:AdminGetUser')));
  const publish = policyFor(handler('backend.events.publish').id)
    .find(statement => statement.Action === 'appsync:EventPublish');
  assert.ok(publish);
  assert.notEqual(publish.Resource, '*');
  assert.ok(!JSON.stringify(policyFor(auth.id)).includes('appsync:EventPublish'));
  template.resourceCountIs('AWS::AppSync::ApiKey', 0);
});

test('stream consumers have exact filters, partial failure handling, bounded retries and monitored failure destinations', () => {
  const mappings = Object.values(template.findResources('AWS::Lambda::EventSourceMapping'));
  assert.equal(mappings.length, 2);
  for (const [name, kind] of [['backend.events.dispatch', 'run'], ['backend.events.publish', 'event']]) {
    const fn = handler(name);
    const mapping = mappings.find(resource => JSON.stringify(resource.Properties.FunctionName).includes(fn.id))?.Properties;
    assert.ok(mapping, name);
    assert.equal(mapping.StartingPosition, 'TRIM_HORIZON');
    assert.deepEqual(mapping.FunctionResponseTypes, ['ReportBatchItemFailures']);
    assert.equal(mapping.BisectBatchOnFunctionError, true);
    assert.equal(mapping.MaximumRetryAttempts, 3);
    assert.equal(mapping.MaximumRecordAgeInSeconds, 3600);
    assert.deepEqual(mapping.FilterCriteria.Filters.map((filter: { Pattern: string }) => JSON.parse(filter.Pattern)), [{
      eventName: ['INSERT'], dynamodb: { NewImage: {
        kind: { S: [kind] }, ...(kind === 'run' ? { status: { S: ['pending'] } } : {}),
      } },
    }]);
    const queueId = mapping.DestinationConfig.OnFailure.Destination['Fn::GetAtt'][0];
    assert.equal(resources[queueId].Type, 'AWS::SQS::Queue');
    assert.equal(resources[queueId].Properties.MessageRetentionPeriod, 14 * 24 * 60 * 60);
    assert.equal(resources[queueId].Properties.SqsManagedSseEnabled, true);
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: 'AWS/SQS', MetricName: 'ApproximateNumberOfMessagesVisible', Threshold: 1,
      Dimensions: [{ Name: 'QueueName', Value: { 'Fn::GetAtt': [queueId, 'QueueName'] } }],
    });
    for (const metric of ['Errors', 'Throttles', 'IteratorAge', 'DestinationDeliveryFailures']) {
      template.hasResourceProperties('AWS::CloudWatch::Alarm', {
        Namespace: 'AWS/Lambda', MetricName: metric,
        Dimensions: [{ Name: 'FunctionName', Value: { Ref: fn.id } }],
      });
    }
    assert.ok(policyFor(fn.id).some(statement =>
      JSON.stringify(statement.Action).includes('sqs:SendMessage')
      && JSON.stringify(statement.Resource).includes(queueId)));
  }
});

test('reconciler runs every minute with a bounded invocation and retry destination', () => {
  const reconciler = handler('backend.events.reconcile');
  assert.equal(reconciler.Timeout, 60);
  template.hasResourceProperties('AWS::Events::Rule', {
    ScheduleExpression: 'rate(1 minute)',
    Targets: [Match.objectLike({ Arn: { 'Fn::GetAtt': [reconciler.id, 'Arn'] },
      DeadLetterConfig: { Arn: Match.anyValue() },
      RetryPolicy: { MaximumEventAgeInSeconds: 300, MaximumRetryAttempts: 2 } })],
  });
  template.hasResourceProperties('AWS::Lambda::EventInvokeConfig', {
    FunctionName: { Ref: reconciler.id }, MaximumRetryAttempts: 2, MaximumEventAgeInSeconds: 300,
  });
  assert.ok(reconciler.DeadLetterConfig.TargetArn);
});

test('runtime can persist run transactions and messages and read agent records', () => {
  const runtime = resources.Runtime.Properties;
  const commands = handler('backend.lambda_handler.handler');
  assert.deepEqual(runtime.EnvironmentVariables.TABLE_NAME, { Ref: 'MetadataBDB8F4DB' });
  assert.deepEqual(runtime.EnvironmentVariables.RUN_TABLE_NAME, commands.Environment.Variables.RUN_TABLE_NAME);
  const statements = resources.RuntimeRoleDefaultPolicy31263C89.Properties.PolicyDocument.Statement;
  for (const action of ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem']) {
    assert.ok(statements.some((statement: { Action: string[]; Resource: unknown }) =>
      statement.Action.includes(action) && JSON.stringify(statement.Resource).includes('MetadataBDB8F4DB')), action);
  }
  const runTableId = runtime.EnvironmentVariables.RUN_TABLE_NAME.Ref;
  for (const action of ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
    'dynamodb:DeleteItem', 'dynamodb:ConditionCheckItem']) {
    assert.ok(statements.some((statement: { Action: string[]; Resource: unknown }) =>
      statement.Action.includes(action) && JSON.stringify(statement.Resource).includes(runTableId)), action);
  }
});

test('all gateway handlers share one zip asset and lock-pinned layer and preserve the backend environment', () => {
  const backend = functions.filter(([, resource]) => resource.Properties.Handler?.startsWith('backend.'));
  assert.equal(backend.length, 5);
  const layers = Object.entries(template.findResources('AWS::Lambda::LayerVersion', {
    Properties: Match.objectLike({ Description: Match.stringLikeRegexp('uv\\.lock') }),
  }));
  assert.equal(layers.length, 1);
  const [[layerId, layer]] = layers;
  assert.deepEqual(layer.Properties.CompatibleArchitectures, ['arm64']);
  assert.deepEqual(layer.Properties.CompatibleRuntimes, ['python3.12']);
  assert.equal(new Set(backend.map(([, resource]) => JSON.stringify(resource.Properties.Code))).size, 1);
  for (const [, resource] of backend) {
    assert.equal(resource.Properties.PackageType, undefined); // Zip, not a container image.
    assert.equal(resource.Properties.Runtime, 'python3.12');
    assert.deepEqual(resource.Properties.Architectures, ['arm64']);
    assert.deepEqual(resource.Properties.Layers, [{ Ref: layerId }]);
    assert.ok(resource.Properties.Timeout <= 60);
    const environment = resource.Properties.Environment.Variables;
    for (const name of ['TABLE_NAME', 'RUN_TABLE_NAME', 'DIRECTORY_INDEX_NAME', 'USER_POOL_ID',
      'HUMAN_CLIENT_ID', 'AGENT_CLIENT_ID', 'PUBLIC_URL', 'COGNITO_DOMAIN', 'COGNITO_ISSUER',
      'SECRET_PREFIX', 'AGENT_ROOT', 'RUNTIME_ARN', 'DEFAULT_TEAM_ID']) assert.ok(environment[name], name);
    assert.equal(environment.AWS_REGION, undefined); // Reserved Lambda-provided variable.
  }
  template.resourceCountIs('AWS::ECR::Repository', 0);
  const environment = handler('backend.lambda_handler.handler').Environment.Variables;
  assert.ok(environment.EVENTS_HTTP_DOMAIN);
  assert.ok(environment.EVENTS_REALTIME_DOMAIN);
});

test('the dependency layer installs exactly what uv.lock pins for the target platform', () => {
  const [exportCommand, install] = lambdaInstallCommands('/src', '/out');
  for (const flag of ['--frozen', '--only-group', 'lambda', '--no-emit-project']) {
    assert.ok(exportCommand.includes(flag), flag);
  }
  for (const flag of ['--require-hashes', '--no-deps', '--only-binary', 'aarch64-manylinux2014']) {
    assert.ok(install.includes(flag), flag);
  }
  assert.equal(install[install.indexOf('--target') + 1], '/out/python');
  assert.equal(install[install.indexOf('--python-version') + 1], '3.12');
  // Every third-party module the handlers import must be declared in the layer group.
  const pyproject = readFileSync(path.join(__dirname, '..', 'pyproject.toml'), 'utf8');
  const group = pyproject.match(/^lambda = \[\n([\s\S]*?)^\]/m)?.[1] ?? '';
  for (const name of ['boto3', 'fastapi', 'httpx', 'PyJWT[crypto]', 'mangum']) {
    assert.ok(group.includes(`"${name}"`), name);
  }
});

test('all Cognito clients explicitly exclude authorization attributes from self-service writes', () => {
  for (const [configured, count] of [
    [template, 2],
    [synth({ mobileCallbackUrls: ['agentsandbox://oauth/callback'] }), 3],
  ] as const) {
    const clients = configured.findResources('AWS::Cognito::UserPoolClient');
    assert.equal(Object.keys(clients).length, count);
    for (const [id, client] of Object.entries(clients)) {
      // An omitted or empty allowlist can restore Cognito's permissive defaults.
      // Keep this an exact standard-attribute allowlist so future custom fields are denied too.
      assert.deepEqual(client.Properties.WriteAttributes, ['email'], id);
    }
    const agent = clients.UsersAgentClient396CE365.Properties;
    assert.ok(agent.ExplicitAuthFlows.includes('ALLOW_ADMIN_USER_PASSWORD_AUTH'));
    const humans = Object.entries(clients)
      .filter(([id]) => id !== 'UsersAgentClient396CE365').map(([, client]) => client);
    assert.equal(humans.length, count - 1);
    for (const client of humans) assert.deepEqual(client.Properties.AllowedOAuthFlows, ['code']);
  }
});

test('mobile code-flow client is opt-in and its ID reaches backend handlers', () => {
  template.resourceCountIs('AWS::Cognito::UserPoolClient', 2);
  assert.equal(handler('backend.lambda_handler.handler').Environment.Variables.MOBILE_CLIENT_ID, undefined);
  const mobile = synth({ mobileCallbackUrls: ['agentsandbox://oauth/callback'] });
  mobile.resourceCountIs('AWS::Cognito::UserPoolClient', 3);
  const clients = mobile.findResources('AWS::Cognito::UserPoolClient', {
    Properties: Match.objectLike({ CallbackURLs: ['agentsandbox://oauth/callback'],
      GenerateSecret: false, AllowedOAuthFlows: ['code'] }),
  });
  assert.equal(Object.keys(clients).length, 1);
  const clientId = Object.keys(clients)[0];
  for (const resource of Object.values(mobile.findResources('AWS::Lambda::Function', {
    Properties: Match.objectLike({ Handler: Match.stringLikeRegexp('^backend\\.') }),
  }))) assert.deepEqual(resource.Properties.Environment.Variables.MOBILE_CLIENT_ID, { Ref: clientId });
});

test('runtime exports metadata-only ADOT traces and enables service trace delivery', () => {
  const environment = resources.Runtime.Properties.EnvironmentVariables;
  assert.equal(environment.AGENT_OBSERVABILITY_ENABLED, 'true');
  assert.equal(environment.OTEL_SERVICE_NAME, 'agent-harness');
  assert.equal(environment.UNIFIED_TRACES_DESTINATION_ENABLED, 'true');
  assert.equal(environment.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, 'https://xray.eu-west-1.amazonaws.com/v1/traces');
  assert.equal(environment.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT, 'false');
  assert.equal(environment.OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED, 'false');
  assert.equal(environment.OTEL_TRACES_SAMPLER, 'always_on');
  template.hasResourceProperties('AWS::Logs::DeliverySource', {
    LogType: 'TRACES', ResourceArn: { 'Fn::GetAtt': ['Runtime', 'AgentRuntimeArn'] },
  });
  template.hasResourceProperties('AWS::Logs::DeliveryDestination', { DeliveryDestinationType: 'XRAY' });
  template.resourceCountIs('AWS::Logs::Delivery', 1);
  const policy = resources.RuntimeRoleDefaultPolicy31263C89.Properties.PolicyDocument.Statement;
  assert.ok(policy.some((statement: { Action: string[]; Resource: unknown }) =>
    statement.Action.includes('logs:PutResourcePolicy') && statement.Resource !== '*'));
  assert.ok(policy.some((statement: { Action: string[]; Resource: unknown }) =>
    statement.Action.includes('xray:PutTraceSegments') && statement.Action.includes('xray:PutTelemetryRecords')));
});
