"""Serve-stale singleflight caching for expensive zero-argument callables.

This module implements the semantics of RFC 5861 (``stale-while-revalidate``
and ``stale-if-error``) for plain Python callables:

fresh
    The cached snapshot is served; no work happens.
stale
    Exactly one caller recomputes. Every caller that arrives while the
    recomputation is in flight is served the previous snapshot immediately
    instead of waiting (``stale-while-revalidate``).
refresh failure
    The previous snapshot keeps being served and keeps aging
    (``stale-if-error``). The error is recorded and optionally reported.
empty
    The very first caller computes; callers that arrive during that first
    computation receive ``None`` and decide for themselves what "not yet"
    means in their domain.

The design is intentionally the opposite of a blocking dogpile lock: under
load, no caller is ever parked behind another caller's slow recomputation.
That trade -- bounded staleness in exchange for bounded latency -- is the
right one for readiness gates, dashboards, and feature-flag style lookups,
and the wrong one when every caller must observe the newest value.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

__all__ = ["SWRCache", "Snapshot", "swr"]

T = TypeVar("T")

Clock = Callable[[], float]
ErrorCallback = Callable[[BaseException], None]


@dataclass(frozen=True, slots=True)
class Snapshot(Generic[T]):
    """An immutable value paired with the instant it was computed.

    ``created_at`` is expressed on the cache's clock (monotonic by
    default), so it is suitable for measuring age, not for display.
    """

    value: T
    created_at: float


class SWRCache(Generic[T]):
    """A serve-stale singleflight cache around one zero-argument callable.

    One instance guards one value. Publication is a single reference
    assignment of an immutable :class:`Snapshot`, so readers in other
    threads always observe either the previous snapshot or the new one,
    never a partially built value.

    Thread-safety: safe under CPython's GIL, which every current supported
    runtime has. A free-threaded (PEP 703) deployment should wrap
    ``get``/``peek`` in a lock of its own; reference-swap atomicity is
    implementation behaviour there, not a documented guarantee.

    Args:
        source: The expensive callable producing the value.
        ttl: Seconds a snapshot is considered fresh. Must be positive.
        on_error: Called with the exception when a refresh triggered by
            :meth:`get` fails. Failures never propagate out of ``get``;
            they surface here and on :attr:`last_error`.
        clock: Monotonic time source; injectable for tests.
    """

    def __init__(
        self,
        source: Callable[[], T],
        *,
        ttl: float,
        on_error: ErrorCallback | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        if ttl <= 0:
            raise ValueError(f"ttl must be positive, got {ttl!r}")
        self._source = source
        self._ttl = float(ttl)
        self._on_error = on_error
        self._clock = clock
        self._snapshot: Snapshot[T] | None = None
        self._refresh_lock = threading.Lock()
        #: The exception from the most recent failed refresh, cleared by the
        #: next successful one. Advisory: read it for diagnostics, not logic.
        self.last_error: BaseException | None = None

    @property
    def ttl(self) -> float:
        """Seconds a snapshot is served without triggering a refresh."""
        return self._ttl

    def get(self) -> Snapshot[T] | None:
        """Return a snapshot, refreshing at most once per staleness.

        Never blocks on another caller's refresh and never raises on a
        failed refresh; see the module docstring for the full semantics.
        Returns ``None`` only while the very first computation has not yet
        completed.
        """
        snapshot = self._snapshot
        if snapshot is not None and self.age() < self._ttl:
            return snapshot
        if self._refresh_lock.acquire(blocking=False):
            try:
                return self.refresh()
            except Exception as error:  # noqa: BLE001 -- stale-if-error is the contract
                self.last_error = error
                if self._on_error is not None:
                    self._on_error(error)
            finally:
                self._refresh_lock.release()
        # Lock was busy (another caller is refreshing) or the refresh
        # failed: the previous snapshot -- possibly None -- is the answer.
        return self._snapshot

    def refresh(self) -> Snapshot[T]:
        """Recompute synchronously and publish, regardless of freshness.

        Unlike :meth:`get`, a failure here propagates: an explicit refresh
        is a command, not a read. Concurrent explicit refreshes are not
        deduplicated; the last one to finish wins the publication.
        """
        snapshot = Snapshot(value=self._source(), created_at=self._clock())
        self._snapshot = snapshot
        self.last_error = None
        return snapshot

    def peek(self) -> Snapshot[T] | None:
        """Return the current snapshot, fresh or stale, without any work."""
        return self._snapshot

    def age(self) -> float:
        """Seconds since the current snapshot was computed.

        Returns ``float('inf')`` when nothing has been computed yet, so the
        result is always comparable against a TTL or staleness budget.
        """
        snapshot = self._snapshot
        if snapshot is None:
            return float("inf")
        return self._clock() - snapshot.created_at

    def invalidate(self) -> None:
        """Drop the snapshot; the next :meth:`get` recomputes from empty."""
        self._snapshot = None


def swr(
    *,
    ttl: float,
    on_error: ErrorCallback | None = None,
    clock: Clock = time.monotonic,
) -> Callable[[Callable[[], T]], SWRCache[T]]:
    """Build an :class:`SWRCache` in decorator position.

    The decorated name is *replaced by the cache instance*, which makes the
    call-site semantics explicit -- ``config.get()`` returns a
    :class:`Snapshot`, not a bare value::

        @swr(ttl=5.0)
        def mesh_health() -> dict[str, bool]:
            return probe_everything()

        snapshot = mesh_health.get()
    """

    def wrap(source: Callable[[], T]) -> SWRCache[T]:
        return SWRCache(source, ttl=ttl, on_error=on_error, clock=clock)

    return wrap
