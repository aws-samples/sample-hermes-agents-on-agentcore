"""Minimal framework-free reference adapter; deliberately makes no model calls."""

from runtime.contract import TurnResult


class Adapter:
    def __init__(self, config, emit):
        self.emit = emit
        self.messages = []

    def estimate_tokens(self, text):
        return max(1, (len(text) + 3) // 4)

    def run(self, message):
        self.messages.append(message)
        text = f'Turn {len(self.messages)}: {message}'
        self.emit('delta', text=text)
        return TurnResult(text)

    def close(self):
        pass
