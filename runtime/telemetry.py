"""Metadata-only ADOT spans, exported exclusively by the trusted supervisor.

Do not add prompts, responses, tool arguments/results or authorization headers here.
Agent tool spans measure callback observation, not exact parallel tool execution time.
"""

import asyncio
import json
import logging
import os
import re
import threading
from contextlib import contextmanager

from opentelemetry import baggage, context, trace
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from runtime.adapter import load_adapter

logger = logging.getLogger(__name__)
tracer = trace.get_tracer('agentcore.harness')
_RUN = context.create_key('agentcore-run-telemetry')
_PARENT_SOURCE = context.create_key('agentcore-parent-trace-source')
TOOL_NAMES = {'terminal', 'process', 'read_file', 'write_file', 'search_files', 'patch',
              'skills_list', 'skill_view', 'skill_manage', 'memory'}
STARTUP_PHASES = {
    'security': 'agentcore.worker.security',
    'bridges': 'agentcore.worker.bridges',
    'adapter_imports': 'agentcore.adapter.imports',
    'agent_init': 'agentcore.adapter.initialize',
}


def configure():
    if os.environ.get('AGENT_OBSERVABILITY_ENABLED', '').lower() != 'true':
        return
    from amazon.opentelemetry.distro.aws_opentelemetry_configurator import (
        AwsOpenTelemetryConfigurator,
    )
    from amazon.opentelemetry.distro.aws_opentelemetry_distro import AwsOpenTelemetryDistro

    # Configure the ADOT providers/exporters without auto-instrumenting libraries that may
    # capture request bodies. We explicitly instrument the sandbox boundary and model broker.
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    AwsOpenTelemetryDistro().configure(apply_patches=False)
    AwsOpenTelemetryConfigurator().configure()


def flush():
    provider = trace.get_tracer_provider()
    if hasattr(provider, 'force_flush'):
        try:
            if not provider.force_flush(timeout_millis=3000):
                logger.warning('OTel flush timed out')
        except Exception:  # noqa: BLE001 - exporter failures cannot alter completed work
            logger.warning('OTel flush failed')


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000 else None


class RunTrace:
    def __init__(self, span, run):
        self.span = span
        self.attributes = {
            'session.id': run['runtime_session_id'],
            'gen_ai.conversation.id': run['conversation_id'],
            'gen_ai.agent.id': run['agent_id'], 'gen_ai.agent.name': load_adapter().id,
            'agentcore.run.id': run['id'],
        }
        self.tools = {}
        self.lock = threading.Lock()
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.iterations = 0

    def model_finished(self, usage):
        with self.lock:
            self.model_calls += 1
            self.input_tokens += usage.get('input_tokens', 0)
            self.output_tokens += usage.get('output_tokens', 0)

    def observe(self, data):
        event = data.get('event')
        if event == 'model_iteration':
            iteration = _count(data.get('iteration'))
            if iteration is not None:
                self.iterations = max(self.iterations, iteration)
            return
        call_id = data.get('tool_call_id')
        if not isinstance(call_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call_id):
            return
        name = data.get('tool_name')
        name = name if isinstance(name, str) and name in TOOL_NAMES else 'other'
        if event == 'tool_start' and call_id not in self.tools and len(self.tools) < 128:
            self.tools[call_id] = tracer.start_span('execute_tool ' + name, attributes={
                **self.attributes, 'gen_ai.operation.name': 'execute_tool',
                'gen_ai.tool.name': name, 'gen_ai.tool.call.id': call_id,
                'agentcore.tool.timing': 'callback_observation',
            })
        elif event == 'tool_end':
            tool = self.tools.pop(call_id, None)
            if tool:
                tool.set_attribute('agentcore.tool.lifecycle', 'result_available')
                tool.end()  # result availability does not assert tool success

    def finish(self, run):
        for tool in self.tools.values():
            tool.set_attribute('agentcore.tool.lifecycle', 'unresolved')
            tool.end()
        self.tools.clear()
        status = run.get('status', 'interrupted')
        self.span.set_attribute('agentcore.run.status', status)
        self.span.set_attribute('agentcore.model_calls', self.model_calls)
        self.span.set_attribute('agentcore.iterations', self.iterations)
        self.span.set_attribute('gen_ai.usage.input_tokens', self.input_tokens)
        self.span.set_attribute('gen_ai.usage.output_tokens', self.output_tokens)
        self.span.set_status(Status(StatusCode.OK if status == 'complete' else StatusCode.ERROR))


def extract_parent(headers):
    # AgentCore's service trace uses X-Ray context. Prefer it when present; W3C remains
    # supported for callers using that format. Do not propagate other headers or baggage.
    allowed = {key.lower(): value for key, value in headers.items()
               if key.lower() in {'traceparent', 'tracestate', 'x-amzn-trace-id'}}
    xray_header = allowed.get('x-amzn-trace-id', '')
    if isinstance(xray_header, str) and len(xray_header) <= 512:
        parent = AwsXRayPropagator().extract({'X-Amzn-Trace-Id': xray_header})
        if trace.get_current_span(parent).get_span_context().is_valid:
            return context.set_value(_PARENT_SOURCE, 'xray', parent)
    parent = TraceContextTextMapPropagator().extract({
        key: allowed[key] for key in ('traceparent', 'tracestate') if key in allowed})
    source = 'w3c' if trace.get_current_span(parent).get_span_context().is_valid else 'missing'
    return context.set_value(_PARENT_SOURCE, source, parent)


