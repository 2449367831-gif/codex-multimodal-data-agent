from app.model_turn_stream import TurnBudget


def test_compaction_time_is_credited_but_capped():
    now = [0.0]
    budget = TurnBudget(100, 30, clock=lambda: now[0])
    now[0] = 95
    assert budget.remaining() == 5
    budget.compaction_started("compact-1")
    now[0] = 125
    budget.compaction_completed("compact-1")
    assert budget.remaining() == 5
    now[0] = 160
    assert budget.remaining() == 0


def test_duplicate_or_unmatched_compaction_events_do_not_add_credit():
    now = [0.0]
    budget = TurnBudget(60, 20, clock=lambda: now[0])
    budget.compaction_completed("unknown")
    budget.compaction_started("compact-1")
    budget.compaction_started("compact-1")
    now[0] = 10
    budget.compaction_completed("compact-2")
    assert budget.remaining() == 60
    budget.compaction_completed("compact-1")
    assert budget.remaining() == 60
    budget.compaction_started("compact-1")
    now[0] = 30
    budget.compaction_completed("compact-1")
    assert budget.remaining() == 40


def test_compaction_allowance_is_consumed_only_after_observed_time():
    now = [0.0]
    budget = TurnBudget(60, 20, clock=lambda: now[0])
    budget.compaction_started("compact-1")
    now[0] = 100
    assert budget.remaining() == 0
