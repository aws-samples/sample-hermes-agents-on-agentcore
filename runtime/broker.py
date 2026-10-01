"""Trusted per-execution brokers: fixed Bedrock model and public HTTPS CONNECT only."""

import ipaddress
import json
import select
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from runtime import telemetry


def public_target(authority: str) -> tuple[str, int]:
    parsed = urlsplit('//' + authority)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Invalid CONNECT authority')
    if parsed.port != 443 or not parsed.hostname:
        raise ValueError('Only public HTTPS port 443 is allowed')
    addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, 443,
                                                            socket.AF_INET, socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Private, local and metadata addresses are forbidden')
    # Connect directly to the validated IP. Never resolve a hostname again after validation.
    return min(addresses), 443


def relay(left: socket.socket, right: socket.socket, lifetime: int = 240):
    deadline = time.monotonic() + lifetime
    while time.monotonic() < deadline:
        ready, _, _ = select.select([left, right], [], [], 5)
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            destination = right if source is left else left
            destination.sendall(data)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        # SDKs routinely close loopback sockets after cancellation or after they
        # have enough of an HTTP error response. Do not turn that into a noisy
        # traceback; every other broker exception retains the default logging.
        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each response, including SSE; no ambiguous chunk framing.
    protocol_version = 'HTTP/1.0'

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # The model/HTTP client already abandoned this response. Closing the
            # handler is sufficient; propagating only produces socketserver noise.
            self.close_connection = True

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        if self.server.kind != 'egress':
            self.send_error(405)
            return
        try:
            ip, port = public_target(self.path)
            with socket.create_connection((ip, port), timeout=10) as upstream:
                self.send_response(200, 'Connection Established')
                self.end_headers()
                self.wfile.flush()
                relay(self.connection, upstream)
        except (ValueError, OSError):
            self.close_connection = True

    def do_POST(self):
        if self.server.kind != 'model' or self.path.split('?')[0] != '/v1/messages':
            self.send_error(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 2 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            streaming = data.pop('stream', False)
            if data.pop('model', None) != self.server.model_alias:
                raise ValueError('Model is not allowed')
            allowed = {'messages', 'system', 'max_tokens', 'temperature', 'top_p', 'top_k',
                       'stop_sequences', 'tools', 'tool_choice', 'thinking', 'metadata'}
            if set(data) - allowed or not isinstance(data.get('messages'), list):
                raise ValueError('Invalid inference parameters')
            max_tokens = data.get('max_tokens', 4096)
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
                raise ValueError('Invalid max_tokens')
            if self.server.max_output_tokens and max_tokens > self.server.max_output_tokens:
                raise ValueError('Requested max_tokens exceeds the agent output limit')
            data['anthropic_version'] = 'bedrock-2023-05-31'
            params = {'modelId': self.server.model_id, 'contentType': 'application/json',
                      'body': json.dumps(data)}
            with telemetry.model_span(self.server, streaming, max_tokens) as (span, usage):
                self._invoke_model(params, streaming, span, usage)
        except (ValueError, KeyError, ClientError):
            self.send_error(502, 'Model request rejected or unavailable')

    def _invoke_model(self, params, streaming, span, usage):
        if streaming:
            result = self.server.bedrock.invoke_model_with_response_stream(**params)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            try:
                for item in result['body']:
                    if 'chunk' not in item:
                        raise RuntimeError('Bedrock streaming failure')
                    event = json.loads(item['chunk']['bytes'])
                    telemetry.record_usage(usage, event.get('message') if event.get('type') == 'message_start' else event)
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
            finally:
                result['body'].close()
        else:
            result = self.server.bedrock.invoke_model(**params)
            with result['body'] as body:
                response = body.read()
            telemetry.record_usage(usage, json.loads(response))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response)


def start_brokers(root: Path, bedrock, model_id: str, model_alias: str,
                  max_output_tokens: int = 0):
    servers = []
    for kind in ('model', 'egress'):
        server = UnixServer(str(root / f'{kind}.sock'), Handler)
        server.kind = kind
        server.bedrock, server.model_id, server.model_alias = bedrock, model_id, model_alias
        server.max_output_tokens = max_output_tokens
        server.trace_context = telemetry.context.get_current()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    return servers
