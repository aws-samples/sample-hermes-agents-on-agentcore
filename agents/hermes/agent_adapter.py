"""Hermes integration, imported and executed exclusively inside the sandbox."""

import os
import re
import sqlite3
import sys
from pathlib import Path

from runtime.contract import AgentConfig, TurnResult

TRACED_TOOLS = {'terminal', 'process', 'read_file', 'write_file', 'search_files', 'patch',
                'skills_list', 'skill_view', 'skill_manage', 'memory'}
MEMORY_SCOPE_GUIDANCE = (
    "Agent storage scopes: memory target='memory' writes shared agent knowledge to "
    "/shared/agent/MEMORY.md. The agent's shared identity/persona is /shared/agent/SOUL.md; "
    "use that path when reading or editing SOUL.md. memory target='user' writes to "
    "/shared/agent/USER.md. MEMORY.md, USER.md and SOUL.md belong to this agent and are "
    "shared across its conversations. Do not copy private conversation transcripts into "
    "shared memory, skills or workspace files unless explicitly asked to publish them. "
    "Shared files are visible to other authorized users of this agent."
)


def configure_memory_scope(shared_home=Path('/shared/agent')):
    """Preserve upstream locking/atomic replacement while scoping path hooks."""
    from agent import prompt_builder
    from tools.memory_tool_store import MemoryStore

    def memory_path(target):
        if target == 'memory':
            return shared_home / 'MEMORY.md'
        if target == 'user':
            return shared_home / 'USER.md'
        raise ValueError('Unknown memory target')

    MemoryStore._path_for = staticmethod(memory_path)
    original_soul = getattr(prompt_builder.load_soul_md, '_unscoped_loader', prompt_builder.load_soul_md)

    def load_shared_soul(context_length=None, home_override=None):
        return original_soul(context_length=context_length, home_override=shared_home)

    load_shared_soul._unscoped_loader = original_soul
    prompt_builder.load_soul_md = load_shared_soul


def tool_trace_fields(call_id, name):
    if not isinstance(call_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call_id):
        return None
    return {'tool_call_id': call_id,
            'tool_name': name if isinstance(name, str) and name in TRACED_TOOLS else 'other'}


def configure_home(config):
    os.environ.update(HERMES_HOME=str(config.state), TERMINAL_ENV='local',
                      TERMINAL_CWD=str(config.workspace), TERMINAL_HOME_MODE='profile')
    (config.state / 'skills').mkdir(exist_ok=True)
    (config.state / 'config.yaml').write_text(
        f'terminal:\n  backend: local\n  cwd: {config.workspace}\n  home_mode: profile\n'
        f'skills:\n  ledger: false\n  external_dirs:\n    - {config.shared_skills}\n'
        f'  create_dir: {config.shared_skills}\n')


class Adapter:
    def __init__(self, config: AgentConfig, emit):
        self.config = config
        self.emit = emit
        self.database = config.state / 'state.db'
        configure_home(config)
        sys.path.insert(0, '/opt/hermes')
        import hermes_state
        hermes_state.DEFAULT_DB_PATH = self.database
        from agent.model_metadata import estimate_tokens_rough
        from agent.system_prompt import invalidate_system_prompt
        from hermes_state import SessionDB
        from run_agent import AIAgent

        configure_memory_scope(config.shared_agent)
        self.estimate_tokens = estimate_tokens_rough
        self.invalidate_system_prompt = invalidate_system_prompt
        self.db = SessionDB(self.database)
        try:
            self.agent = AIAgent(
                provider='anthropic', api_mode='anthropic_messages',
                base_url=config.model_url, api_key=config.model_api_key,
                model=config.model, max_tokens=config.max_output_tokens or None,
                max_iterations=20, session_id=config.conversation_id, session_db=self.db,
                enabled_toolsets=['terminal', 'file', 'skills', 'memory'],
                quiet_mode=True, skip_background_review=True,
                run_budget_seconds=config.run_budget_seconds, save_trajectories=False,
                load_soul_identity=True, ephemeral_system_prompt=MEMORY_SCOPE_GUIDANCE,
                stream_delta_callback=self.stream_delta,
                tool_start_callback=self.tool_start, tool_complete_callback=self.tool_complete,
                step_callback=self.iteration, cwd=str(config.workspace),
            )
        except BaseException:
            self.db.close()
            raise

    def stream_delta(self, text):
        if text is None:
            self.emit('segment_end')
        elif isinstance(text, str) and text:
            self.emit('delta', text=text)

    def tool_start(self, call_id, name, _arguments):
        fields = tool_trace_fields(call_id, name)
        if fields:
            self.emit('telemetry', event='tool_start', **fields)
        self.emit('status', text='Using a tool')

    def tool_complete(self, call_id, name, _arguments, _result):
        fields = tool_trace_fields(call_id, name)
        if fields:
            self.emit('telemetry', event='tool_end', **fields)

    def iteration(self, number, _previous_round):
        self.emit('telemetry', event='model_iteration', iteration=number)

    def run(self, message):
        # Refresh agent-scoped MEMORY/USER/SOUL before every turn.
        self.invalidate_system_prompt(self.agent)
        result = self.agent.run_conversation(
            user_message=message,
            conversation_history=self.db.get_messages_as_conversation(self.agent.session_id),
        )
        if result.get('failed'):
            raise RuntimeError(result.get('error') or 'Hermes turn failed')
        self.save_snapshot()
        return TurnResult(result.get('final_response', ''), result.get('completed', False),
                          result.get('turn_exit_reason', 'incomplete'))

    def save_snapshot(self):
        destination = self.config.state / 'checkpoint.next.db'
        destination.unlink(missing_ok=True)
        with sqlite3.connect(self.database) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        destination.replace(self.config.state / 'checkpoint.db')

    def close(self):
        try:
            self.agent.close()
        finally:
            self.db.close()
