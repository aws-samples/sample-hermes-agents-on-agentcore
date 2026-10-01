import { App, Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { ProbeStack } from './probe-stack.js';
import { PortalStack } from './portal-stack.js';

const app = new App();
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
const options = {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  vpcId: app.node.tryGetContext('vpcId') as string | undefined,
  subnetIds: (app.node.tryGetContext('subnetIds') as string | undefined)?.split(','),
};
if (app.node.tryGetContext('stage') === 'portal') {
  const portal = new PortalStack(app, 'AgentSandboxPortal', options);
  NagSuppressions.addStackSuppressions(portal, [
    { id: 'AwsSolutions-VPC7', reason: 'This sample omits VPC Flow Logs to avoid retaining network metadata by default.' },
    { id: 'AwsSolutions-COG2', reason: 'MFA enrollment is an identity-provider policy selected by each deployment.' },
    { id: 'AwsSolutions-COG8', reason: 'Cognito Plus is an optional paid tier outside this sample deployment.' },
    { id: 'AwsSolutions-IAM4', reason: 'Lambda service roles use AWS managed execution policies supplied by CDK.' },
    { id: 'AwsSolutions-IAM5', reason: 'Generated resource names require scoped wildcard access; review the synthesized policies before deployment.' },
    { id: 'AwsSolutions-L1', reason: 'The sample pins its Lambda runtime for reproducible compatibility.' },
    { id: 'AwsSolutions-APIG1', reason: 'API access-log retention and destination are deployment-specific operational choices.' },
    { id: 'AwsSolutions-APIG4', reason: 'The FastAPI backend enforces Cognito session and bearer-token authorization.' },
    { id: 'AwsSolutions-S1', reason: 'Static-site bucket access is logged at CloudFront when a deployment enables logging.' },
    { id: 'AwsSolutions-CFR1', reason: 'Geographic restrictions are deployment-specific access policy.' },
    { id: 'AwsSolutions-CFR2', reason: 'WAF association is deployment-specific edge protection policy.' },
    { id: 'AwsSolutions-CFR3', reason: 'CloudFront log delivery and retention are deployment-specific operational choices.' },
    { id: 'AwsSolutions-CFR4', reason: 'The default CloudFront certificate is used for the sample distribution.' },
    { id: 'AwsSolutions-SQS3', reason: 'These queues are themselves dead-letter queues for DynamoDB stream consumers.' },
  ]);
  NagSuppressions.addResourceSuppressionsByPath(portal, `/${portal.stackName}/OriginVerifySecret/Resource`, [
    { id: 'AwsSolutions-SMG4', reason: 'CloudFront receives this origin-verify value only at deployment; rotate it by redeploying, not automatically.' },
  ]);
} else {
  const probe = new ProbeStack(app, 'AgentSandboxIsolationProbe', options);
  NagSuppressions.addStackSuppressions(probe, [
    { id: 'AwsSolutions-VPC7', reason: 'This disposable isolation probe omits VPC Flow Logs to avoid retaining network metadata.' },
    { id: 'AwsSolutions-IAM5', reason: 'The runtime role uses a scoped wildcard for generated runtime log groups.' },
  ]);
}
