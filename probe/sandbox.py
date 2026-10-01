"""Fail-closed namespace launcher. No shell and no unsandboxed fallback."""

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID


@contextmanager
def workspace_fd(root: Path, agent_sub: str):
    """Pin the trusted root and one canonical UUID directory without following links."""
    if str(UUID(agent_sub)) != agent_sub:
        raise ValueError('Agent subject must be a canonical UUID')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(root, flags)
    try:
        fd = os.open(agent_sub, flags, dir_fd=root_fd)
        try:
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def command(fd: int) -> list[str]:
    inspector = Path(__file__).with_name('inspect_sandbox.py').resolve()
    entrypoint = Path(__file__).with_name('enter_sandbox.py').resolve()
    return [
        '/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
        '--cap-drop', 'ALL', '--clearenv',
        '--ro-bind', '/usr', '/usr',
        '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/sbin', '/sbin',
        '--symlink', 'usr/lib', '/lib',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
        '--dir', '/app', '--ro-bind', str(inspector), '/app/inspect_sandbox.py',
        '--ro-bind', str(entrypoint), '/app/enter_sandbox.py',
        '--bind', f'/proc/self/fd/{fd}', '/workspace',
        '--setenv', 'HOME', '/workspace', '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
        '--setenv', 'HERMES_HOME', '/workspace/hermes',
        '--chdir', '/workspace',
        '/usr/local/bin/python', '-I', '/app/enter_sandbox.py',
    ]


def run(root: Path, agent_sub: str, payload: str) -> subprocess.CompletedProcess:
    with workspace_fd(root, agent_sub) as fd:
        # command() builds a fixed bwrap argument vector and never invokes a shell.
        return subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            command(fd), input=payload, text=True, capture_output=True,
            timeout=30, pass_fds=(fd,), check=False,
        )
