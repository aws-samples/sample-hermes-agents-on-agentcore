"""Framework-independent bootstrap and JSONL runner inside the restricted namespace."""

import ctypes
import errno
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class StartupTimings:
    """Local measurements only; the supervisor owns all OpenTelemetry/export credentials."""

    def __init__(self):
        self.records = []

    @contextmanager
    def measure(self, phase):
        start = time.time_ns()
        status = 'error'
        try:
            yield
            status = 'ok'
        finally:
            self.records.append({'phase': phase, 'start_ns': start,
                                 'end_ns': time.time_ns(), 'status': status})


class Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as upstream:
            upstream.connect(self.server.upstream)
            self.request.settimeout(120)
            upstream.settimeout(120)
            while True:
                ready, _, _ = select.select([self.request, upstream], [], [], 120)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def restrict_syscalls():
    """Prevent privilege/namespace manipulation after bubblewrap establishes the boundary."""
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)

    class Compare(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                    ('mask', ctypes.c_uint64), ('value', ctypes.c_uint64)]

    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                              ctypes.c_uint, ctypes.POINTER(Compare)]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('Cannot initialize seccomp')
    try:
        for name in ('mount', 'umount2', 'pivot_root', 'unshare', 'setns', 'ptrace',
                     'open_by_handle_at', 'bpf', 'perf_event_open', 'userfaultfd',
                     'keyctl', 'reboot', 'kexec_load', 'init_module', 'finit_module'):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and library.seccomp_rule_add_array(context, 0x50000 | errno.EPERM, number, 0, None):
                raise RuntimeError('Cannot install seccomp rule')
        clone3 = library.seccomp_syscall_resolve_name(b'clone3')
        if clone3 >= 0 and library.seccomp_rule_add_array(context, 0x50000 | errno.ENOSYS, clone3, 0, None):
            raise RuntimeError('Cannot constrain clone3')
        clone = library.seccomp_syscall_resolve_name(b'clone')
        for flag in (0x20000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000):
            compare = Compare(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
            if library.seccomp_rule_add_array(context, 0x50000 | errno.EPERM, clone, 1, ctypes.byref(compare)):
                raise RuntimeError('Cannot constrain namespace clones')
        if library.seccomp_load(context):
            raise RuntimeError('Cannot activate seccomp')
    finally:
        library.seccomp_release(context)


def secure_process():
    # Must happen before importing an adapter, loading plugins or starting threads.
    for entry in os.listdir('/proc/self/fd'):
        fd = int(entry)
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                if os.path.exists(f'/proc/self/fd/{fd}'):
                    raise
    restrict_syscalls()


def start_bridges():
    for port, kind in ((9001, 'model'), (9002, 'egress')):
        server = Server(('127.0.0.1', port), Bridge)
        server.upstream = f'/broker/{kind}.sock'
        threading.Thread(target=server.serve_forever, daemon=True).start()


def load_factory():
    # Both directories are immutable image mounts; neither is the writable workspace.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path.insert(0, '/app/adapter')
    from agent_adapter import Adapter
    return Adapter


def serve(factory, config, source, output, startup, input_limit_value=0, input_limit_unit='tokens'):
    """Run the adapter contract on streams; process isolation is established by main()."""
    output_lock = threading.Lock()
    request_context = {'id': None}
    worker_instance_id = str(uuid.uuid4())

    def emit(kind: str, **fields):
        event = {**fields, 'type': kind, 'request_id': request_context['id']}
        with output_lock:
            output.write(json.dumps(event) + '\n')
            output.flush()

    def agent_event(kind: str, **fields):
        if kind not in {'delta', 'segment_end', 'status', 'telemetry'}:
            raise ValueError('Unsupported adapter event')
        emit(kind, **fields)

    with startup.measure('agent_init'):
        agent = factory(config, agent_event)
    try:
        emit('ready', pid=os.getpid(), worker_instance_id=worker_instance_id,
             startup_timings=startup.records)
        for line in source:
            request = json.loads(line)
            if request.get('conversation_id') != config.conversation_id:
                raise ValueError('Worker conversation mismatch')
            request_context['id'] = request['request_id']
            if input_limit_value:
                if input_limit_unit == 'tokens':
                    actual = agent.estimate_tokens(request['message'])
                    exceeded = actual > input_limit_value
                    detail = f'approximately {actual} tokens'
                else:
                    actual = len(request['message'].encode('utf-8'))
                    exceeded = actual > input_limit_value * 1024 * 1024
                    detail = f'{actual / (1024 * 1024):.2f} MB'
                if exceeded:
                    emit('error', fatal=False,
                         message=(f'Input is {detail}; this agent input limit is '
                                  f'{input_limit_value} {input_limit_unit}'))
                    continue
            try:
                result = agent.run(request['message'])
                common = {'text': result.text, 'pid': os.getpid(),
                          'worker_instance_id': worker_instance_id}
                if result.completed:
                    emit('complete', **common)
                else:
                    emit('partial', reason=result.reason, **common)
            except Exception as exc:  # noqa: BLE001 - fatal worker protocol boundary
                emit('error', message=str(exc)[:500], fatal=True)
                return
    finally:
        agent.close()


def main():
    startup = StartupTimings()
    with startup.measure('security'):
        secure_process()
    output = sys.stdout
    sys.stdout = sys.stderr
    with startup.measure('bridges'):
        start_bridges()
    with startup.measure('adapter_imports'):
        factory = load_factory()
        from runtime.contract import AgentConfig
    config = AgentConfig(os.environ['CONVERSATION_ID'], os.environ['MODEL_ALIAS'],
                         int(os.environ['MAX_OUTPUT_TOKENS']))
    serve(factory, config, sys.stdin, output, startup,
          int(os.environ['INPUT_LIMIT_VALUE']), os.environ['INPUT_LIMIT_UNIT'])


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No traceback or environment dump sent to the browser.
        sys.__stdout__.write(json.dumps({
            'type': 'error', 'request_id': locals().get('request_context', {}).get('id'),
            'message': str(exc)[:500],
        }) + '\n')
        sys.__stdout__.flush()
        raise SystemExit(1) from exc
