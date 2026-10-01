from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import boto3
import pytest
from moto import mock_aws

from common.runs import RunStore


@pytest.fixture(autouse=True)
def adapter_manifest(monkeypatch):
    monkeypatch.setenv('AGENT_ADAPTER_MANIFEST',
                       str(Path(__file__).resolve().parents[1] / 'agents/hermes/manifest.json'))


@pytest.fixture
def durable():
    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        for name in ('runs', 'metadata'):
            extra = {} if name == 'metadata' else {'GlobalSecondaryIndexes': [{
                'IndexName': 'WorkByStatus', 'KeySchema': [
                    {'AttributeName': 'status', 'KeyType': 'HASH'},
                    {'AttributeName': 'updated_at', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'ALL'}}]}
            attributes = [{'AttributeName': 'pk', 'AttributeType': 'S'},
                          {'AttributeName': 'sk', 'AttributeType': 'S'}]
            if name == 'runs':
                attributes += [{'AttributeName': 'status', 'AttributeType': 'S'},
                               {'AttributeName': 'updated_at', 'AttributeType': 'N'}]
            resource.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                                  KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'},
                                             {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                                  AttributeDefinitions=attributes, **extra)
        store = RunStore(resource, 'runs', 'metadata')
        agent = {'id': str(uuid4()), 'sub': str(uuid4()), 'team_id': str(uuid4())}
        conversation = {'id': str(uuid4()), 'runtime_session_id': str(uuid4())}
        user = {'sub': str(uuid4()), 'username': 'alice'}
        conversation['owner_sub'] = user['sub']
        store.metadata.put_item(Item={'pk': 'AGENT#' + agent['id'],
                                      'sk': 'CONV#' + conversation['id'], **conversation})
        yield SimpleNamespace(store=store, agent=agent, conversation=conversation, user=user)
