import pytest

from vigil.state import Health, TargetState
from vigil.stats import percentile, summarize


def feed(state, pattern, start=0.0, step=10.0):
    transitions = []
    for i, ch in enumerate(pattern):
        tr = state.observe(ch == "1", "x", now=start + i * step)
        if tr:
            transitions.append(tr)
    return transitions


def test_first_success_shows_up_immediately():
    s = TargetState("t", fail_after=3, recover_after=2)
    [tr] = feed(s, "1")
    assert (tr.old, tr.new) == (Health.UNKNOWN, Health.UP)


def test_single_failure_at_startup_does_not_mark_down():
    s = TargetState("t", fail_after=3, recover_after=2)
    assert feed(s, "0") == []
    assert s.health is Health.UNKNOWN
    [tr] = feed(s, "1")
    assert (tr.old, tr.new) == (Health.UNKNOWN, Health.UP)


def test_startup_goes_down_after_fail_after_failures():
    s = TargetState("t", fail_after=3, recover_after=2)
    [tr] = feed(s, "000")
    assert (tr.old, tr.new) == (Health.UNKNOWN, Health.DOWN)


def test_isolated_failures_do_not_flip_state():
    s = TargetState("t", fail_after=3, recover_after=2)
    transitions = feed(s, "1101101101")
    assert [t.new for t in transitions] == [Health.UP]
    assert s.health is Health.UP


def test_down_needs_n_consecutive_failures_and_up_needs_m_successes():
    s = TargetState("t", fail_after=3, recover_after=2)
    transitions = feed(s, "1" "000" "1" "0" "11")
    assert [t.new for t in transitions] == [Health.UP, Health.DOWN, Health.UP]


def test_repeated_transitions_inside_window_are_flagged_as_flapping():
    s = TargetState("t", fail_after=1, recover_after=1, flap_window=600, flap_threshold=4)
    transitions = feed(s, "1010101", step=10)
    assert not transitions[1].flapping
    assert transitions[-1].flapping
    assert s.is_flapping


def test_flapping_clears_once_changes_age_out():
    s = TargetState("t", fail_after=1, recover_after=1, flap_window=60, flap_threshold=3)
    feed(s, "10101", step=5)
    assert s.is_flapping
    feed(s, "0", start=10_000)
    assert not s.is_flapping


@pytest.mark.parametrize("p, expected", [(0, 1), (50, 3), (100, 5), (25, 2)])
def test_percentile_interpolates(p, expected):
    assert percentile([5, 1, 4, 2, 3], p) == expected


def test_percentile_of_empty_is_none():
    assert percentile([], 95) is None


def test_mean_would_hide_the_tail_but_p95_does_not():
    lat = [2.0] * 95 + [800.0] * 5
    s = summarize([True] * 100, lat, availability_slo=99.9)
    assert s.p50 == 2.0
    assert s.p99 == 800.0


def test_error_budget():
    oks = [True] * 9996 + [False] * 4
    s = summarize(oks, [1.0] * 10_000, availability_slo=99.9)
    assert s.availability == 99.96
    assert s.error_budget_remaining == 60.0


def test_budget_goes_negative_when_slo_breached():
    s = summarize([True] * 98 + [False] * 2, [1.0] * 100, availability_slo=99.0)
    assert s.error_budget_remaining == -100.0


def test_failures_count_against_latency_slo():
    s = summarize([True, True, False, True], [10, 300, None, 20], 99.0, latency_slo_ms=100)
    assert s.latency_slo_compliance == 50.0
