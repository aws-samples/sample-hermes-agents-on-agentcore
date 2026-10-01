"""Offline Linux image smoke: the full harness with the snapshot-free Echo adapter.

Run in the Echo image with this script mounted at /app/check_echo_sandbox.py.
No AWS credentials, model calls or external network are used.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from runtime import server
from runtime.adapter import load_adapter


async def run():
    assert load_adapter().id == 'echo-v1', 'Build with AGENT_SOURCE=agents/echo'
    os.environ['MODEL_ALIAS'] = 'offline'
    os.environ['BEDROCK_MODEL_ID'] = 'offline'
    server.bedrock = object()  # Echo never invokes the model broker.
    server.worker_lock = asyncio.Lock()
    with tempfile.TemporaryDirectory() as root:
        server.ROOT = Path(root)
        sub = str(uuid4())
        (server.ROOT / sub / 'workspace').mkdir(parents=True)
        payload = server.Invocation(conversation_id=uuid4(), team_id=uuid4(), message='hello')
        instances = []
        try:
            for turn in range(1, 4):
                if turn == 3:
                    await server.reset_worker()
                events = [json.loads(item.removeprefix('data: '))
                          async for item in server.execute(sub, payload)]
                assert events[-1]['type'] == 'complete', events
                expected_turn = turn if turn < 3 else 1
                assert events[-1]['text'] == f'Turn {expected_turn}: hello', events
                instances.append(events[-1]['worker_instance_id'])
            assert instances[0] == instances[1] and instances[1] != instances[2]
            assert not (server.ROOT / sub / 'hermes').exists()
            assert not list(server.ROOT.rglob('*.db'))
            print(json.dumps({'adapter': 'echo-v1', 'sandbox': True,
                              'worker_reuse': True, 'snapshot_free_completion': True}))
        finally:
            await server.reset_worker()


if __name__ == '__main__':
    asyncio.run(run())
