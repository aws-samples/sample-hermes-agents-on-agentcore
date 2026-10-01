"""Contract/process tests; namespace enforcement is covered by the sandbox probe."""

import asyncio
import io
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agents.echo.agent_adapter import Adapter
from agents.hermes.agent_adapter import configure_home
from runtime import server, worker
from runtime.adapter import NoopLifecycle, load_adapter, load_lifecycle
from runtime.contract import AgentConfig, TurnResult

ROOT = Path(__file__).resolve().parents[1]


def run_adapter(tmp_path, messages, factory=Adapter, **limits):
    config = AgentConfig('conversation', 'model', state=tmp_path)
    source = io.StringIO(''.join(json.dumps({
        'request_id': str(i), 'conversation_id': config.conversation_id, 'message': message,
    }) + '\n' for i, message in enumerate(messages)))
    output = io.StringIO()
    worker.serve(factory, config, source, output, worker.StartupTimings(), **limits)
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_echo_runs_without_snapshot_api_or_files_and_reuses_instance(tmp_path):
    events = run_adapter(tmp_path, ['one', 'two'])
    results = [event for event in events if event['type'] == 'complete']
    assert [event['text'] for event in results] == ['Turn 1: one', 'Turn 2: two']
    assert results[0]['worker_instance_id'] == results[1]['worker_instance_id']
    assert [event['request_id'] for event in results] == ['0', '1']
    assert not hasattr(Adapter, 'checkpoint')
    assert list(tmp_path.iterdir()) == []
    restarted = run_adapter(tmp_path, ['three'])[-1]
    assert restarted['text'] == 'Turn 1: three'
    assert restarted['worker_instance_id'] != results[-1]['worker_instance_id']


def test_adapter_cannot_override_request_correlation_or_emit_terminal_events(tmp_path):
    class Invalid(Adapter):
        def run(self, message):
            self.emit('delta', text='hello', request_id='forged')
            self.emit('complete', text='forged completion')

    events = run_adapter(tmp_path, ['hello'], Invalid)
    assert events[1]['request_id'] == '0'
    assert events[-1]['type'] == 'error' and events[-1]['fatal']
    assert not (tmp_path / 'checkpoint.db').exists()


def test_adapter_failure_never_emits_success_and_closes_adapter(tmp_path):
    closed = []

    class Broken(Adapter):
        def run(self, message):
            raise OSError('agent failed')

        def close(self):
            closed.append(True)

    events = run_adapter(tmp_path, ['hello'], Broken)
    assert events[-1]['type'] == 'error' and events[-1]['fatal']
    assert not any(event['type'] == 'complete' for event in events)
    assert closed == [True]


def test_partial_turn_does_not_require_persistence(tmp_path):
    class Partial(Adapter):
        def run(self, message):
            super().run(message)
            return TurnResult('unfinished', False, 'budget')

    event = run_adapter(tmp_path, ['hello'], Partial)[-1]
    assert (event['type'], event['text'], event['reason']) == ('partial', 'unfinished', 'budget')
    assert list(tmp_path.iterdir()) == []


def test_limit_rejection_does_not_advance_agent_state(tmp_path):
    events = run_adapter(tmp_path, ['far too long', 'ok'], input_limit_value=1)
    assert events[1]['type'] == 'error' and not events[1]['fatal']
    assert events[-1]['text'] == 'Turn 1: ok'


def test_cross_conversation_request_is_rejected_and_adapter_closed(tmp_path):
    closed = []

    class Tracked(Adapter):
        def close(self):
            closed.append(True)

    source = io.StringIO(json.dumps({'conversation_id': 'other', 'request_id': '1', 'message': 'no'}))
    with pytest.raises(ValueError, match='conversation mismatch'):
        worker.serve(Tracked, AgentConfig('mine', 'model', state=tmp_path), source,
                     io.StringIO(), worker.StartupTimings())
    assert closed == [True]
    assert not (tmp_path / 'checkpoint.db').exists()


def test_bootstrap_secures_process_before_bridges_or_adapter_import(monkeypatch):
    order = []
    monkeypatch.setattr(worker, 'secure_process', lambda: order.append('security'))
    monkeypatch.setattr(worker, 'start_bridges', lambda: order.append('bridges'))
    monkeypatch.setattr(worker, 'load_factory', lambda: order.append('import') or Adapter)
    monkeypatch.setattr(worker, 'serve', lambda *args: order.append('serve'))
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    for key, value in {'CONVERSATION_ID': 'c', 'MODEL_ALIAS': 'm', 'MAX_OUTPUT_TOKENS': '0',
                       'INPUT_LIMIT_VALUE': '0', 'INPUT_LIMIT_UNIT': 'tokens'}.items():
        monkeypatch.setenv(key, value)
    worker.main()
    assert order == ['security', 'bridges', 'import', 'serve']


def test_hermes_home_configuration_lives_in_adapter(tmp_path, monkeypatch):
    for name in ('HERMES_HOME', 'TERMINAL_ENV', 'TERMINAL_CWD', 'TERMINAL_HOME_MODE'):
        monkeypatch.delenv(name, raising=False)
    # configure_home mutates the environment in its sandbox process.
    with monkeypatch.context() as scoped:
        scoped.setattr('os.environ', {})
        configure_home(AgentConfig('c', 'm', state=tmp_path))
        import os
        assert os.environ['HERMES_HOME'] == str(tmp_path)
    config = (tmp_path / 'config.yaml').read_text()
    assert 'ledger: false' in config and 'create_dir: /shared/skills' in config


