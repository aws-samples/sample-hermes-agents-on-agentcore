"""Creation-time execution policy. Missing values retain legacy sequential behavior."""

from typing import Literal

ExecutionMode = Literal['sequential', 'concurrent']


def execution_mode(record) -> ExecutionMode:
    value = record.get('execution_mode', 'sequential')
    if value not in ('sequential', 'concurrent'):
        raise ValueError('Invalid execution mode')
    return value
