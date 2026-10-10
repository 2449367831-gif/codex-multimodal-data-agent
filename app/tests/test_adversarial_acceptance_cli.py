"""CLI options are never sent to the agent as business questions."""

import json

import pytest

from tests.acceptance import check_adversarial_variants_live as live


def test_timeout_is_an_option_not_a_question():
    args = live.parse_args(["--timeout", "180"])
    assert args.timeout == 180 and args.questions == []
    args = live.parse_args(["--timeout", "60", "business question"])
    assert args.questions == ["business question"]


@pytest.mark.parametrize("args", [["--unknown"], ["--timeout", "0"], ["--timeout", "1801"]])
def test_invalid_options_fail_before_network(args, monkeypatch):
    monkeypatch.setattr(live, "ChatClient", lambda *_: pytest.fail("network called"))
    with pytest.raises(SystemExit) as error:
        live.main(args)
    assert error.value.code == 2


def test_observation_timeout_keeps_same_task_without_resubmitting(monkeypatch, capsys):
    calls = []
    current = [0]

    class Client:
        def __init__(self, *_):
            pass

        def request(self, path, *args):
            calls.append(path)
            if path == "/v1/session":
                return {"conversation_id": "test", "csrf_token": "test"}
            return {"task_id": "task_test", "state": "running"}

    monkeypatch.setattr(live, "ChatClient", Client)
    monkeypatch.setattr(live.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(live.time, "sleep", lambda seconds: current.__setitem__(0, current[0] + seconds))
    assert live.main(["--timeout", "30", "question"]) == 2
    assert calls.count("/v1/tasks") == 1
    assert set(calls[2:]) == {"/v1/tasks/task_test"}
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["verification"] == "incomplete" and final["state"] == "running"