@pytest.mark.parametrize('field,value', [
    ('storage_namespace', '../escape'), ('storage_namespace', '.control'),
    ('masked_skill_files', ['../../escape']), ('protocol_version', 2),
    ('model_protocol', 'openai'),
])
def test_invalid_deployment_contract_fails_closed(tmp_path, monkeypatch, field, value):
    manifest = json.loads((ROOT / 'agents/echo/manifest.json').read_text())
    manifest[field] = value
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'agent_adapter.py').touch()
    monkeypatch.setenv('AGENT_ADAPTER_MANIFEST', str(tmp_path / 'manifest.json'))
    with pytest.raises(ValueError):
        load_adapter()


def test_host_lifecycle_is_optional_and_never_imports_sandboxed_agent(tmp_path, monkeypatch):
    manifest = json.loads((ROOT / 'agents/echo/manifest.json').read_text())
    del manifest['host_lifecycle']
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'agent_adapter.py').write_text('raise AssertionError("sandboxed code imported")')
    monkeypatch.setenv('AGENT_ADAPTER_MANIFEST', str(tmp_path / 'manifest.json'))
    assert isinstance(load_lifecycle(load_adapter()), NoopLifecycle)
    manifest['host_lifecycle'] = True
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'lifecycle.py').write_text('class Lifecycle: pass')
    assert type(load_lifecycle(load_adapter())).__name__ == 'Lifecycle'


def test_framework_free_process_publishes_durable_runs_without_snapshots(
        durable, tmp_path, monkeypatch):
    """Real worker pipes + supervisor + fenced store; replace only bwrap and AWS brokers."""
    monkeypatch.setenv('AGENT_ADAPTER_MANIFEST', str(ROOT / 'agents/echo/manifest.json'))
    monkeypatch.setenv('MODEL_ALIAS', 'test')
    monkeypatch.setenv('BEDROCK_MODEL_ID', 'test')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'worker', None)
    monkeypatch.setattr(server, 'busy', False)
    monkeypatch.setattr(server, 'bedrock', object(), raising=False)
    monkeypatch.setattr(server, 'start_brokers', lambda *args: [])
    (tmp_path / durable.agent['sub'] / 'workspace').mkdir(parents=True)
    launch_process = asyncio.create_subprocess_exec
    launches = []

    async def launch(*command, **kwargs):
        launches.append(command)
        assert 'HERMES_HOME' not in command
        binds = {command[i + 2]: command[i + 1] for i, arg in enumerate(command) if arg == '--bind'}
        env = {command[i + 1]: command[i + 2] for i, arg in enumerate(command) if arg == '--setenv'}
        code = '''
import sys
from pathlib import Path
class NoFrameworkImports:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'hermes_state', 'run_agent', 'agent', 'boto3', 'fastapi'}:
            raise ImportError('Framework/host dependency imported: ' + fullname)
sys.meta_path.insert(0, NoFrameworkImports())
sys.path.insert(0, sys.argv[1])
from agents.echo.agent_adapter import Adapter
from runtime.contract import AgentConfig
from runtime.worker import serve, StartupTimings
serve(Adapter, AgentConfig(sys.argv[3], 'test', state=Path(sys.argv[2])),
      sys.stdin, sys.stdout, StartupTimings())
'''
        return await launch_process(sys.executable, '-I', '-u', '-c', code, str(ROOT),
                                    binds['/state'], env['CONVERSATION_ID'],
                                    stdin=kwargs['stdin'], stdout=kwargs['stdout'],
                                    stderr=asyncio.subprocess.PIPE, limit=kwargs['limit'])

    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', launch)

    async def scenario():
        monkeypatch.setattr(server, 'worker_lock', asyncio.Lock(), raising=False)
        try:
            ids = []
            for number in range(1, 4):
                if number == 3:
                    await server.reset_worker()
                message = f'message {number}'
                run = durable.store.claim(durable.store.create(
                    durable.agent, durable.conversation, durable.user, str(uuid4()), message)['id'],
                    str(uuid4()))
                payload = server.Invocation(operation='start', run_id=run['id'],
                                            conversation_id=run['conversation_id'],
                                            team_id=durable.agent['team_id'], message=message)
                server.busy = True
                await server.produce_run(durable.store, run, durable.agent['sub'], payload)
                assert durable.store.run(run['id'])['status'] == 'complete'
                events = [item['data'] for item in durable.store.page(run, 0)['events']]
                expected_turn = number if number < 3 else 1
                assert ''.join(event.get('text', '') for event in events
                               if event['type'] == 'final_chunk') == f'Turn {expected_turn}: {message}'
                assert durable.store.conversation(run)['adapter_state'] is None
                assert not list(tmp_path.rglob('*.db'))
                ids.append(next(event['worker_instance_id'] for event in events
                                if event['type'] == 'complete'))
            assert ids[0] == ids[1] and ids[1] != ids[2]
            assert len(launches) == 2
            assert (tmp_path / durable.agent['sub'] / 'echo' / 'agent').is_dir()
            assert not (tmp_path / durable.agent['sub'] / 'hermes').exists()
        finally:
            await server.reset_worker()

    asyncio.run(scenario())
