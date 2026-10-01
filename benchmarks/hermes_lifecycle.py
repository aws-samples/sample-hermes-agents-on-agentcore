"""Compare Hermes lifecycle costs against a deterministic local Anthropic endpoint.

Run this inside runtime/Dockerfile's image. No AWS or external model calls are made.
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean, median

RESPONSE = 'Deterministic benchmark response.'


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        request = json.loads(self.rfile.read(size))
        if request.get('stream'):
            message = {
                'id': 'msg_benchmark', 'type': 'message', 'role': 'assistant',
                'model': 'claude-sonnet-4-6', 'content': [], 'stop_reason': None,
                'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0},
            }
            events = [
                ('message_start', {'type': 'message_start', 'message': message}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                         'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'text_delta', 'text': RESPONSE}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta',
                                   'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                   'usage': {'output_tokens': 5}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            body = ''.join(f'event: {name}\ndata: {json.dumps(event)}\n\n'
                           for name, event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({
            'id': 'msg_benchmark', 'type': 'message', 'role': 'assistant',
            'model': 'claude-sonnet-4-6',
            'content': [{'type': 'text', 'text': RESPONSE}],
            'stop_reason': 'end_turn', 'stop_sequence': None,
            'usage': {'input_tokens': 10, 'output_tokens': 5},
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def agent_turn(base_url: str, db_path: Path, session_id: str, workspace: Path,
               reuse=None, details=False):
    os.environ['ANTHROPIC_API_KEY'] = 'benchmark-key'
    os.environ['ANTHROPIC_BASE_URL'] = base_url
    from hermes_state import SessionDB
    from run_agent import AIAgent

    started = time.perf_counter()
    initialized = started
    if reuse is None:
        db = SessionDB(db_path)
        agent = AIAgent(
            provider='anthropic', api_mode='anthropic_messages', base_url=base_url,
            api_key='benchmark-key', model='claude-sonnet-4-6', max_tokens=256,
            max_iterations=2, session_id=session_id, session_db=db,
            enabled_toolsets=['terminal', 'file', 'skills', 'memory'], quiet_mode=True,
            skip_memory=True, skip_background_review=True, run_budget_seconds=30,
            cwd=str(workspace),
        )
        initialized = time.perf_counter()
    else:
        agent, db = reuse
    run_started = time.perf_counter()
    result = agent.run_conversation(
        user_message='Return the deterministic response.',
        conversation_history=db.get_messages_as_conversation(session_id),
    )
    run_finished = time.perf_counter()
    if result.get('final_response', '').strip() != RESPONSE:
        raise RuntimeError(f'Unexpected response: {result.get("final_response")!r}')
    if reuse is None:
        agent.close()
        db.close()
    metrics = {
        'lifecycle_seconds': run_finished - started,
        'initialization_seconds': initialized - started,
        'run_seconds': run_finished - run_started,
    }
    return metrics if details else metrics['lifecycle_seconds']


def make_agent(base_url, db_path, session_id, workspace):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path)
    return AIAgent(
        provider='anthropic', api_mode='anthropic_messages', base_url=base_url,
        api_key='benchmark-key', model='claude-sonnet-4-6', max_tokens=256,
        max_iterations=2, session_id=session_id, session_db=db,
        enabled_toolsets=['terminal', 'file', 'skills', 'memory'], quiet_mode=True,
        skip_memory=True, skip_background_review=True, run_budget_seconds=30,
        cwd=str(workspace),
    ), db


def child(args):
    before = time.perf_counter()
    turn = agent_turn(args.base_url, Path(args.db), args.session, Path(args.workspace), details=True)
    print(json.dumps({
        'total_seconds': time.perf_counter() - before,
        **turn,
        'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }))


def summarize(samples):
    return {
        'samples': len(samples), 'first_ms': round(samples[0] * 1000, 1),
        'median_ms': round(median(samples) * 1000, 1),
        'steady_median_ms': round(median(samples[1:] or samples) * 1000, 1),
        'mean_ms': round(mean(samples) * 1000, 1),
        'min_ms': round(min(samples) * 1000, 1), 'max_ms': round(max(samples) * 1000, 1),
    }


def benchmark(args):
    server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f'http://127.0.0.1:{server.server_port}'
    script = str(Path(__file__).resolve())
    report = {'turns': args.turns, 'model': 'deterministic local Anthropic SSE'}
    try:
        with tempfile.TemporaryDirectory(prefix='hermes-lifecycle-') as temporary:
            root = Path(temporary)
            os.environ['HERMES_HOME'] = str(root / 'hermes-home')
            os.environ['ANTHROPIC_API_KEY'] = 'benchmark-key'
            os.environ['ANTHROPIC_BASE_URL'] = base_url
            hermes_home = Path(os.environ['HERMES_HOME'])
            hermes_home.mkdir()
            (hermes_home / 'config.yaml').write_text(
                'auxiliary:\n  title_generation:\n    enabled: false\n')
            # Hermes normally persists this public model registry. Pre-seeding a
            # minimal fresh cache keeps --network none from measuring a 10s fetch timeout.
            (hermes_home / 'models_dev_cache.json').write_text(json.dumps({
                'anthropic': {
                    'id': 'anthropic', 'name': 'Anthropic',
                    'models': {'claude-sonnet-4-6': {
                        'id': 'claude-sonnet-4-6', 'name': 'Claude Sonnet 4.6',
                        'limit': {'context': 200000, 'output': 64000},
                    }},
                },
            }))
            workspace = root / 'workspace'
            workspace.mkdir()
            cold = []
            cold_turns = []
            cold_initializations = []
            cold_runs = []
            cold_rss = []
            cold_session = str(uuid.uuid4())
            for _ in range(args.turns):
                started = time.perf_counter()
                # The benchmark re-executes this checked-in script with temporary paths only.
                process = subprocess.run([  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                    sys.executable, script, '--child-once', '--base-url', base_url,
                    '--db', str(root / 'cold.db'), '--session', cold_session,
                    '--workspace', str(workspace),
                ], capture_output=True, text=True, check=True, timeout=90)
                cold.append(time.perf_counter() - started)
                sample = json.loads(process.stdout.strip().splitlines()[-1])
                cold_rss.append(sample['max_rss_kib'])
                cold_turns.append(sample['lifecycle_seconds'])
                cold_initializations.append(sample['initialization_seconds'])
                cold_runs.append(sample['run_seconds'])
            report['fresh_process_per_turn'] = summarize(cold) | {
                'median_peak_rss_mib': round(median(cold_rss) / 1024, 1),
                'in_process_lifecycle_median_ms': round(median(cold_turns) * 1000, 1),
                'initialization_median_ms': round(median(cold_initializations) * 1000, 1),
                'run_median_ms': round(median(cold_runs) * 1000, 1),
            }

            # Hermes's stock HTTP gateway follows this lifecycle: modules stay loaded,
            # but a new AIAgent is constructed for each request.
            fresh_agent = []
            fresh_session = str(uuid.uuid4())
            import_started = time.perf_counter()
            __import__('run_agent')
            report['persistent_process_import_ms'] = round(
                (time.perf_counter() - import_started) * 1000, 1)
            for _ in range(args.turns):
                fresh_agent.append(agent_turn(
                    base_url, root / 'fresh-agent.db', fresh_session, workspace))
            report['persistent_process_fresh_agent'] = summarize(fresh_agent)

            reused_session = str(uuid.uuid4())
            created = time.perf_counter()
            reusable = make_agent(base_url, root / 'reused-agent.db', reused_session, workspace)
            report['reused_agent_initialization_ms'] = round(
                (time.perf_counter() - created) * 1000, 1)
            reused = [agent_turn(base_url, root / 'reused-agent.db', reused_session,
                                 workspace, reusable) for _ in range(args.turns)]
            reusable[0].close()
            reusable[1].close()
            report['persistent_process_reused_agent'] = summarize(reused)
            report['persistent_process_rss_mib'] = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    finally:
        server.shutdown()
        server.server_close()
    cold = report['fresh_process_per_turn']['steady_median_ms']
    warm = report['persistent_process_fresh_agent']['steady_median_ms']
    reused = report['persistent_process_reused_agent']['steady_median_ms']
    report['speedup'] = {
        'persistent_process_vs_fresh_process': round(cold / warm, 2),
        'reused_agent_vs_fresh_process': round(cold / reused, 2),
    }
    print(json.dumps(report, indent=2))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + '\n')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turns', type=int, default=8)
    parser.add_argument('--output')
    parser.add_argument('--child-once', action='store_true')
    parser.add_argument('--base-url')
    parser.add_argument('--db')
    parser.add_argument('--session')
    parser.add_argument('--workspace')
    return parser.parse_args()


if __name__ == '__main__':
    options = parse_args()
    if options.child_once:
        child(options)
    else:
        benchmark(options)
