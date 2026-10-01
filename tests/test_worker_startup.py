import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from common.security import directory
from runtime import server, telemetry
from runtime.worker import StartupTimings


@pytest.fixture
def startup_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, 'tracer', provider.get_tracer('test.startup'))
    yield exporter
    provider.shutdown()


def test_worker_timer_measures_actual_block_and_never_swallows_failure():
    timer = StartupTimings()
    with patch('runtime.worker.time.time_ns', side_effect=[100, 150, 200, 270]):
        with timer.measure('adapter_imports'):
            pass
        with pytest.raises(ValueError), timer.measure('agent_init'):
            raise ValueError('PRIVATE exception content')
    assert timer.records == [
        {'phase': 'adapter_imports', 'start_ns': 100, 'end_ns': 150, 'status': 'ok'},
        {'phase': 'agent_init', 'start_ns': 200, 'end_ns': 270, 'status': 'error'},
    ]
    assert 'PRIVATE' not in json.dumps(timer.records)


def test_reported_intervals_keep_timestamps_and_bootstrap_parent(startup_spans):
    with telemetry.phase('agentcore.worker.bootstrap') as parent:
        start = time.time_ns()
        count = telemetry.startup_timings([
            {'phase': 'adapter_imports', 'start_ns': start, 'end_ns': start + 100, 'status': 'ok'},
            {'phase': 'bridges', 'start_ns': start + 110, 'end_ns': start + 200, 'status': 'ok'},
            {'phase': 'agent_init', 'start_ns': start + 210, 'end_ns': start + 300,
             'status': 'error', 'arguments': 'PRIVATE'},
        ], start, start + 400)
        assert count == 3
    children = [span for span in startup_spans.get_finished_spans() if span.name != 'agentcore.worker.bootstrap']
    assert [span.name for span in children] == [
        'agentcore.adapter.imports', 'agentcore.worker.bridges', 'agentcore.adapter.initialize']
    assert children[0].start_time == start and children[0].end_time == start + 100
    assert children[-1].status.status_code == StatusCode.ERROR
    assert all(span.parent.span_id == parent.get_span_context().span_id for span in children)
    assert all('PRIVATE' not in span.to_json() for span in children)


@pytest.mark.parametrize('bad', [
    {'phase': 'PRIVATE', 'start_ns': 100, 'end_ns': 200, 'status': 'ok'},
    {'phase': 'security', 'start_ns': 99, 'end_ns': 200, 'status': 'ok'},
    {'phase': 'security', 'start_ns': 100, 'end_ns': 501, 'status': 'ok'},
    {'phase': 'security', 'start_ns': 300, 'end_ns': 200, 'status': 'ok'},
    {'phase': 'security', 'start_ns': True, 'end_ns': 200, 'status': 'ok'},
    {'phase': 'security', 'start_ns': 100, 'end_ns': 200, 'status': 'PRIVATE'},
    {'phase': [], 'start_ns': 100, 'end_ns': 200, 'status': 'ok'},
    'PRIVATE',
])
def test_invalid_worker_reports_do_not_create_spans(startup_spans, bad):
    assert telemetry.startup_timings([bad], 100, 500) == 0
    assert startup_spans.get_finished_spans() == ()


def test_duplicate_or_overlapping_worker_reports_are_ignored(startup_spans):
    records = [
        {'phase': 'security', 'start_ns': 100, 'end_ns': 200, 'status': 'ok'},
        {'phase': 'security', 'start_ns': 210, 'end_ns': 220, 'status': 'ok'},
        {'phase': 'bridges', 'start_ns': 150, 'end_ns': 180, 'status': 'ok'},
    ]
    assert telemetry.startup_timings(records, 100, 500) == 1
    assert telemetry.startup_timings(None, 100, 500) == 0


@pytest.mark.parametrize('report', ['valid', 'absent', 'invalid'])
def test_startup_emits_supervisor_phases_and_accepts_optional_bounded_ready_timings(
        tmp_path, monkeypatch, startup_spans, report):
    agent = tmp_path / 'agent'
    (agent / 'workspace').mkdir(parents=True)
    (agent / 'hermes').mkdir()
    monkeypatch.setattr(server, 'bedrock', object(), raising=False)
    monkeypatch.setattr(server, 'start_brokers', lambda *args: [])
    monkeypatch.setenv('MODEL_ALIAS', 'test')
    monkeypatch.setenv('BEDROCK_MODEL_ID', 'test')

    async def ready():
        data = {'type': 'ready'}
        if report == 'valid':
            timer = StartupTimings()
            for name in telemetry.STARTUP_PHASES:
                with timer.measure(name):
                    pass
            data['startup_timings'] = timer.records
        elif report == 'invalid':
            data['startup_timings'] = 'PRIVATE'
        return (json.dumps(data) + '\n').encode()

    async def launch(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=SimpleNamespace(readline=ready))

    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', launch)

    async def scenario():
        with directory(agent) as root, directory(tmp_path) as control, \
                telemetry.phase('agentcore.worker.acquire'):
            worker = await server.PersistentWorker.start('agent', 'conversation', False,
                                                         0, 'tokens', 0, root, control)
        worker._cleanup()

    asyncio.run(scenario())
    spans = {span.name: span for span in startup_spans.get_finished_spans()}
    acquire = spans['agentcore.worker.acquire']
    for name in ('agentcore.worker.local_state', 'agentcore.lifecycle.before_start',
                 'agentcore.brokers.start', 'agentcore.mounts.prepare', 'agentcore.worker.bootstrap'):
        assert spans[name].parent.span_id == acquire.context.span_id
    bootstrap = spans['agentcore.worker.bootstrap']
    assert spans['agentcore.process.launch'].parent.span_id == bootstrap.context.span_id
    expected = len(telemetry.STARTUP_PHASES) if report == 'valid' else 0
    assert bootstrap.attributes['agentcore.worker.startup_measurements'] == expected
    if report == 'valid':
        for name in telemetry.STARTUP_PHASES.values():
            assert spans[name].parent.span_id == bootstrap.context.span_id
            assert bootstrap.start_time <= spans[name].start_time <= spans[name].end_time <= bootstrap.end_time
    assert all('PRIVATE' not in span.to_json() for span in spans.values())
