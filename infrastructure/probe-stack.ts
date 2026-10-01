import * as path from 'node:path';
import {
  ArnFormat, CfnOutput, RemovalPolicy, Stack, StackProps, Tags,
  aws_bedrockagentcore as agentcore, aws_ec2 as ec2, aws_efs as efs,
  aws_ecr_assets as assets, aws_iam as iam,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface ProbeStackProps extends StackProps {
  readonly vpcId?: string;
  readonly subnetIds?: string[];
}

export class ProbeStack extends Stack {
  constructor(scope: Construct, id: string, props: ProbeStackProps = {}) {
    super(scope, id, props);
    Tags.of(this).add('Project', 'agent-sandbox-agentcore-portal');
    Tags.of(this).add('Stage', 'isolation-probe');

    if (props.subnetIds && !props.vpcId) throw new Error('subnetIds requires vpcId');
    const vpc = props.vpcId
      ? ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: props.vpcId })
      : new ec2.Vpc(this, 'Vpc', {
        maxAzs: 2, natGateways: 1,
        subnetConfiguration: [
          { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
          { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
        ],
      });
    const subnets = props.subnetIds
      ? vpc.selectSubnets({ subnetFilters: [ec2.SubnetFilter.byIds(props.subnetIds)] }).subnets
      : vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnets;
    if (!subnets.length || (props.subnetIds && subnets.length !== props.subnetIds.length)) {
      throw new Error('Select existing private subnets in the supplied VPC, with HTTPS egress');
    }
    const runtimeSg = new ec2.SecurityGroup(this, 'RuntimeSecurityGroup', {
      vpc, allowAllOutbound: false,
      description: 'Probe runtime: HTTPS services and EFS only',
    });
    runtimeSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'AWS HTTPS services');
    const storageSg = new ec2.SecurityGroup(this, 'StorageSecurityGroup', {
      vpc, allowAllOutbound: false,
    });
    storageSg.addIngressRule(runtimeSg, ec2.Port.tcp(2049), 'NFS from runtime only');
    runtimeSg.addEgressRule(storageSg, ec2.Port.tcp(2049), 'EFS mount');

    const fs = new efs.FileSystem(this, 'FileSystem', {
      vpc, vpcSubnets: { subnets }, securityGroup: storageSg,
      encrypted: true, enableAutomaticBackups: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const accessPoint = fs.addAccessPoint('AgentRoot', {
      path: '/agents', posixUser: { uid: '1000', gid: '1000' },
      createAcl: { ownerUid: '1000', ownerGid: '1000', permissions: '0700' },
    });
    fs.addToResourcePolicy(new iam.PolicyStatement({
      effect: iam.Effect.DENY, principals: [new iam.AnyPrincipal()],
      actions: ['elasticfilesystem:ClientMount', 'elasticfilesystem:ClientWrite'],
      resources: ['*'], conditions: { Bool: { 'aws:SecureTransport': 'false' } },
    }));

    const image = new assets.DockerImageAsset(this, 'ProbeImage', {
      directory: path.join(__dirname, '..', 'probe'),
      platform: assets.Platform.LINUX_ARM64,
      exclude: ['__pycache__', '*.pyc'],
    });
    const runtimeName = 'agent_sandbox_isolation_probe';
    const runtimeArnPattern = this.formatArn({
      service: 'bedrock-agentcore', resource: 'runtime',
      resourceName: `${runtimeName}-*`, arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
    });
    const role = new iam.Role(this, 'RuntimeRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': runtimeArnPattern },
        },
      }),
    });
    image.repository.grantPull(role);
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['elasticfilesystem:ClientMount', 'elasticfilesystem:ClientWrite'],
      resources: [fs.fileSystemArn],
      conditions: { ArnEquals: { 'elasticfilesystem:AccessPointArn': accessPoint.accessPointArn } },
    }));
    // Required by AgentCore's filesystem validation in addition to NFS client permissions.
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['elasticfilesystem:DescribeAccessPoints', 'elasticfilesystem:DescribeMountTargets'],
      resources: ['*'],
    }));
    // Describe/ENI actions do not all support resource-level scoping. AgentCore
    // needs these to establish the VPC attachment, never exposed to sandbox code.
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeNetworkInterfaces', 'ec2:DescribeVpcs', 'ec2:DescribeSubnets',
        'ec2:DescribeSecurityGroups', 'ec2:CreateNetworkInterface',
        'ec2:DeleteNetworkInterface', 'ec2:CreateNetworkInterfacePermission'],
      resources: ['*'],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents',
        'logs:DescribeLogStreams'],
      resources: [this.formatArn({ service: 'logs', resource: 'log-group',
        resourceName: '/aws/bedrock-agentcore/runtimes/agent_sandbox_isolation_probe-*',
        arnFormat: ArnFormat.COLON_RESOURCE_NAME })],
    }));

    const runtime = new agentcore.CfnRuntime(this, 'Runtime', {
      agentRuntimeName: runtimeName,
      description: 'Fixed diagnostic probe; IAM authenticated; no Hermes/user execution',
      agentRuntimeArtifact: { containerConfiguration: { containerUri: image.imageUri } },
      roleArn: role.roleArn,
      protocolConfiguration: 'HTTP',
      networkConfiguration: {
        networkMode: 'VPC',
        networkModeConfig: {
          subnets: subnets.map(s => s.subnetId), securityGroups: [runtimeSg.securityGroupId],
        },
      },
      filesystemConfigurations: [{ efsAccessPoint: {
        accessPointArn: accessPoint.accessPointArn, mountPath: '/mnt/agents',
      } }],
      lifecycleConfiguration: { idleRuntimeSessionTimeout: 60, maxLifetime: 600 },
      environmentVariables: {
        PROBE_ROOT: '/mnt/agents',
        EFS_DNS: `${fs.fileSystemId}.efs.${this.region}.${this.urlSuffix}`,
      },
    });
    runtime.node.addDependency(fs.mountTargetsAvailable);
    runtime.node.addDependency(role);
    new CfnOutput(this, 'RuntimeArn', { value: runtime.attrAgentRuntimeArn });
    new CfnOutput(this, 'RuntimeId', { value: runtime.attrAgentRuntimeId });
    new CfnOutput(this, 'FileSystemId', { value: fs.fileSystemId });
    new CfnOutput(this, 'AccessPointArn', { value: accessPoint.accessPointArn });
    new CfnOutput(this, 'VpcId', { value: vpc.vpcId });
    new CfnOutput(this, 'ImageUri', { value: image.imageUri });
  }
}
