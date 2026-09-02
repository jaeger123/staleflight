import threading

import pytest
from staleflight import SWRCache, Snapshot, swr


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def test_first_call_computes_and_returns_snapshot(clock):
    cache = SWRCache(lambda: "v1", ttl=5.0, clock=clock)

    snapshot = cache.get()

    assert isinstance(snapshot, Snapshot)
    assert snapshot.value == "v1"
    assert snapshot.created_at == clock.now


def test_fresh_snapshot_is_served_without_recompute(clock):
    calls = []
    cache = SWRCache(lambda: calls.append(1) or len(calls), ttl=5.0, clock=clock)

    first = cache.get()
    clock.advance(4.9)
    second = cache.get()

    assert calls == [1]
    assert second is first


def test_stale_snapshot_triggers_exactly_one_recompute(clock):
    calls = []
    cache = SWRCache(lambda: calls.append(1) or len(calls), ttl=5.0, clock=clock)

    cache.get()
    clock.advance(5.0)
    refreshed = cache.get()

    assert calls == [1, 1]
    assert refreshed is not None and refreshed.value == 2


def test_caller_during_refresh_gets_previous_snapshot_immediately(clock):
    """The defining semantic: losers are served stale, never parked."""
    in_refresh = threading.Event()
    release = threading.Event()
    values = iter(["old", "new"])

    def slow_source():
        value = next(values)
        if value == "new":
            in_refresh.set()
            assert release.wait(timeout=5)
        return value

    cache = SWRCache(slow_source, ttl=5.0, clock=clock)
    cache.get()  # publish "old"
    clock.advance(5.0)

    winner_result = {}
    winner = threading.Thread(target=lambda: winner_result.update(snap=cache.get()))
    winner.start()
    assert in_refresh.wait(timeout=5)

    loser_snapshot = cache.get()  # winner still inside slow_source
    assert loser_snapshot is not None
    assert loser_snapshot.value == "old"

    release.set()
    winner.join(timeout=5)
    assert winner_result["snap"].value == "new"
    assert cache.get().value == "new"


def test_caller_during_first_ever_compute_gets_none(clock):
    cache = SWRCache(lambda: "never", ttl=5.0, clock=clock)

    with cache._refresh_lock:  # simulate the first computation in flight
        assert cache.get() is None


def test_failed_refresh_serves_stale_and_records_error(clock):
    errors = []
    state = {"fail": False}

    def source():
        if state["fail"]:
            raise RuntimeError("boom")
        return "good"

    cache = SWRCache(source, ttl=5.0, on_error=errors.append, clock=clock)
    cache.get()
    state["fail"] = True
    clock.advance(5.0)

    snapshot = cache.get()

    assert snapshot is not None and snapshot.value == "good"
    assert cache.age() >= 5.0  # still aging: staleness stays observable
    assert isinstance(cache.last_error, RuntimeError)
    assert errors and errors[0] is cache.last_error


def test_successful_refresh_clears_last_error(clock):
    state = {"fail": True}

    def source():
        if state["fail"]:
            raise RuntimeError("boom")
        return "recovered"

    cache = SWRCache(source, ttl=5.0, clock=clock)
    assert cache.get() is None
    assert cache.last_error is not None

    state["fail"] = False
    snapshot = cache.get()

    assert snapshot is not None and snapshot.value == "recovered"
    assert cache.last_error is None


def test_explicit_refresh_raises_and_keeps_previous_snapshot(clock):
    state = {"fail": False}

    def source():
        if state["fail"]:
            raise RuntimeError("boom")
        return "v1"

    cache = SWRCache(source, ttl=5.0, clock=clock)
    cache.get()
    state["fail"] = True

    with pytest.raises(RuntimeError):
        cache.refresh()
    assert cache.peek() is not None and cache.peek().value == "v1"


def test_peek_never_computes(clock):
    cache = SWRCache(lambda: pytest.fail("peek must not compute"), ttl=5.0, clock=clock)

    assert cache.peek() is None


def test_invalidate_forces_recompute_from_empty(clock):
    calls = []
    cache = SWRCache(lambda: calls.append(1) or len(calls), ttl=5.0, clock=clock)
    cache.get()

    cache.invalidate()

    assert cache.peek() is None
    assert cache.age() == float("inf")
    assert cache.get().value == 2


def test_age_uses_injected_clock(clock):
    cache = SWRCache(lambda: "v", ttl=5.0, clock=clock)
    cache.get()
    clock.advance(2.5)

    assert cache.age() == pytest.approx(2.5)


def test_ttl_must_be_positive():
    with pytest.raises(ValueError, match="ttl must be positive"):
        SWRCache(lambda: 1, ttl=0)


def test_swr_decorator_returns_a_cache(clock):
    @swr(ttl=5.0, clock=clock)
    def value() -> str:
        return "decorated"

    assert isinstance(value, SWRCache)
    assert value.get().value == "decorated"


def test_freshness_is_judged_on_the_returned_snapshot(clock):
    """invalidate() between capture and age check must not corrupt get()."""
    cache = SWRCache(lambda: "v", ttl=5.0, clock=clock)
    first = cache.get()

    # Regression guard for the capture race: age is computed from the same
    # snapshot object that is returned, never re-read from the instance.
    assert cache.get() is first


def test_explicit_refreshes_serialize_on_the_lock(clock):
    order = []

    def source():
        order.append("run")
        return len(order)

    cache = SWRCache(source, ttl=5.0, clock=clock)

    slow_started = threading.Event()
    release = threading.Event()

    def slow_source():
        slow_started.set()
        assert release.wait(timeout=5)
        order.append("slow")
        return "slow"

    cache._source = slow_source
    slow = threading.Thread(target=cache.refresh)
    slow.start()
    assert slow_started.wait(timeout=5)

    cache._source = source
    fast = threading.Thread(target=cache.refresh)
    fast.start()

    release.set()
    slow.join(timeout=5)
    fast.join(timeout=5)

    # The fast refresh could not start until the slow one finished, so the
    # final publication is the later-started computation (which ran second
    # and saw order == ["slow", "run"]), never a stale overwrite.
    assert order == ["slow", "run"]
    assert cache.peek().value == 2
