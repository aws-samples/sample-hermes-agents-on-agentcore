"""Trusted image configuration and optional host lifecycle integration.

Host lifecycle modules are trusted platform extensions, separate from sandboxed agents.
They must not import agent frameworks. The default lifecycle has no persistence policy.
"""

import importlib.util
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdapterSpec:
    id: str
    source: Path
    storage_namespace: str
    masked_skill_files: tuple[str, ...]
    host_lifecycle: bool


class NoopLifecycle:
    def before_start(self, state, control_fd, conversation_id, context):
        pass

    def after_turn(self, state, control_fd, run_id):
        return None


def load_lifecycle(adapter):
    if not adapter.host_lifecycle:
        return NoopLifecycle()
    spec = importlib.util.spec_from_file_location('agent_host_lifecycle', adapter.source / 'lifecycle.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Lifecycle()


def load_adapter() -> AdapterSpec:
    # This path is deployment configuration, never part of the invocation schema.
    manifest = Path(os.environ['AGENT_ADAPTER_MANIFEST']).resolve(strict=True)
    data = json.loads(manifest.read_text())
    if set(data) - {'host_lifecycle'} != {'id', 'protocol_version', 'model_protocol',
                                        'storage_namespace', 'masked_skill_files'}:
        raise ValueError('Invalid agent adapter manifest fields')
    if type(data['protocol_version']) is not int or data['protocol_version'] != 1:
        raise ValueError('Unsupported agent adapter protocol')
    if data['model_protocol'] != 'anthropic_messages':
        raise ValueError('Unsupported model broker protocol')
    for name in ('id', 'storage_namespace'):
        if not isinstance(data[name], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', data[name]):
            raise ValueError(f'Invalid adapter {name}')
    masks = data['masked_skill_files']
    if (not isinstance(masks, list) or len(masks) > 16
            or any(not isinstance(name, str) or not re.fullmatch(r'\.[a-zA-Z0-9_-]+\.[a-z]+', name)
                   for name in masks)):
        raise ValueError('Invalid skill file masks')
    if not (manifest.parent / 'agent_adapter.py').is_file():
        raise ValueError('Agent adapter entry point is missing')
    host_lifecycle = data.get('host_lifecycle', False)
    if type(host_lifecycle) is not bool:
        raise ValueError('Invalid host lifecycle setting')
    if host_lifecycle and not (manifest.parent / 'lifecycle.py').is_file():
        raise ValueError('Host lifecycle entry point is missing')
    return AdapterSpec(data['id'], manifest.parent, data['storage_namespace'], tuple(masks),
                       host_lifecycle)
