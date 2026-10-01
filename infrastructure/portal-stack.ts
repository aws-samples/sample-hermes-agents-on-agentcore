import * as path from 'node:path';
import {
  ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags,
  aws_ec2 as ec2, aws_efs as efs, aws_iam as iam, aws_bedrockagentcore as agentcore,
  aws_cognito as cognito, aws_dynamodb as dynamodb,
  aws_ecr_assets as assets, aws_apigatewayv2 as apigateway,
  aws_apigatewayv2_integrations as integrations, aws_appsync as appsync,
  aws_events as events, aws_events_targets as targets,
  aws_cloudfront as cloudfront, aws_cloudfront_origins as origins,
  aws_s3 as s3, aws_s3_deployment as deployment, aws_logs as logs,
  aws_lambda as lambda, aws_secretsmanager as secretsmanager,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  PORTAL_LAMBDA_ARCHITECTURE, PORTAL_LAMBDA_RUNTIME, portalDependencyLayer, portalHandlerCode,
} from './lambda-bundling.js';
import { RunStreamConsumer, monitorFunction, failureQueue } from './run-stream-consumer.js';

// Must match backend/lambda_handler.py.
export const ORIGIN_VERIFY_HEADER = 'X-Origin-Verify';

export interface PortalStackProps extends StackProps {
  readonly vpcId?: string;
  readonly subnetIds?: string[];
}

