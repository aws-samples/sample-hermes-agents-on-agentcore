"""Exercise the memory adapter against the pinned Hermes install in an offline runtime container.

Run with /opt/venv/bin/python in the Hermes image and this script at /app/check_memory_scope.py.
No model calls, credentials, AWS mutations or image rendering are involved.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def child(shared, private, name):
    os.environ['HERMES_HOME'] = private
    sys.path.insert(0, '/opt/hermes')
    sys.path.insert(0, '/app')
    sys.path.insert(0, '/app/adapter')
    from agent_adapter import Adapter, configure_memory_scope

    from runtime.contract import AgentConfig
    configure_memory_scope(Path(shared))
    from agent import prompt_builder
    from agent.system_prompt import _identity_parts, invalidate_system_prompt
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    store.load_from_disk()
    if name != 'READER':
        assert store.add('memory', f'The team maintains project {name}.')['success']
        assert store.add('user', f'The agent keeps profile note SHARED_{name}.')['success']
    agent = SimpleNamespace(_memory_store=store, load_soul_identity=True, skip_context_files=False)
    invalidate_system_prompt(agent)
    for writer in ('ALICE', 'BOB') if name == 'READER' else (name,):
        assert f'project {writer}' in store.format_for_system_prompt('memory')
        assert f'SHARED_{writer}' in store.format_for_system_prompt('user')
    identity, loaded = _identity_parts(agent, None)
    assert loaded and any('Shared team persona' in part for part in identity)
    assert store._path_for('memory') == Path(shared) / 'MEMORY.md'
    assert store._path_for('user') == Path(shared) / 'USER.md'
    assert 'Shared team persona' in prompt_builder.load_soul_md(home_override=Path(private))
    # Replacing SOUL.md changes what the shared loader reads, rather than replacing a private link.
    if name == 'ALICE':
        from utils import atomic_write_text
        atomic_write_text(Path(shared) / 'SOUL.md', 'Shared team persona, updated.')
    # Exercise the extracted adapter's constructor and checkpoint against the pinned
    # framework as well as its path hooks. No model request is needed for initialization.
    workspace = Path(private) / 'workspace'
    workspace.mkdir()
    skills = Path(shared) / 'skills'
    skills.mkdir(exist_ok=True)
    config = AgentConfig(name, 'claude-sonnet-4-6', 256, state=Path(private),
                         workspace=workspace, shared_agent=Path(shared), shared_skills=skills)
    adapter = Adapter(config, lambda *args, **kwargs: None)
    try:
        adapter.save_snapshot()
        assert (Path(private) / 'checkpoint.db').stat().st_size > 0
    finally:
        adapter.close()
    assert not (Path(private) / 'memories' / 'USER.md').exists()
    print(json.dumps({'conversation': name, 'shared_memory': True,
                      'shared_user_memory': True, 'shared_soul': True}))


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        paths = json.loads(sys.argv[2])
        child(paths['shared'], paths['private'], sys.argv[3])
        return
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shared = root / 'agent'
        shared.mkdir()
        (shared / 'SOUL.md').write_text('Shared team persona.')
        processes = []
        for name in ('ALICE', 'BOB'):
            private = root / name
            private.mkdir()
            # Run this checked-in script with the current interpreter and temporary paths only.
            processes.append(subprocess.Popen([  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                sys.executable, __file__, '--child', json.dumps({'shared': str(shared), 'private': str(private)}),
                name,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            if process.returncode:
                raise RuntimeError(stderr + stdout)
        memory = (shared / 'MEMORY.md').read_text()
        assert 'project ALICE' in memory and 'project BOB' in memory
        assert 'SHARED_' not in memory
        user = (shared / 'USER.md').read_text()
        assert 'SHARED_ALICE' in user and 'SHARED_BOB' in user
        for name in ('ALICE', 'BOB'):
            assert not (root / name / 'memories' / 'USER.md').exists()
        # A fresh worker with empty private state must load both writers' shared profiles.
        private = root / 'READER'
        private.mkdir()
        # Run this checked-in script with the current interpreter and temporary paths only.
        reader = subprocess.run([  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            sys.executable, __file__, '--child', json.dumps({'shared': str(shared), 'private': str(private)}),
            'READER',
        ], capture_output=True, text=True, timeout=90, check=False)
        if reader.returncode:
            raise RuntimeError(reader.stderr + reader.stdout)
        assert (shared / 'SOUL.md').read_text() == 'Shared team persona, updated.'
        print(json.dumps({'shared_memory_concurrent_writes': True, 'shared_user_concurrent_writes': True,
                          'shared_user_fresh_worker': True,
                          'shared_soul_atomic_replace': True, 'adapter_initialization': True,
                          'adapter_checkpoint': True}))


if __name__ == '__main__':
    main()
