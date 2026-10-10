from types import SimpleNamespace


def _snapshot(state):
    return SimpleNamespace(
        thread=SimpleNamespace(status=SimpleNamespace(type=state)),
    )


def test_persistent_context_is_compacted_before_the_turn(monkeypatch):
    from app import codex_agent

    class Thread:
        def __init__(self):
            self.compact_calls = 0
            self.states = iter(("active", "idle"))

        def compact(self):
            self.compact_calls += 1

        def read(self):
            return _snapshot(next(self.states))

    thread = Thread()
    monkeypatch.setattr(codex_agent.time, "sleep", lambda _: None)

    assert codex_agent._compact_persistent_thread(thread, 8) is True
    assert thread.compact_calls == 1


def test_short_context_does_not_start_compaction():
    from app import codex_agent

    class Thread:
        def compact(self):
            raise AssertionError("compaction should not start")

    assert codex_agent._compact_persistent_thread(Thread(), 7) is False