@contextmanager
def phase(name):
    observer = context.get_value(_RUN)
    attributes = dict(observer.attributes) if isinstance(observer, RunTrace) else {}
    attributes['agentcore.phase'] = name
    with tracer.start_as_current_span(name, attributes=attributes,
                                      record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except BaseException as error:
            span.set_attribute('error.type', type(error).__name__)
            span.set_status(StatusCode.ERROR)
            raise


async def call(name, function, *args, **kwargs):
    with phase(name):
        return await asyncio.to_thread(function, *args, **kwargs)


def startup_timings(records, window_start_ns, window_end_ns):
    """Export bounded, allowlisted worker-reported phases under the current bootstrap span.

    Both processes share the VM wall clock. Reject malformed/out-of-window intervals rather
    than allowing sandbox output to create arbitrary spans, attributes or trace parents.
    """
    if not isinstance(records, list):
        return 0
    observer = context.get_value(_RUN)
    attributes = dict(observer.attributes) if isinstance(observer, RunTrace) else {}
    seen = set()
    last_end = window_start_ns
    for record in records[:len(STARTUP_PHASES)]:
        if not isinstance(record, dict):
            continue
        phase_name, start, end = record.get('phase'), record.get('start_ns'), record.get('end_ns')
        if (not isinstance(phase_name, str) or phase_name not in STARTUP_PHASES or phase_name in seen
                or type(start) is not int or type(end) is not int
                or not last_end <= start <= end <= window_end_ns
                or record.get('status') not in ('ok', 'error')):
            continue
        name = STARTUP_PHASES[phase_name]
        span = tracer.start_span(name, start_time=start, attributes={
            **attributes, 'agentcore.phase': name, 'agentcore.startup.measurement': 'worker_wall_clock',
        })
        span.set_status(StatusCode.OK if record['status'] == 'ok' else StatusCode.ERROR)
        span.end(end_time=end)
        seen.add(phase_name)
        last_end = end
    return len(seen)


@contextmanager
def run_trace(run, parent_context=None):
    attributes = {
        'gen_ai.operation.name': 'invoke_agent', 'gen_ai.provider.name': 'aws.bedrock',
        'gen_ai.agent.name': load_adapter().id, 'gen_ai.agent.id': run['agent_id'],
        'gen_ai.request.model': os.environ.get('MODEL_ALIAS', 'unknown'),
        'session.id': run['runtime_session_id'], 'gen_ai.conversation.id': run['conversation_id'],
        'agentcore.run.id': run['id'], 'agentcore.execution_mode': run.get('execution_mode', 'sequential'),
        'agentcore.trace.parent_source': context.get_value(_PARENT_SOURCE, parent_context) or 'local',
    }
    with tracer.start_as_current_span('invoke_agent ' + attributes['gen_ai.agent.name'], context=parent_context, attributes=attributes,
                                      record_exception=False, set_status_on_exception=False) as span:
        observer = RunTrace(span, run)
        ctx = context.set_value(_RUN, observer)
        ctx = baggage.set_baggage('session.id', run['runtime_session_id'], context=ctx)
        token = context.attach(ctx)
        logger.info(json.dumps({'event': 'agent.turn.started', 'run_id': run['id'],
                               'trace_id': format(span.get_span_context().trace_id, '032x')}))
        try:
            yield observer
        finally:
            observer.finish(run)
            logger.info(json.dumps({'event': 'agent.turn.finished', 'run_id': run['id'],
                                   'status': run.get('status', 'interrupted'),
                                   'trace_id': format(span.get_span_context().trace_id, '032x')}))
            context.detach(token)


def observe(data):
    observer = context.get_value(_RUN)
    if isinstance(observer, RunTrace):
        observer.observe(data)


def outcome(data):
    observer = context.get_value(_RUN)
    if isinstance(observer, RunTrace):
        reason = data.get('reason')
        if isinstance(reason, str) and re.fullmatch(r'[a-z_]{1,64}', reason):
            observer.span.set_attribute('gen_ai.response.finish_reasons', [reason])


@contextmanager
def model_span(server, streaming, max_tokens):
    ctx = getattr(server, 'trace_context', None) or context.Context()
    observer = context.get_value(_RUN, context=ctx)
    attributes = dict(observer.attributes) if isinstance(observer, RunTrace) else {}
    attributes.update({
        'gen_ai.operation.name': 'chat', 'gen_ai.provider.name': 'aws.bedrock',
        'gen_ai.request.model': server.model_id,
        'gen_ai.request.max_tokens': max_tokens, 'agentcore.model.streaming': bool(streaming),
    })
    usage = {}
    with tracer.start_as_current_span('chat ' + server.model_alias, context=ctx, kind=SpanKind.CLIENT,
                                      attributes=attributes, record_exception=False,
                                      set_status_on_exception=False) as span:
        try:
            yield span, usage
            span.set_status(StatusCode.OK)
        except BaseException as error:
            span.set_attribute('error.type', type(error).__name__)
            span.set_status(StatusCode.ERROR)
            raise
        finally:
            for key in ('input_tokens', 'output_tokens'):
                if key in usage:
                    span.set_attribute('gen_ai.usage.' + key, usage[key])
            for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
                if key in usage:
                    span.set_attribute('aws.bedrock.usage.' + key, usage[key])
            if isinstance(observer, RunTrace):
                observer.model_finished(usage)


def record_usage(usage, message):
    if not isinstance(message, dict):
        return
    source = message.get('usage', {})
    if not isinstance(source, dict):
        return
    for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
        value = _count(source.get(key))
        if value is not None:
            usage[key] = value
