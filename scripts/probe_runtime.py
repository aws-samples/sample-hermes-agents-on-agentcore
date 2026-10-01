"""Invoke the deployed probe; save evidence and stop sessions even on failure."""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config


def run_probe(session: boto3.Session, stack_name: str) -> dict:
    config = Config(retries={'mode': 'adaptive', 'total_max_attempts': 3},
                    connect_timeout=10, read_timeout=180)
    cfn = session.client('cloudformation', config=config)
    control = session.client('bedrock-agentcore-control', config=config)
    # Invocations perform writes. Avoid automatically replaying a request on an ambiguous timeout.
    runtime = session.client('bedrock-agentcore', config=config.merge(
        Config(retries={'mode': 'standard', 'total_max_attempts': 1})))
    stack = cfn.describe_stacks(StackName=stack_name)['Stacks'][0]
    outputs = {entry['OutputKey']: entry['OutputValue'] for entry in stack['Outputs']}
    runtime_id, arn = outputs['RuntimeId'], outputs['RuntimeArn']
    endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName='DEFAULT')
    if endpoint['status'] != 'READY':
        raise RuntimeError(f"Runtime endpoint is not ready: {endpoint['status']}")
    evidence = {
        'timestamp': datetime.now(UTC).isoformat(),
        'region': session.region_name, 'stack': stack_name, 'outputs': outputs,
        'runtime_version': endpoint.get('agentRuntimeVersion'), 'runs': [],
    }
    marker = str(uuid.uuid4())
    # New microVM sessions, same agent folder: prove the persistence is EFS, not process state.
    for agent, operation in [('a', 'write'), ('b', 'write'), ('a', 'read')]:
        session_id = str(uuid.uuid4())
        try:
            response = runtime.invoke_agent_runtime(
                agentRuntimeArn=arn, runtimeSessionId=session_id,
                contentType='application/json', accept='application/json',
                payload=json.dumps({'agent': agent, 'operation': operation,
                                    'marker': marker}).encode(),
            )
            with response['response'] as body:
                report = json.loads(body.read())
            evidence['runs'].append({'agent': agent, 'operation': operation,
                                     'session_id': session_id, 'report': report})
            if not report.get('passed'):
                break
        finally:
            runtime.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id)
    evidence['passed'] = len(evidence['runs']) == 3 and all(
        run['report'].get('passed') for run in evidence['runs'])
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='AgentSandboxIsolationProbe')
    parser.add_argument('--profile')
    parser.add_argument('--region')
    parser.add_argument('--output', type=Path, default=Path('.deployment/probe-result.json'))
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    evidence = run_probe(session, args.stack)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2))
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
