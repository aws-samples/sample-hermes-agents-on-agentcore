"""Runs in the built Linux container: exercise real Hermes through a fake Bedrock response.

No external credentials. Tests the entire sandbox/worker/broker/checkpoint pipeline.
"""

import asyncio
import io
import json
import os
import tempfile
import uuid
from pathlib import Path

from runtime import server


class FakeBedrock:
    def invoke_model(self, **kwargs):
        body = json.loads(kwargs['body'])
        assert body['anthropic_version'] == 'bedrock-2023-05-31'
        if body.get('tools'):
            assert body['max_tokens'] == 256
        used_tool = any(isinstance(message.get('content'), list) and
                        any(block.get('type') == 'tool_result' for block in message['content'])
                        for message in body['messages'])
        content = [{'type': 'text', 'text': 'Sandbox integration succeeded.'}] if used_tool else [
            {'type': 'tool_use', 'id': 'tool_test', 'name': 'terminal',
             'input': {'command': 'test "$SECURITY_TEST_MODE" = true && printf sandbox-canary > proof.txt'}},
        ]
        return {'body': io.BytesIO(json.dumps({
            'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-4-6',
            'content': content,
            'stop_reason': 'end_turn' if used_tool else 'tool_use', 'stop_sequence': None,
            'usage': {'input_tokens': 10, 'output_tokens': 6},
        }).encode())}

    def invoke_model_with_response_stream(self, **kwargs):
        response = json.loads(self.invoke_model(**kwargs)['body'].read())
        block = response['content'][0]
        reason = response['stop_reason']
        response['content'] = []
        response['stop_reason'] = None
        if block['type'] == 'text':
            start = {'type': 'text', 'text': ''}
            delta = {'type': 'text_delta', 'text': block['text']}
        else:
            start = {**block, 'input': {}}
            delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
        events = [
            {'type': 'message_start', 'message': response},
            {'type': 'content_block_start', 'index': 0, 'content_block': start},
            {'type': 'content_block_delta', 'index': 0, 'delta': delta},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None},
             'usage': {'output_tokens': 6}},
            {'type': 'message_stop'},
        ]

        class Stream:
            def __iter__(self):
                return iter({'chunk': {'bytes': json.dumps(event).encode()}} for event in events)

            def close(self):
                pass

        return {'body': Stream()}


async def run():
    os.environ['MODEL_ALIAS'] = 'claude-sonnet-4-6'
    os.environ['BEDROCK_MODEL_ID'] = 'eu.anthropic.claude-sonnet-4-6'
    with tempfile.TemporaryDirectory() as root:
        server.ROOT = Path(root)
        server.bedrock = FakeBedrock()
        server.worker = None
        server.worker_lock = asyncio.Lock()
        sub = str(uuid.uuid4())
        (server.ROOT / sub / 'workspace').mkdir(parents=True)
        (server.ROOT / sub / 'hermes').mkdir()
        payload = server.Invocation(
            conversation_id=uuid.uuid4(), team_id=uuid.uuid4(),
            security_test_mode=True, input_limit_value=256,
            input_limit_unit='tokens', max_output_tokens=256, message='Hello')
        events = []
        async for item in server.execute(sub, payload):
            print(item, flush=True)
            events.append(json.loads(item[6:]))
        assert events[-1]['type'] == 'complete', events
        first_pid = events[-1]['pid']
        first_worker = events[-1]['worker_instance_id']
        oversized = payload.model_copy(update={'message': 'x' * 2000})
        rejected = []
        async for item in server.execute(sub, oversized):
            print(item, flush=True)
            rejected.append(json.loads(item[6:]))
        assert rejected[-1]['type'] == 'error'
        assert rejected[-1]['fatal'] is False
        assert server.worker.process.returncode is None
        second_events = []
        async for item in server.execute(sub, payload):
            print(item, flush=True)
            second_events.append(json.loads(item[6:]))
        assert second_events[-1]['type'] == 'complete', second_events
        assert second_events[-1]['pid'] == first_pid
        assert second_events[-1]['worker_instance_id'] == first_worker
        assert (server.ROOT / '.control' / sub / f'{payload.conversation_id}.db').stat().st_size > 0
        assert (server.ROOT / sub / 'workspace' / 'proof.txt').read_text() == 'sandbox-canary'
        oversized_mb = payload.model_copy(update={
            'input_limit_value': 1, 'input_limit_unit': 'mb',
            'message': 'x' * (1024 * 1024 + 1),
        })
        mb_rejected = []
        async for item in server.execute(sub, oversized_mb):
            mb_rejected.append(json.loads(item[6:]))
        assert mb_rejected[-1]['type'] == 'error'
        assert 'MB' in mb_rejected[-1]['message']
        await server.reset_worker()


if __name__ == '__main__':
    asyncio.run(run())
