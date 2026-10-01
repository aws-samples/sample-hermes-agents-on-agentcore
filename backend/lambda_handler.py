"""The existing HTTP application, invoked only for bounded command/read requests."""

import hmac
import json
import os
from functools import lru_cache

import boto3
from mangum import Mangum

from backend.app import app

# Must match ORIGIN_VERIFY_HEADER in infrastructure/portal-stack.ts. API Gateway
# HTTP APIs deliver header names in lowercase.
ORIGIN_VERIFY_HEADER = 'x-origin-verify'

_app = Mangum(app, lifespan='off')


@lru_cache(maxsize=1)
def origin_secret() -> str:
    """Fetched once per execution environment; CloudFront only learns new values on deploy."""
    arn = os.environ.get('ORIGIN_SECRET_ARN')
    if not arn:
        raise RuntimeError('ORIGIN_SECRET_ARN is not configured')
    value = boto3.client('secretsmanager').get_secret_value(SecretId=arn)['SecretString']
    if not value:
        raise RuntimeError('Origin verification secret is empty')
    return value


def _forbidden():
    return {'statusCode': 403, 'isBase64Encoded': False,
            'headers': {'content-type': 'application/json', 'cache-control': 'no-store'},
            'body': json.dumps({'detail': 'Forbidden'})}


def handler(event, context):
    # Reject anything that did not come through CloudFront before doing any app work.
    headers = {str(key).lower(): value for key, value in (event.get('headers') or {}).items()}
    supplied = headers.get(ORIGIN_VERIFY_HEADER)
    if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode(), origin_secret().encode()):
        return _forbidden()
    # Keep the secret out of the application (and anything it might log or echo).
    event = {**event, 'headers': {key: value for key, value in event['headers'].items()
                                  if str(key).lower() != ORIGIN_VERIFY_HEADER}}
    return _app(event, context)
