import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import ExitStack, aclosing, asynccontextmanager
from pathlib import Path
from typing import Literal

import boto3
import jwt
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from common.execution import ExecutionMode, execution_mode
from common.runs import LostOwnership, public_run, run_store
from common.security import Tokens, directory
from runtime import telemetry
from runtime.adapter import load_adapter, load_lifecycle
from runtime.broker import start_brokers

ROOT = Path(os.environ.get('AGENT_ROOT', '/mnt/agents'))
EXECUTION_DEADLINE_SECONDS = 60 * 60
bound_subject = None
bound_conversation = None
busy = False
worker = None
run_tasks = set()
logger = logging.getLogger(__name__)


class Invocation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['execute', 'start'] = 'execute'
    run_id: uuid.UUID | None = None
    conversation_id: uuid.UUID
    team_id: uuid.UUID
    security_test_mode: bool = False
    execution_mode: ExecutionMode = 'sequential'
    input_limit_value: int = Field(default=0, ge=0)
    input_limit_unit: str = Field(default='tokens', pattern=r'^(tokens|mb)$')
    max_output_tokens: int = Field(default=0, ge=0)
    message: str = Field(min_length=1, max_length=16000)


def shared_mounts(agent_fd, storage_namespace):
    """Open only explicitly shared children; never pass the agent-root descriptor to bwrap."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    workspace = os.open('workspace', flags, dir_fd=agent_fd)
    descriptors = [workspace]
    try:
        try:
            os.mkdir(storage_namespace, mode=0o700, dir_fd=agent_fd)
        except FileExistsError:
            pass
        shared = os.open(storage_namespace, flags, dir_fd=agent_fd)
        try:
            for name in ('skills', 'agent'):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=shared)
                except FileExistsError:
                    pass
                descriptors.append(os.open(name, flags, dir_fd=shared))
        finally:
            os.close(shared)
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise
    return tuple(descriptors)


class PersistentWorker:
    def __init__(self, sub, conversation_id, security_test_mode, input_limit_value,
                 input_limit_unit, max_output_tokens,
                 temporary, state, servers, process):
        self.sub = sub
        self.conversation_id = conversation_id
        self.security_test_mode = security_test_mode
        self.input_limit_value = input_limit_value
        self.input_limit_unit = input_limit_unit
        self.max_output_tokens = max_output_tokens
        self.temporary = temporary
        self.state = state
        self.servers = servers
        self.process = process
        self.adapter_state = None

    @classmethod
    async def start(cls, sub, conversation_id, security_test_mode,
                    input_limit_value, input_limit_unit, max_output_tokens,
                    workspace_fd, control_fd, session_context=None):
        adapter = load_adapter()
        lifecycle = load_lifecycle(adapter)
        session_context = session_context or {}
        with telemetry.phase('agentcore.worker.local_state'):
            temporary = tempfile.TemporaryDirectory(prefix='agent-session-')
            local = Path(temporary.name)
            state, broker = local / 'state', local / 'broker'
            state.mkdir()
            broker.mkdir()
            (state / 'home').mkdir()
            (state / 'empty').touch()
        try:
            with telemetry.phase('agentcore.lifecycle.before_start'):
                lifecycle.before_start(state, control_fd, conversation_id, session_context)
        except BaseException:
            temporary.cleanup()
            raise
        with telemetry.phase('agentcore.brokers.start'):
            servers = start_brokers(
                broker, bedrock, os.environ['BEDROCK_MODEL_ID'], os.environ['MODEL_ALIAS'],
                max_output_tokens)
        try:
            with telemetry.phase('agentcore.mounts.prepare'):
                shared_workspace, shared_skills, shared_agent = shared_mounts(
                    workspace_fd, adapter.storage_namespace)
        except BaseException:
            for server in servers:
                server.shutdown()
                server.server_close()
            temporary.cleanup()
            raise
        command = [
            'bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--clearenv', '--ro-bind', '/usr', '/usr',
            '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
            '--symlink', 'usr/sbin', '/sbin', '--ro-bind', '/opt', '/opt',
            '--ro-bind', '/etc/ssl', '/etc/ssl', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp',
            '--ro-bind', '/app/runtime/__init__.py', '/app/runtime/__init__.py',
            '--ro-bind', '/app/runtime/worker.py', '/app/runtime/worker.py',
            '--ro-bind', '/app/runtime/contract.py', '/app/runtime/contract.py',
            '--ro-bind', str(adapter.source), '/app/adapter',
            '--dir', '/workspace', '--dir', '/shared',
            '--bind', f'/proc/self/fd/{shared_workspace}', '/workspace/workspace',
            '--bind', f'/proc/self/fd/{shared_skills}', '/shared/skills',
            '--bind', f'/proc/self/fd/{shared_agent}', '/shared/agent',
            '--bind', str(state), '/state', '--ro-bind', str(broker), '/broker',
            '--setenv', 'HOME', '/state/home',
            '--setenv', 'PATH', '/state/home/.local/bin:/opt/venv/bin:/usr/local/bin:/usr/bin:/bin',
            '--setenv', 'MODEL_ALIAS', os.environ['MODEL_ALIAS'],
            '--setenv', 'CONVERSATION_ID', conversation_id,
            '--setenv', 'INPUT_LIMIT_VALUE', str(input_limit_value),
            '--setenv', 'INPUT_LIMIT_UNIT', input_limit_unit,
            '--setenv', 'MAX_OUTPUT_TOKENS', str(max_output_tokens),
            '--setenv', 'HTTP_PROXY', 'http://127.0.0.1:9002',
            '--setenv', 'HTTPS_PROXY', 'http://127.0.0.1:9002',
            '--setenv', 'NO_PROXY', '127.0.0.1,localhost',
            '--setenv', 'ANTHROPIC_API_KEY', 'sandbox-broker',
            '--setenv', 'ANTHROPIC_BASE_URL', 'http://127.0.0.1:9001',
        ]
        for name in adapter.masked_skill_files:
            command.extend(['--ro-bind', str(state / 'empty'), f'/shared/skills/{name}'])
        if security_test_mode:
            command.extend(['--setenv', 'SECURITY_TEST_MODE', 'true'])
        command.extend(['--chdir', '/workspace/workspace',
                        '/opt/venv/bin/python', '-I', '/app/runtime/worker.py'])
        try:
            with telemetry.phase('agentcore.worker.bootstrap') as span:
                startup_start = time.time_ns()
                with telemetry.phase('agentcore.process.launch'):
                    # The fixed bwrap command uses explicit argument positions and no shell.
                    process = await asyncio.create_subprocess_exec(  # nosemgrep: python.lang.security.audit.dangerous-asyncio-create-exec-audit.dangerous-asyncio-create-exec-audit, python.lang.security.audit.dangerous-asyncio-create-exec-tainted-env-args.dangerous-asyncio-create-exec-tainted-env-args
                        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=None if os.environ.get('WORKER_DEBUG') == '1' else asyncio.subprocess.DEVNULL,
                        pass_fds=(shared_workspace, shared_skills, shared_agent), limit=256 * 1024,
                    )
                instance = cls(sub, conversation_id, security_test_mode,
                               input_limit_value, input_limit_unit, max_output_tokens,
                               temporary, state, servers, process)
                instance.adapter_state = session_context.get('adapter_state')
                instance.lifecycle = lifecycle
                instance.adapter = adapter
                line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
                if not line:
                    raise RuntimeError('Agent worker exited during startup')
                ready = json.loads(line)
                if ready.get('type') != 'ready':
                    raise RuntimeError('Agent worker did not become ready')
                reported = telemetry.startup_timings(ready.get('startup_timings'), startup_start, time.time_ns())
                span.set_attribute('agentcore.worker.startup_measurements', reported)
            return instance
        except BaseException:
            if 'process' in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            for server in servers:
                server.shutdown()
                server.server_close()
            temporary.cleanup()
            raise
        finally:
            os.close(shared_workspace)
            os.close(shared_skills)
            os.close(shared_agent)

    @property
    def alive(self):
        return self.process.returncode is None

    async def events(self, payload):
        # Broker handlers run on other threads. Snapshot the current run's OTel context
        # explicitly; never accept trace context or exporter configuration from the worker.
        terminal = None
        with telemetry.phase('agentcore.worker.turn') as span:
            active_context = telemetry.context.get_current()
            for broker in self.servers:
                broker.trace_context = active_context
            try:
                async with aclosing(self._events(payload)) as source:
                    async for data in source:
                        if data.get('type') in {'complete', 'partial', 'error'}:
                            terminal = data
                            if data['type'] == 'error':
                                span.set_status(telemetry.StatusCode.ERROR)
                            break
                        yield data
            finally:
                for broker in self.servers:
                    if broker.trace_context is active_context:
                        broker.trace_context = None
        # Exclude host lifecycle hooks and finalization from worker turn duration.
        if terminal is not None:
            yield terminal

    async def _events(self, payload):
        request_id = str(uuid.uuid4())
        request = payload.model_dump(mode='json') | {'request_id': request_id}
        self.process.stdin.write((json.dumps(request) + '\n').encode())
        await self.process.stdin.drain()
        deadline = time.monotonic() + EXECUTION_DEADLINE_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Execution deadline exceeded')
            try:
                line = await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=min(15, remaining))
            except TimeoutError:
                yield {'type': 'heartbeat'}
                continue
            if not line:
                raise RuntimeError('Agent worker exited during execution')
            data = json.loads(line)
            if data.get('request_id') != request_id:
                raise RuntimeError('Agent worker response correlation failed')
            yield data
            if data.get('type') in {'complete', 'partial'}:
                return
            if data.get('type') == 'error':
                if data.get('fatal', True):
                    raise RuntimeError('Agent worker failed')
                return

    async def close(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await asyncio.to_thread(self._cleanup)

    def _cleanup(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.temporary.cleanup()


@asynccontextmanager
async def lifespan(_app):
    global verifier, bedrock, worker_lock
    load_adapter()  # Fail startup on a missing/unsupported deployment contract.
    telemetry.configure()
    verifier = Tokens(
        os.environ['COGNITO_ISSUER'], os.environ['AGENT_CLIENT_ID'], 'access', 'Agents')
    bedrock = boto3.client(
        'bedrock-runtime', region_name=os.environ.get('AWS_REGION'),
        config=Config(retries={'mode': 'adaptive', 'total_max_attempts': 2},
                      connect_timeout=10, read_timeout=120),
    )
    worker_lock = asyncio.Lock()
    # This preflight uses a fixed bwrap command with no shell or external input.
    process = await asyncio.create_subprocess_exec(  # nosemgrep: python.lang.security.audit.dangerous-asyncio-create-exec-audit.dangerous-asyncio-create-exec-audit
        'bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib',
        '--proc', '/proc', '--dev', '/dev', '/usr/bin/true',
    )
    if await process.wait() != 0:
        raise RuntimeError('Required sandbox namespaces are unavailable')
    try:
        yield
    finally:
        for task in run_tasks:
            task.cancel()
        await asyncio.gather(*run_tasks, return_exceptions=True)
        await reset_worker()
        await asyncio.to_thread(telemetry.flush)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/ping')
def ping():
    return {'status': 'HealthyBusy' if busy else 'Healthy'}


def reserve_execution() -> bool:
    global busy
    if busy:
        return False
    busy = True
    return True


def event(data):
    return 'data: ' + json.dumps(data) + '\n\n'


async def reset_worker(expected=None):
    global worker
    async with worker_lock:
        if worker is None or (expected is not None and worker is not expected):
            return
        previous, worker = worker, None
        await previous.close()


async def get_worker(sub, conversation_id, security_test_mode,
                     input_limit_value, input_limit_unit, max_output_tokens,
                     workspace_fd, control_fd, session_context=None):
    global worker
    async with worker_lock:
        if worker and worker.alive:
            if worker.adapter != load_adapter():
                raise RuntimeError('Runtime session adapter configuration changed')
            if worker.sub != sub or worker.conversation_id != conversation_id:
                raise RuntimeError('Runtime session worker identity mismatch')
            if (worker.security_test_mode == security_test_mode
                    and worker.input_limit_value == input_limit_value
                    and worker.input_limit_unit == input_limit_unit
                    and worker.max_output_tokens == max_output_tokens
                    and worker.adapter_state == (session_context or {}).get('adapter_state')):
                return worker, False
            previous, worker = worker, None
            await previous.close()
        if worker:
            await worker.close()
        worker = await PersistentWorker.start(
            sub, conversation_id, security_test_mode,
            input_limit_value, input_limit_unit, max_output_tokens,
            workspace_fd, control_fd, session_context)
        return worker, True


@app.post('/invocations')
async def invoke(request: Request, payload: Invocation):
    global bound_subject, bound_conversation, busy
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        raise HTTPException(401, 'Access token required')
    try:
        claims = await asyncio.to_thread(verifier.verify, header[7:])
    except (ValueError, jwt.PyJWTError):
        raise HTTPException(401, 'Invalid agent identity') from None
    sub = claims['sub']
    conversation_id = str(payload.conversation_id)
    if str(payload.team_id) != claims['team_id']:
        raise HTTPException(403, 'Agent team does not match the invocation')
    if payload.security_test_mode != claims['security_test_mode']:
        raise HTTPException(403, 'Agent security test mode does not match its identity')
    if payload.execution_mode != execution_mode(claims):
        raise HTTPException(403, 'Agent execution mode does not match its identity')
    for claim in ('input_limit_value', 'input_limit_unit', 'max_output_tokens'):
        if getattr(payload, claim) != claims[claim]:
            raise HTTPException(403, 'Agent input or output limit does not match its identity')
    if bound_subject is not None and bound_subject != sub:
        raise HTTPException(403, 'Session belongs to another agent')
    if bound_conversation is not None and bound_conversation != conversation_id:
        raise HTTPException(403, 'Runtime session belongs to another conversation')
    bound_subject, bound_conversation = sub, conversation_id
    if payload.operation == 'start':
        if not payload.run_id:
            raise HTTPException(422, 'run_id required')
        store = run_store()
        run = await asyncio.to_thread(store.run, str(payload.run_id))
        if (not run or run['agent_sub'] != sub or run['conversation_id'] != conversation_id
                or run['message'] != payload.message or execution_mode(run) != payload.execution_mode):
            raise HTTPException(403, 'Run identity mismatch')
        if int(run['expires']) <= time.time():
            raise HTTPException(410, 'Run expired')
        if run['status'] != 'pending':
            # Claimed runs are never re-executed, even after a process replacement.
            return public_run(run)
        if not reserve_execution():
            raise HTTPException(409, 'Agent is busy')
        try:
            claimed = await asyncio.to_thread(store.claim, run['id'], str(uuid.uuid4()))
            if claimed is None:
                busy = False
                return public_run(await asyncio.to_thread(store.run, run['id']))
            task = asyncio.create_task(produce_run(store, claimed, sub, payload,
                                                   parent_context=telemetry.extract_parent(request.headers)))
            run_tasks.add(task)
            task.add_done_callback(run_tasks.discard)
            return public_run(claimed)
        except BaseException:
            busy = False
            raise
    if os.environ.get('RUN_TABLE_NAME'):
        raise HTTPException(410, 'Durable run start required')
    response = StreamingResponse(
        execute(sub, payload, reserved=True), media_type='text/event-stream',
        headers={'Cache-Control': 'no-store'})
    if not reserve_execution():
        raise HTTPException(409, 'Agent is busy')
    return response


async def produce_run(store, run, sub, payload, parent_context=None):
    try:
        with telemetry.run_trace(run, parent_context):
            await _produce_run(store, run, sub, payload)
    finally:
        await asyncio.to_thread(telemetry.flush)


async def _produce_run(store, run, sub, payload):
    """Independent producer: request cancellation cannot cancel an accepted turn."""
    global busy
    buffer = ''
    current = ''
    interim = ''
    flushed = time.monotonic()
    heartbeat_at = time.monotonic()

    async def flush():
        nonlocal buffer, flushed
        if buffer:
            await telemetry.call('agentcore.event.persist', store.append, run, {'type': 'delta', 'text': buffer})
            buffer = ''
            flushed = time.monotonic()

    async def finalize(data, adapter_state):
        # Called before worker reuse/Busy clears, with the EFS lock held in sequential mode.
        with telemetry.phase('agentcore.run.finalize'):
            telemetry.outcome(data)
            await flush()
            text = data.get('text') or current or interim
            if len(text.encode()) > 300_000:
                raise ValueError('Final response exceeds storage limit')
            for offset in range(0, len(text), 8192):
                await telemetry.call('agentcore.event.persist', store.append, run, {
                    'type': 'final_chunk', 'text': text[offset:offset + 8192]})
            await telemetry.call('agentcore.completion.commit', store.append, run, {**data, 'text': ''},
                                 status=data['type'], final_text=text, adapter_state=adapter_state)

    try:
        session_context = await telemetry.call('agentcore.session.lookup', store.conversation, run)
        async with aclosing(execute(sub, payload, reserved=True,
                                    session_context=session_context, finalize=finalize)) as source:
            async for wire in source:
                data = json.loads(wire.removeprefix('data: '))
                kind = data.get('type')
                if kind in {'complete', 'partial'}:
                    # execute only yields successful completion after the fenced transaction.
                    return
                if time.monotonic() - heartbeat_at >= 15:
                    renewed = await telemetry.call('agentcore.heartbeat', store.heartbeat, run['id'], run['owner'])
                    run.update(renewed)
                    heartbeat_at = time.monotonic()
                if kind == 'delta':
                    text = data.get('text', '')
                    current += text
                    if len(current.encode()) > 300_000:
                        raise ValueError('Response segment exceeds storage limit')
                    buffer += text
                    if len(buffer) >= 2048 or time.monotonic() - flushed >= 0.25:
                        await flush()
                    continue
                await flush()
                if kind == 'heartbeat':
                    continue
                if kind == 'segment_end':
                    if current.strip():
                        interim = current
                    current = ''
                if kind == 'error':
                    await source.aclose()
                    await telemetry.call('agentcore.event.persist', store.append, run, data, status='failed')
                    return
                await telemetry.call('agentcore.event.persist', store.append, run, data)
        await telemetry.call('agentcore.event.persist', store.append, run, {
            'type': 'interrupted', 'message': 'Execution ended without a terminal response.'},
            status='interrupted')
    except LostOwnership:
        logger.warning('Run ownership lost: %s', run['id'])
    except BaseException as error:
        logger.exception('Run producer stopped: %s', run['id'])
        try:
            await telemetry.call('agentcore.event.persist', store.append, run, {
                'type': 'interrupted', 'message': 'Execution interrupted.'},
                status='interrupted')
        except Exception:
            logger.exception('Run interruption will be reconciled: %s', run['id'])
        if isinstance(error, asyncio.CancelledError):
            raise
    finally:
        # Source cleanup finalized/discarded the worker, retaining any sequential-mode EFS lock.
        busy = False


async def execute(sub: str, payload: Invocation, reserved: bool = False,
                  session_context=None, finalize=None):
    global busy
    if not reserved and not reserve_execution():
        yield event({'type': 'error', 'message': 'Agent is busy'})
        return
    active_worker = None
    completed = False
    lock_fd = None
    try:
        conversation_id = str(payload.conversation_id)
        if payload.operation == 'start' and (finalize is None or payload.run_id is None):
            raise ValueError('Durable execution requires fenced finalization')
        with ExitStack() as directories:
            with telemetry.phase('agentcore.workspace.prepare'):
                workspace = directories.enter_context(directory(ROOT, sub))
                control = directories.enter_context(directory(ROOT, '.control', sub, create=True))
                if payload.execution_mode == 'sequential':
                    lock_fd = os.open(
                        'execution.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                        dir_fd=control)
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        yield event({'type': 'error', 'message': 'Another session is using this agent'})
                        return
            with telemetry.phase('agentcore.worker.acquire') as span:
                active_worker, created = await get_worker(
                    sub, conversation_id, payload.security_test_mode,
                    payload.input_limit_value, payload.input_limit_unit,
                    payload.max_output_tokens, workspace, control, session_context)
                span.set_attribute('agentcore.worker.reused', not created)
            yield event({
                'type': 'status',
                'text': 'Starting persistent agent process' if created
                else 'Reusing persistent agent process',
            })
            async for data in active_worker.events(payload):
                if data.get('type') == 'telemetry':
                    telemetry.observe(data)
                    continue
                if data.get('type') in {'delta', 'segment_end', 'status', 'heartbeat', 'error'}:
                    if (data.get('type') == 'error' and not data.get('fatal', True)
                            and payload.operation != 'start'):
                        completed = True
                    yield event(data)
                elif data.get('type') in {'complete', 'partial'}:
                    with telemetry.phase('agentcore.lifecycle.after_turn'):
                        adapter_state = active_worker.lifecycle.after_turn(
                            active_worker.state, control,
                            str(payload.run_id) if payload.operation == 'start' else None)
                    if payload.operation == 'start':
                        await finalize(data, adapter_state)
                        active_worker.adapter_state = adapter_state
                    completed = True
                    yield event(data)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        yield event({
            'type': 'error',
            'message': 'Execution failed or timed out.',
        })
    finally:
        try:
            if active_worker is not None and not completed:
                with telemetry.phase('agentcore.worker.discard'):
                    await asyncio.shield(reset_worker(active_worker))
        finally:
            # Keep the independent lock fd alive until worker termination, even after the
            # directory context closes. Never permit the next session to race a dying worker.
            if lock_fd is not None:
                os.close(lock_fd)
            if payload.operation != 'start':
                busy = False