export class PortalStack extends Stack {
  constructor(scope: Construct, id: string, props: PortalStackProps) {
    super(scope, id, props);
    Tags.of(this).add('Project', 'agent-sandbox-agentcore-portal');
    const vpc = props.vpcId
      ? ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: props.vpcId })
      : new ec2.Vpc(this, 'Vpc', { maxAzs: 2, natGateways: 1 });
    const subnets = vpc.selectSubnets(props.subnetIds
      ? { subnetFilters: [ec2.SubnetFilter.byIds(props.subnetIds)] }
      : { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnets;
    const runtimeSg = new ec2.SecurityGroup(this, 'RuntimeSg', { vpc, allowAllOutbound: false });
    const backendSg = new ec2.SecurityGroup(this, 'BackendSg', { vpc, allowAllOutbound: false });
    const storageSg = new ec2.SecurityGroup(this, 'StorageSg', { vpc, allowAllOutbound: false });
    for (const source of [runtimeSg, backendSg]) {
      source.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS egress');
      source.addEgressRule(storageSg, ec2.Port.tcp(2049), 'EFS');
      storageSg.addIngressRule(source, ec2.Port.tcp(2049));
    }
    const fs = new efs.FileSystem(this, 'AgentFiles', {
      vpc, vpcSubnets: { subnets }, securityGroup: storageSg, encrypted: true,
      enableAutomaticBackups: true, removalPolicy: RemovalPolicy.RETAIN,
    });
    const access = fs.addAccessPoint('Root', {
      path: '/agents', posixUser: { uid: '1000', gid: '1000' },
      createAcl: { ownerUid: '1000', ownerGid: '1000', permissions: '0700' },
    });
    fs.addToResourcePolicy(new iam.PolicyStatement({
      effect: iam.Effect.DENY, principals: [new iam.AnyPrincipal()], resources: ['*'],
      actions: ['elasticfilesystem:ClientMount', 'elasticfilesystem:ClientWrite'],
      conditions: { Bool: { 'aws:SecureTransport': 'false' } },
    }));
    const pool = new cognito.UserPool(this, 'Users', {
      selfSignUpEnabled: false, signInAliases: { username: true },
      customAttributes: {
        teams: new cognito.StringAttribute({ mutable: true, minLen: 36, maxLen: 2048 }),
        security_test_mode: new cognito.StringAttribute({ mutable: true, minLen: 4, maxLen: 5 }),
        execution_mode: new cognito.StringAttribute({ mutable: true, minLen: 10, maxLen: 10 }),
        token_budget: new cognito.StringAttribute({ mutable: true, minLen: 1, maxLen: 10 }),
        input_limit_value: new cognito.StringAttribute({ mutable: true, minLen: 1, maxLen: 10 }),
        input_limit_unit: new cognito.StringAttribute({ mutable: true, minLen: 2, maxLen: 6 }),
        max_output_tokens: new cognito.StringAttribute({ mutable: true, minLen: 1, maxLen: 10 }),
      },
      passwordPolicy: { minLength: 14, requireDigits: true, requireLowercase: true,
        requireUppercase: true, requireSymbols: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    for (const group of ['Humans', 'Agents', 'Admins']) {
      new cognito.CfnUserPoolGroup(this, group, { userPoolId: pool.userPoolId, groupName: group });
    }
    const claims = new lambda.Function(this, 'TeamClaims', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, 'lambdas', 'team-claims')),
      timeout: Duration.seconds(5),
      description: 'Copies immutable team membership into ID and access tokens',
    });
    pool.addTrigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG, claims, cognito.LambdaVersion.V2_0);
    // Cognito defaults to client-write access to all custom attributes. Use a nonempty
    // profile-only allowlist on every client; team membership and agent settings must
    // only change through IAM-authorized administrative APIs, never UpdateUserAttributes.
    const clientWriteAttributes = new cognito.ClientAttributes().withStandardAttributes({ email: true });
    const agentClient = pool.addClient('AgentClient', {
      authFlows: { adminUserPassword: true }, generateSecret: false,
      writeAttributes: clientWriteAttributes,
      preventUserExistenceErrors: true, accessTokenValidity: Duration.minutes(15),
    });
    const domain = pool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: `agent-sandbox-${this.account}-${this.region}` },
    });
    const table = new dynamodb.Table(this, 'Metadata', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires', pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const directoryIndex = 'DirectoryByType';
    table.addGlobalSecondaryIndex({
      indexName: directoryIndex,
      partitionKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['id', 'name', 'created_at', 'team_id', 'status', 'entity', 'security_test_mode'],
    });
    const runs = new dynamodb.Table(this, 'Runs', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires', stream: dynamodb.StreamViewType.NEW_IMAGE,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    runs.addGlobalSecondaryIndex({
      indexName: 'WorkByStatus',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'updated_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    const issuer = `https://cognito-idp.${this.region}.${this.urlSuffix}/${pool.userPoolId}`;
    const agentImplementation = this.node.tryGetContext('agentImplementation') ?? 'hermes';
    if (typeof agentImplementation !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(agentImplementation)) {
      throw new Error('agentImplementation must name a directory under agents/');
    }
    // The harness image is framework-neutral; AGENT_SOURCE selects the adapter.
    const runtimeImage = new assets.DockerImageAsset(this, 'AgentSandboxImage', {
      directory: path.join(__dirname, '..'), file: 'runtime/Dockerfile',
      buildArgs: { AGENT_SOURCE: `agents/${agentImplementation}` },
      platform: assets.Platform.LINUX_ARM64,
      exclude: ['node_modules', 'cdk.out', '.venv', '.deployment', '.git', 'frontend', 'tests', 'probe'],
    });
    const runtimeRole = new iam.Role(this, 'RuntimeRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', { conditions: {
        StringEquals: { 'aws:SourceAccount': this.account },
        ArnLike: { 'aws:SourceArn': this.formatArn({ service: 'bedrock-agentcore', resource: 'runtime',
          resourceName: 'agent_sandbox_portal-*', arnFormat: ArnFormat.SLASH_RESOURCE_NAME }) },
      } }),
    });
    runtimeImage.repository.grantPull(runtimeRole);
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeNetworkInterfaces', 'ec2:DescribeVpcs', 'ec2:DescribeSubnets',
        'ec2:DescribeSecurityGroups', 'ec2:CreateNetworkInterface', 'ec2:DeleteNetworkInterface',
        'ec2:CreateNetworkInterfacePermission', 'elasticfilesystem:DescribeAccessPoints',
        'elasticfilesystem:DescribeMountTargets'], resources: ['*'],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:DescribeLogStreams',
        'logs:PutLogEvents', 'logs:PutResourcePolicy'],
      resources: [`arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/agent_sandbox_portal-*`],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      // X-Ray ingestion APIs do not support resource-level permissions.
      actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'], resources: ['*'],
    }));
    const modelId = (this.node.tryGetContext('modelId') as string | undefined) ?? 'eu.anthropic.claude-sonnet-4-6';
    const modelAlias = (this.node.tryGetContext('modelAlias') as string | undefined) ?? 'claude-sonnet-4-6';
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        `arn:${this.partition}:bedrock:${this.region}:${this.account}:inference-profile/${modelId}`,
        ...['eu-north-1', 'eu-west-3', 'eu-south-1', 'eu-south-2', 'eu-west-1', 'eu-central-1'].map(region =>
          `arn:${this.partition}:bedrock:${region}::foundation-model/anthropic.${modelAlias}`),
      ],
    }));
    const mount = (role: iam.IRole) => role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['elasticfilesystem:ClientMount', 'elasticfilesystem:ClientWrite'], resources: [fs.fileSystemArn],
      conditions: { ArnEquals: { 'elasticfilesystem:AccessPointArn': access.accessPointArn } },
    }));
    mount(runtimeRole);
    runs.grantReadWriteData(runtimeRole);
    // DynamoDB transactions authorize the underlying item actions, not a
    // separate TransactWriteItems IAM action. GetItem reads agent metadata.
    table.grant(runtimeRole, 'dynamodb:GetItem', 'dynamodb:PutItem',
      'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem');
    const runtime = new agentcore.CfnRuntime(this, 'Runtime', {
      agentRuntimeName: 'agent_sandbox_portal', roleArn: runtimeRole.roleArn,
      agentRuntimeArtifact: { containerConfiguration: { containerUri: runtimeImage.imageUri } },
      protocolConfiguration: 'HTTP',
      authorizerConfiguration: { customJwtAuthorizer: {
        discoveryUrl: `${issuer}/.well-known/openid-configuration`, allowedClients: [agentClient.userPoolClientId],
      } },
      requestHeaderConfiguration: { requestHeaderAllowlist: ['Authorization'] },
      networkConfiguration: { networkMode: 'VPC', networkModeConfig: {
        securityGroups: [runtimeSg.securityGroupId], subnets: subnets.map(s => s.subnetId),
      } },
      filesystemConfigurations: [{ efsAccessPoint: { accessPointArn: access.accessPointArn, mountPath: '/mnt/agents' } }],
      lifecycleConfiguration: { idleRuntimeSessionTimeout: 900, maxLifetime: 28800 },
      environmentVariables: { COGNITO_ISSUER: issuer, AGENT_CLIENT_ID: agentClient.userPoolClientId,
        AGENT_ROOT: '/mnt/agents', BEDROCK_MODEL_ID: modelId, MODEL_ALIAS: modelAlias,
        RUN_TABLE_NAME: runs.tableName, TABLE_NAME: table.tableName,
        AGENT_OBSERVABILITY_ENABLED: 'true', OTEL_SERVICE_NAME: 'agent-harness',
        OTEL_TRACES_EXPORTER: 'otlp', OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: `https://xray.${this.region}.amazonaws.com/v1/traces`,
        OTEL_TRACES_SAMPLER: 'always_on', OTEL_BSP_SCHEDULE_DELAY: '1000',
        OTEL_METRICS_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none',
        OTEL_AWS_APPLICATION_SIGNALS_ENABLED: 'false',
        OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED: 'false',
        OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'false',
        UNIFIED_TRACES_DESTINATION_ENABLED: 'true' },
    });
    runtime.node.addDependency(fs.mountTargetsAvailable);
    runtime.node.addDependency(runtimeRole);

    // Service-provided invocation spans complement the explicit agent/model/tool spans.
    // Account-level CloudWatch Transaction Search must be enabled (verified before deployment).
    const traceSource = new logs.CfnDeliverySource(this, 'RuntimeTraceSource', {
      name: `${this.stackName.toLowerCase()}-runtime-traces`,
      logType: 'TRACES', resourceArn: runtime.attrAgentRuntimeArn,
    });
    const traceDestination = new logs.CfnDeliveryDestination(this, 'RuntimeTraceDestination', {
      name: `${this.stackName.toLowerCase()}-runtime-xray`, deliveryDestinationType: 'XRAY',
    });
    new logs.CfnDelivery(this, 'RuntimeTraceDelivery', {
      deliverySourceName: traceSource.name, deliveryDestinationArn: traceDestination.attrArn,
    }).addResourceDependency(traceSource);

    // Create the API independently of its integration: CloudFront and the
    // OAuth client must exist before the commands function can reference them.
    const api = new apigateway.HttpApi(this, 'HttpApi');
    // The execute-api endpoint is public. CloudFront attaches this secret to every
    // origin request (overwriting any viewer-supplied copy) and the Commands Lambda
    // rejects requests without it, so CloudFront cannot be bypassed.
    const originSecret = new secretsmanager.Secret(this, 'OriginVerifySecret', {
      description: 'Shared secret CloudFront sends to the portal API origin',
      generateSecretString: { passwordLength: 64, excludePunctuation: true, includeSpace: false },
    });
    const bucket = new s3.Bucket(this, 'Frontend', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const apiBehavior: cloudfront.BehaviorOptions = {
      origin: new origins.HttpOrigin(`${api.apiId}.execute-api.${this.region}.${this.urlSuffix}`, {
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        readTimeout: Duration.seconds(30),
        // Resolves to a CloudFormation dynamic reference, not a plaintext template value.
        customHeaders: { [ORIGIN_VERIFY_HEADER]: originSecret.secretValue.unsafeUnwrap() },
      }),
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      // Includes cookies, Authorization, Origin (CSRF), and query strings;
      // replace the viewer Host with the execute-api origin's Host.
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
    };
    const distribution = new cloudfront.Distribution(this, 'Portal', {
      defaultRootObject: 'index.html',
      defaultBehavior: { origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS },
      additionalBehaviors: { '/api': apiBehavior, '/api/*': apiBehavior },
    });
    const publicUrl = `https://${distribution.distributionDomainName}`;
    const humanClient = pool.addClient('HumanClient', {
      generateSecret: false, preventUserExistenceErrors: true,
      writeAttributes: clientWriteAttributes,
      oAuth: { flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: [publicUrl + '/api/auth/callback'], logoutUrls: [publicUrl] },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });
    const mobileCallbackUrls = this.node.tryGetContext('mobileCallbackUrls') as unknown;
    if (mobileCallbackUrls !== undefined && (!Array.isArray(mobileCallbackUrls)
      || mobileCallbackUrls.some(url => typeof url !== 'string' || !url))) {
      throw new Error('mobileCallbackUrls must be an array of OAuth callback URLs');
    }
    const mobileClient = Array.isArray(mobileCallbackUrls) && mobileCallbackUrls.length > 0
      ? pool.addClient('MobileClient', {
        generateSecret: false, preventUserExistenceErrors: true,
        writeAttributes: clientWriteAttributes,
        oAuth: { flows: { authorizationCodeGrant: true },
          scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
          callbackUrls: mobileCallbackUrls },
        supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      }) : undefined;
    const secretPrefix = `${this.stackName}/credentials`;
    // AWS_REGION is supplied by Lambda itself and cannot be overridden.
    const commonEnvironment = {
      TABLE_NAME: table.tableName, RUN_TABLE_NAME: runs.tableName,
      DIRECTORY_INDEX_NAME: String(this.node.tryGetContext('directoryIndexEnabled')) === 'false'
        ? '' : directoryIndex,
      USER_POOL_ID: pool.userPoolId, HUMAN_CLIENT_ID: humanClient.userPoolClientId,
      AGENT_CLIENT_ID: agentClient.userPoolClientId, PUBLIC_URL: publicUrl,
      COGNITO_DOMAIN: domain.baseUrl(), COGNITO_ISSUER: issuer,
      SECRET_PREFIX: secretPrefix, AGENT_ROOT: '/mnt/agents', RUNTIME_ARN: runtime.attrAgentRuntimeArn,
      DEFAULT_TEAM_ID: '253e60ee-2188-58c1-9358-87b6e086dfae',
      ...(mobileClient ? { MOBILE_CLIENT_ID: mobileClient.userPoolClientId } : {}),
    };
    const dependencies = portalDependencyLayer(this, 'LambdaDependencies');
    const handlerCode = portalHandlerCode();
    // One code asset and one dependency layer are shared by every backend handler.
    const backendFunction = (id: string, handler: string, timeout: number,
      extra: Partial<lambda.FunctionProps> = {}) => new lambda.Function(this, id, {
      runtime: PORTAL_LAMBDA_RUNTIME, architecture: PORTAL_LAMBDA_ARCHITECTURE,
      code: handlerCode, handler, layers: [dependencies],
      memorySize: 512, timeout: Duration.seconds(timeout), environment: commonEnvironment,
      logGroup: new logs.LogGroup(this, `${id}Logs`, { retention: logs.RetentionDays.ONE_WEEK }),
      ...extra,
    });

    // Separate authorizer: never add Event API domains or grants to this
    // function, because the Event API already references its ARN.
    const authorizer = backendFunction('EventsAuthorizer', 'backend.events.authorize', 10);
    table.grantReadData(authorizer);
    runs.grantReadData(authorizer);
    authorizer.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminGetUser', 'cognito-idp:AdminListGroupsForUser'],
      resources: [pool.userPoolArn],
    }));
    const eventApi = new appsync.EventApi(this, 'RunEvents', {
      apiName: `${this.stackName}-runs`,
      authorizationConfig: {
        authProviders: [
          { authorizationType: appsync.AppSyncAuthorizationType.IAM },
          { authorizationType: appsync.AppSyncAuthorizationType.LAMBDA,
            lambdaAuthorizerConfig: { handler: authorizer, resultsCacheTtl: Duration.seconds(0) } },
        ],
        connectionAuthModeTypes: [appsync.AppSyncAuthorizationType.LAMBDA],
        defaultPublishAuthModeTypes: [appsync.AppSyncAuthorizationType.IAM],
        defaultSubscribeAuthModeTypes: [appsync.AppSyncAuthorizationType.LAMBDA],
      },
    });
    const namespace = eventApi.addChannelNamespace('runs', {
      authorizationConfig: {
        publishAuthModeTypes: [appsync.AppSyncAuthorizationType.IAM],
        subscribeAuthModeTypes: [appsync.AppSyncAuthorizationType.LAMBDA],
      },
    });
    const eventsEnvironment = { ...commonEnvironment,
      EVENTS_HTTP_DOMAIN: eventApi.httpDns, EVENTS_REALTIME_DOMAIN: eventApi.realtimeDns };
    const commands = backendFunction('Commands', 'backend.lambda_handler.handler', 29, {
      environment: { ...eventsEnvironment, ORIGIN_SECRET_ARN: originSecret.secretArn },
      memorySize: 1024,
      vpc, vpcSubnets: { subnets }, securityGroups: [backendSg],
      filesystem: lambda.FileSystem.fromEfsAccessPoint(access, '/mnt/agents'),
    });
    mount(commands.role!);
    commands.node.addDependency(fs.mountTargetsAvailable);
    table.grantReadWriteData(commands);
    runs.grantReadWriteData(commands);
    originSecret.grantRead(commands);
    commands.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminCreateUser', 'cognito-idp:AdminSetUserPassword',
        'cognito-idp:AdminGetUser', 'cognito-idp:AdminAddUserToGroup', 'cognito-idp:AdminRemoveUserFromGroup',
        'cognito-idp:AdminInitiateAuth', 'cognito-idp:AdminUpdateUserAttributes',
        'cognito-idp:AdminEnableUser', 'cognito-idp:AdminDisableUser', 'cognito-idp:AdminDeleteUser',
        'cognito-idp:AdminUserGlobalSignOut', 'cognito-idp:ListUsers', 'cognito-idp:AdminListGroupsForUser'],
      resources: [pool.userPoolArn],
    }));
    commands.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:CreateSecret', 'secretsmanager:GetSecretValue'],
      resources: [`arn:${this.partition}:secretsmanager:${this.region}:${this.account}:secret:${secretPrefix}/agents/*`],
    }));
    api.addRoutes({ path: '/api', methods: [apigateway.HttpMethod.ANY],
      integration: new integrations.HttpLambdaIntegration('ApiRoot', commands) });
    api.addRoutes({ path: '/api/{proxy+}', methods: [apigateway.HttpMethod.ANY],
      integration: new integrations.HttpLambdaIntegration('ApiProxy', commands) });

    const dispatcher = backendFunction('Dispatcher', 'backend.events.dispatch', 60);
    table.grantReadData(dispatcher);
    runs.grantReadWriteData(dispatcher);
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminInitiateAuth', 'cognito-idp:AdminGetUser',
        'cognito-idp:AdminListGroupsForUser'],
      resources: [pool.userPoolArn],
    }));
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [`arn:${this.partition}:secretsmanager:${this.region}:${this.account}:secret:${secretPrefix}/agents/*`],
    }));
    const publisher = backendFunction('Publisher', 'backend.events.publish', 30, {
      environment: eventsEnvironment,
    });
    namespace.grantPublish(publisher);
    new RunStreamConsumer(this, 'DispatchStream', { table: runs, handler: dispatcher, kind: 'run' });
    new RunStreamConsumer(this, 'PublishStream', { table: runs, handler: publisher, kind: 'event' });

    const reconcilerDlq = failureQueue(this, 'ReconcilerFailures');
    const reconciler = backendFunction('Reconciler', 'backend.events.reconcile', 60, {
      deadLetterQueue: reconcilerDlq, retryAttempts: 2, maxEventAge: Duration.minutes(5),
    });
    runs.grantReadWriteData(reconciler);
    table.grantReadWriteData(reconciler);
    new events.Rule(this, 'ReconcileSchedule', {
      schedule: events.Schedule.rate(Duration.minutes(1)),
      targets: [new targets.LambdaFunction(reconciler, {
        deadLetterQueue: reconcilerDlq, retryAttempts: 2, maxEventAge: Duration.minutes(5),
      })],
    });
    for (const [name, fn] of Object.entries({ Commands: commands, EventsAuthorizer: authorizer,
      Reconciler: reconciler })) monitorFunction(this, name, fn);
    new deployment.BucketDeployment(this, 'FrontendDeploy', {
      sources: [deployment.Source.asset(path.join(__dirname, '..', 'frontend', 'dist'))],
      destinationBucket: bucket, distribution, distributionPaths: ['/*'],
    });
    for (const [name, value] of Object.entries({ PortalUrl: publicUrl,
      UserPoolId: pool.userPoolId, RuntimeArn: runtime.attrAgentRuntimeArn,
      RuntimeId: runtime.attrAgentRuntimeId, FileSystemId: fs.fileSystemId,
      HumanClientId: humanClient.userPoolClientId, AgentClientId: agentClient.userPoolClientId,
       RunTableName: runs.tableName, EventsHttpDomain: eventApi.httpDns,
      EventsRealtimeDomain: eventApi.realtimeDns,
      ...(mobileClient ? { MobileClientId: mobileClient.userPoolClientId } : {}) })) {
      new CfnOutput(this, name, { value });
    }
  }
}
