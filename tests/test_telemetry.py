import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agents.hermes.agent_adapter import tool_trace_fields
from runtime import telemetry
from runtime.broker import Handler
from runtime.server import PersistentWorker


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, 'tracer', provider.get_tracer('test.hermes'))
    yield exporter
    provider.shutdown()


def run_record():
    return {'id': 'run', 'agent_id': 'agent', 'conversation_id': 'conversation',
            'runtime_session_id': 'runtime-session', 'execution_mode': 'concurrent',
            'status': 'running', 'message': 'PRIVATE_PROMPT_CANARY'}


def test_agent_tool_and_threaded_model_spans_share_trace_and_session_without_content(spans):
    run = run_record()
    with telemetry.run_trace(run):
        telemetry.observe({'event': 'model_iteration', 'iteration': 3, 'previous_round': 'PRIVATE_CANARY'})
        telemetry.observe({'event': 'tool_start', 'tool_call_id': 'tool-1',
                           'tool_name': 'terminal', 'arguments': 'PRIVATE_CANARY'})
        server = SimpleNamespace(trace_context=context.get_current(), model_id='model', model_alias='alias')

        def model_call():
            with telemetry.model_span(server, True, 1024) as (_, usage):
                telemetry.record_usage(usage, {'usage': {'input_tokens': 12, 'output_tokens': 4},
                                               'content': 'PRIVATE_CANARY'})

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(model_call).result()
        telemetry.observe({'event': 'tool_end', 'tool_call_id': 'tool-1',
                           'tool_name': 'terminal', 'result': 'PRIVATE_CANARY'})
        telemetry.outcome({'reason': 'max_iterations_reached', 'text': 'PRIVATE_CANARY'})
        run['status'] = 'partial'
    finished = spans.get_finished_spans()
    assert {span.name for span in finished} == {'invoke_agent hermes-v1', 'chat alias', 'execute_tool terminal'}
    agent = next(span for span in finished if span.name == 'invoke_agent hermes-v1')
    assert agent.status.status_code == StatusCode.ERROR
    assert agent.attributes['agentcore.iterations'] == 3
    assert agent.attributes['agentcore.model_calls'] == 1
    assert agent.attributes['gen_ai.usage.input_tokens'] == 12
    assert agent.attributes['gen_ai.usage.output_tokens'] == 4
    assert agent.attributes['gen_ai.response.finish_reasons'] == ('max_iterations_reached',)
    for span in finished:
        assert span.context.trace_id == agent.context.trace_id
        assert span.attributes['session.id'] == run['runtime_session_id']
        assert 'PRIVATE' not in span.to_json()
        if span is not agent:
            assert span.parent.span_id == agent.context.span_id


def test_unmatched_tools_are_not_misattributed_and_unclosed_tools_are_unresolved(spans):
    run = run_record()
    with telemetry.run_trace(run):
        telemetry.observe({'event': 'tool_end', 'tool_call_id': 'old', 'tool_name': 'terminal'})
        telemetry.observe({'event': 'tool_start', 'tool_call_id': 'new', 'tool_name': 'PRIVATE_NAME'})
        telemetry.observe({'event': 'tool_start', 'tool_call_id': 'new', 'tool_name': 'PRIVATE_NAME'})
        run['status'] = 'interrupted'
    tools = [span for span in spans.get_finished_spans() if span.name.startswith('execute_tool')]
    assert len(tools) == 1
    assert tools[0].name == 'execute_tool other'
    assert tools[0].attributes['agentcore.tool.lifecycle'] == 'unresolved'
    assert tools[0].attributes['agentcore.tool.timing'] == 'callback_observation'


def test_model_errors_record_type_not_exception_content(spans):
    run = run_record()
    with telemetry.run_trace(run):
        server = SimpleNamespace(trace_context=context.get_current(), model_id='model', model_alias='alias')
        with pytest.raises(RuntimeError, match='PRIVATE'), telemetry.model_span(server, False, 10):
            raise RuntimeError('PRIVATE_REQUEST_AND_CREDENTIAL')
        run['status'] = 'failed'
    model = next(span for span in spans.get_finished_spans() if span.name == 'chat alias')
    assert model.attributes['error.type'] == 'RuntimeError'
    assert model.status.status_code == StatusCode.ERROR
    assert 'PRIVATE' not in model.to_json()


def test_parent_trace_propagates_but_arbitrary_baggage_does_not(spans):
    parent = telemetry.extract_parent({
        'traceparent': '00-123456789012345678901234567890ab-1234567890123456-01',
        'authorization': 'Bearer PRIVATE', 'baggage': 'private=PRIVATE',
    })
    run = run_record()
    with telemetry.run_trace(run, parent):
        run['status'] = 'complete'
    span = spans.get_finished_spans()[0]
    assert span.context.trace_id == int('123456789012345678901234567890ab', 16)
    assert span.parent.span_id == int('1234567890123456', 16)
    assert 'PRIVATE' not in span.to_json()


