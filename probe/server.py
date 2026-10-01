"""IAM-authenticated AgentCore feasibility probe. Never executes agent/user code."""

import json
import os
import platform
import socket
import subprocess
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from probe.sandbox import run

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = threading.Lock()
AGENTS = {
    'a': '11111111-1111-4111-8111-111111111111',
    'b': '22222222-2222-4222-8222-222222222222',
}


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    agent: Literal['a', 'b'] = 'a'
    operation: Literal['write', 'read'] = 'write'
    marker: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9-]+$')


@app.get('/ping')
def ping():
    # A healthy diagnostic server can report sandbox unavailability. This is NOT
    # the production agent's readiness policy; no Hermes code exists in this image.
    return {'status': 'Healthy'}


@app.post('/invocations')
def invoke(request: Request):
    with lock:
        return diagnose(request)


def diagnose(request: Request):
    root = Path(os.environ.get('PROBE_ROOT', '/mnt/agents'))
    root.mkdir(exist_ok=True)
    for sub in AGENTS.values():
        directory = root / sub
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError('Probe directory cannot be a symlink')
        (directory / 'secret').write_text('synthetic-canary-' + sub)
    sub = AGENTS[request.agent]
    sibling = AGENTS['b' if request.agent == 'a' else 'a']
    sibling_secret = str(root / sibling / 'secret')
    escape = root / sub / 'escape'
    if not escape.is_symlink():
        escape.symlink_to(sibling_secret)
    namespaces = {key: os.readlink(f'/proc/self/ns/{key}')
                  for key in ('user', 'mnt', 'pid', 'net', 'ipc', 'uts')}
    addresses = []
    efs_dns = os.environ.get('EFS_DNS')
    if efs_dns:
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(efs_dns, 2049,
                                                                      socket.AF_INET)})
    details = {
        'kernel': platform.release(), 'architecture': platform.machine(), 'uid': os.getuid(),
        'bubblewrap': subprocess.run(['/usr/bin/bwrap', '--version'], capture_output=True,
                                    text=True, check=True).stdout.strip(),
        'outer_namespaces': namespaces,
    }
    payload = {
        'root': str(root), 'sibling_secret': sibling_secret, 'namespaces': namespaces,
        'supervisor_pid': os.getpid(), 'nfs_addresses': addresses,
        'operation': request.operation, 'marker': request.marker,
    }
    try:
        result = run(root, sub, json.dumps(payload))
    except subprocess.TimeoutExpired:
        return {'passed': False, 'stage': 'sandbox_launch', 'error': 'Sandbox timed out', **details}
    if result.returncode:
        return {'passed': False, 'stage': 'sandbox_launch', 'exit_code': result.returncode,
                'stderr': result.stderr[:4000], **details}
    report = json.loads(result.stdout)
    return {'stage': 'sandbox_assertions', **details, **report}
