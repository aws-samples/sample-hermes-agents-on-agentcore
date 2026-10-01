"""Sandbox-side agent contract. Standard library only; no framework or AWS imports."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

RUN_BUDGET_SECONDS = 60 * 60


@dataclass(frozen=True)
class AgentConfig:
    conversation_id: str
    model: str
    max_output_tokens: int = 0
    state: Path = Path('/state')
    workspace: Path = Path('/workspace/workspace')
    shared_agent: Path = Path('/shared/agent')
    shared_skills: Path = Path('/shared/skills')
    model_url: str = 'http://127.0.0.1:9001'
    model_api_key: str = 'sandbox-broker'
    run_budget_seconds: int = RUN_BUDGET_SECONDS

@dataclass(frozen=True)
class TurnResult:
    text: str
    completed: bool = True
    reason: str = 'incomplete'


class AgentAdapter(Protocol):
    """One instance per conversation, constructed only after sandbox restrictions.

    __init__(config: AgentConfig, emit: Callable) initializes or restores private state.
    emit accepts delta, segment_end, status and bounded telemetry events. The bootstrap
    owns request IDs and ready/error/completion events. Persistence is agent-specific.
    """

    def estimate_tokens(self, text: str) -> int: ...

    def run(self, message: str) -> TurnResult: ...

    def close(self) -> None: ...


AdapterFactory = Callable[[AgentConfig, Callable], AgentAdapter]