@pytest.mark.parametrize('header_name', ['X-Amzn-Trace-Id', 'x-amzn-trace-id'])
def test_agentcore_xray_parent_is_preserved_and_preferred_over_w3c(spans, header_name):
    parent = telemetry.extract_parent({
        header_name: 'Root=1-6aafed7c-62f915046d00774d21e3e106;Parent=f922a576bf6e8486;Sampled=1',
        'traceparent': '00-123456789012345678901234567890ab-1234567890123456-01',
        'baggage': 'private=PRIVATE', 'authorization': 'Bearer PRIVATE',
    })
    run = run_record()
    with telemetry.run_trace(run, parent):
        run['status'] = 'complete'
    span = spans.get_finished_spans()[0]
    assert span.context.trace_id == int('6aafed7c62f915046d00774d21e3e106', 16)
    assert span.parent.span_id == int('f922a576bf6e8486', 16)
    assert span.attributes['agentcore.trace.parent_source'] == 'xray'
    assert 'PRIVATE' not in span.to_json()


def test_invalid_xray_header_falls_back_to_w3c(spans):
    parent = telemetry.extract_parent({
        'X-Amzn-Trace-Id': 'invalid',
        'Traceparent': '00-123456789012345678901234567890ab-1234567890123456-01',
    })
    with telemetry.run_trace(run_record(), parent):
        pass
    span = spans.get_finished_spans()[0]
    assert span.context.trace_id == int('123456789012345678901234567890ab', 16)
    assert span.attributes['agentcore.trace.parent_source'] == 'w3c'


def test_worker_phase_contains_model_and_tool_but_ends_before_finalization(spans):
    async def scenario():
        run = run_record()
        broker = SimpleNamespace(model_id='model', model_alias='alias', trace_context=None)
        worker = object.__new__(PersistentWorker)
        worker.servers = [broker]

        async def worker_events(_payload):
            with telemetry.model_span(broker, False, 10):
                pass
            yield {'type': 'telemetry', 'event': 'tool_start', 'tool_name': 'terminal', 'tool_call_id': 'tool-1'}
            yield {'type': 'telemetry', 'event': 'tool_end', 'tool_name': 'terminal', 'tool_call_id': 'tool-1'}
            yield {'type': 'complete', 'text': 'PRIVATE'}

        worker._events = worker_events
        with telemetry.run_trace(run) as root:
            async for data in worker.events(None):
                if data['type'] == 'telemetry':
                    telemetry.observe(data)
                else:
                    assert trace.get_current_span().get_span_context().span_id == root.span.get_span_context().span_id
                    with telemetry.phase('agentcore.run.finalize'):
                        run['status'] = 'complete'
        assert broker.trace_context is None

    asyncio.run(scenario())
    finished = {span.name: span for span in spans.get_finished_spans()}
    worker = finished['agentcore.worker.turn']
    root = finished['invoke_agent hermes-v1']
    assert worker.parent.span_id == root.context.span_id
    for name in ('chat alias', 'execute_tool terminal'):
        assert finished[name].parent.span_id == worker.context.span_id
    assert finished['agentcore.run.finalize'].parent.span_id == root.context.span_id
    assert worker.end_time <= finished['agentcore.run.finalize'].start_time
    assert all('PRIVATE' not in span.to_json() for span in finished.values())


def test_phase_errors_record_only_error_type(spans):
    with telemetry.run_trace(run_record()), pytest.raises(OSError), \
            telemetry.phase('agentcore.session.lookup'):
        raise OSError('PRIVATE storage detail')
    phase = next(span for span in spans.get_finished_spans() if span.name == 'agentcore.session.lookup')
    assert phase.status.status_code == StatusCode.ERROR
    assert phase.attributes['error.type'] == 'OSError'
    assert 'PRIVATE' not in phase.to_json()


def test_worker_telemetry_fields_are_bounded_and_do_not_stringify_objects():
    class Secret:
        def __str__(self):
            raise AssertionError('Must not stringify arbitrary worker objects')
    assert tool_trace_fields(Secret(), Secret()) is None
    assert tool_trace_fields('x' * 129, 'terminal') is None
    assert tool_trace_fields('safe-id', Secret()) == {'tool_call_id': 'safe-id', 'tool_name': 'other'}


@pytest.mark.parametrize('streaming', [True, False])
def test_broker_records_provider_usage_without_serializing_model_payloads(spans, streaming):
    class Stream(list):
        def close(self):
            pass

    run = run_record()
    with telemetry.run_trace(run):
        handler = object.__new__(Handler)
        payload = {'model': 'alias', 'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}],
                   'max_tokens': 100, 'stream': streaming}
        body = json.dumps(payload).encode()
        handler.path = '/v1/messages'
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.server = SimpleNamespace(kind='model', model_alias='alias', model_id='model',
                                         max_output_tokens=100, trace_context=context.get_current(),
                                         bedrock=Mock())
        if streaming:
            chunks = [
                {'type': 'message_start', 'message': {'usage': {'input_tokens': 20}}},
                {'type': 'message_delta', 'usage': {'output_tokens': 8}},
            ]
            handler.server.bedrock.invoke_model_with_response_stream.return_value = {
                'body': Stream({'chunk': {'bytes': json.dumps(chunk).encode()}} for chunk in chunks)}
        else:
            handler.server.bedrock.invoke_model.return_value = {'body': io.BytesIO(json.dumps({
                'usage': {'input_tokens': 20, 'output_tokens': 8}, 'content': 'PRIVATE_OUTPUT'}).encode())}
        handler.do_POST()
        handler.send_response.assert_called_once_with(200)
        run['status'] = 'complete'
    model = next(span for span in spans.get_finished_spans() if span.name == 'chat alias')
    assert model.attributes['gen_ai.usage.input_tokens'] == 20
    assert model.attributes['gen_ai.usage.output_tokens'] == 8
    assert all('PRIVATE' not in span.to_json() for span in spans.get_finished_spans())
