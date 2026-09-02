# staleflight

**Serve-stale singleflight caching for Python — RFC 5861's `stale-while-revalidate` and `stale-if-error`, for plain callables instead of HTTP.**

Zero dependencies. Fully typed (PEP 561). Python 3.11+.

```python
from staleflight import swr


@swr(ttl=5.0)
def mesh_health() -> dict:
    return probe_all_the_things()  # expensive: network calls, big queries…


snapshot = mesh_health.get()  # Snapshot(value=..., created_at=...) or None
```

## The semantics, precisely

| State | What a caller gets |
|---|---|
| **fresh** (age < ttl) | the cached snapshot, ~0 cost |
| **stale** | exactly **one** caller recomputes; every concurrent caller is served the **previous snapshot immediately** — nobody waits behind the refresh |
| **refresh fails** | the previous snapshot keeps being served and keeps aging; the error lands on `cache.last_error` and your `on_error` callback |
| **empty** (first ever call) | the first caller computes; concurrent callers get `None` and decide what "not yet" means for them |

This is the **non-blocking** flavor of stampede protection. Most caching libraries offer the blocking flavor — losers park behind the winner's lock until the new value exists. That is the right choice when every reader must see the newest value, and the wrong one when bounded *latency* matters more than bounded *staleness*: readiness gates, dashboards, feature flags, config lookups. staleflight is for the second family.

## Why not …

*(Survey of 17 caching libraries, 2026. Corrections welcome.)*

- **dogpile.cache** — the one mature library with this exact serve-stale semantic (`get_or_create`). Adopt it if you want regions, backends and invalidation strategies; staleflight is for when you want the 150-line version with no dependencies and stale-if-error built in (dogpile propagates creator failures to the winning caller).
- **cachetools** — TTL only; its stampede options make concurrent callers *wait*. No stale-serving.
- **PyPI `singleflight` ports** — deduplicate concurrent calls but block the losers, and cache nothing.
- **cachier / diskcache / requests-cache** — respectively: immature memory backend; probabilistic *early* refresh that still blocks after full expiry; SWR for HTTP responses only.

## API

```python
from staleflight import SWRCache, Snapshot, swr

cache = SWRCache(source, ttl=5.0, on_error=log_it, clock=time.monotonic)

cache.get()  # Snapshot | None — refreshes at most singleflight-once, never raises
cache.peek()  # Snapshot | None — never triggers work
cache.refresh()  # force recompute now; raises on failure (a command, not a read)
cache.age()  # seconds since compute; float('inf') when empty
cache.invalidate()  # drop the snapshot
cache.last_error  # BaseException | None from the most recent failed get()-refresh
```

`Snapshot` is a frozen dataclass `(value, created_at)`; `created_at` is on the cache's (monotonic) clock. The `clock` parameter makes every behavior testable without sleeps — see the test suite.

## Thread-safety

Publication is a single reference assignment of an immutable `Snapshot`: readers observe the old snapshot or the new one, never a torn value. Safe under CPython's GIL (every currently supported default build). On free-threaded builds (PEP 703) reference-swap visibility is implementation behavior, not contract — wrap access in your own lock there.

## Non-goals

Keyed/memoizing caches, async, eviction policies, external backends, background refresh threads. One value, one callable, one file. If you need more, you want dogpile.cache.

## License

MIT
