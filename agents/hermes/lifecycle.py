"""Trusted Hermes persistence integration; never imports the sandboxed framework.

SQLite creation/backup stays in agent_adapter.py. Only bounded, opaque snapshot bytes
are handled here, using the supervisor's verified per-agent control directory.
"""

import os
from uuid import UUID, uuid4

from common.security import directory, read_file


def checkpoint_filename(conversation_id, run_id):
    return f'{UUID(conversation_id)}.{UUID(run_id)}.db'


def restore_checkpoint(state, control_fd, conversation_id, checkpoint_name=None):
    if checkpoint_name is not None:
        parts = checkpoint_name.split('.')
        if (len(parts) != 3 or parts[0] != conversation_id
                or checkpoint_name != checkpoint_filename(conversation_id, parts[1])):
            raise ValueError('Invalid committed checkpoint')
    name = checkpoint_name or f'{conversation_id}.db'
    try:
        snapshot = read_file(control_fd, name, 64 * 1024 * 1024)
    except FileNotFoundError:
        if checkpoint_name is not None:
            raise RuntimeError('Committed checkpoint is unavailable') from None
        return
    (state / 'state.db').write_bytes(snapshot)


def write_checkpoint(control_fd, name, snapshot, *, immutable):
    temporary_name = f'{uuid4()}.tmp'
    target = os.open(temporary_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
                     dir_fd=control_fd)
    try:
        with os.fdopen(target, 'wb') as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        if immutable:
            os.link(temporary_name, name, src_dir_fd=control_fd, dst_dir_fd=control_fd)
        else:
            os.replace(temporary_name, name, src_dir_fd=control_fd, dst_dir_fd=control_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=control_fd)
        except FileNotFoundError:
            pass
    os.fsync(control_fd)


def committed_checkpoint(context):
    state = context.get('adapter_state')
    if state is None:
        # Existing conversations retain their pre-adapter checkpoint pointer.
        return context.get('checkpoint_name')
    if (not isinstance(state, dict) or state.get('adapter') != 'hermes-v1'
            or not isinstance(state.get('checkpoint'), str)):
        raise ValueError('Incompatible Hermes conversation state')
    return state['checkpoint']


class Lifecycle:
    def before_start(self, state, control_fd, conversation_id, context):
        self.conversation_id = conversation_id
        restore_checkpoint(state, control_fd, conversation_id, committed_checkpoint(context))

    def after_turn(self, state, control_fd, run_id):
        with directory(state) as state_fd:
            snapshot = read_file(state_fd, 'checkpoint.db', 64 * 1024 * 1024)
        name = (checkpoint_filename(self.conversation_id, run_id) if run_id
                else f'{self.conversation_id}.db')
        write_checkpoint(control_fd, name, snapshot, immutable=run_id is not None)
        return {'adapter': 'hermes-v1', 'checkpoint': name}
