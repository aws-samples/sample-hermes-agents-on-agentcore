import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from agents.hermes.agent_adapter import configure_memory_scope


def test_hermes_memory_user_and_soul_paths_belong_to_the_selected_agent(monkeypatch, tmp_path):
    class MemoryStore:
        pass

    prompt_builder = ModuleType('agent.prompt_builder')
    original = Mock(spec=lambda context_length=None, home_override=None: None, return_value='shared persona')
    prompt_builder.load_soul_md = original
    monkeypatch.setitem(sys.modules, 'agent', SimpleNamespace(prompt_builder=prompt_builder))
    monkeypatch.setitem(sys.modules, 'tools.memory_tool_store', SimpleNamespace(MemoryStore=MemoryStore))
    shared = tmp_path / 'agent-shared'
    configure_memory_scope(shared)
    assert MemoryStore._path_for('memory') == shared / 'MEMORY.md'
    assert MemoryStore._path_for('user') == shared / 'USER.md'
    assert prompt_builder.load_soul_md(1000, home_override=Path('/some-other-profile')) == 'shared persona'
    original.assert_called_once_with(context_length=1000, home_override=shared)
    # Reconfiguration in a test must not stack wrappers or keep the old shared home.
    other_shared = tmp_path / 'another-agent'
    configure_memory_scope(other_shared)
    assert MemoryStore._path_for('memory') == other_shared / 'MEMORY.md'
    assert MemoryStore._path_for('user') == other_shared / 'USER.md'
    prompt_builder.load_soul_md()
    original.assert_called_with(context_length=None, home_override=other_shared)
