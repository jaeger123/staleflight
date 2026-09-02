"""staleflight: serve-stale singleflight caching (RFC 5861 for callables)."""

from staleflight.core import Snapshot, SWRCache, swr

__all__ = ["SWRCache", "Snapshot", "swr"]
__version__ = "0.1.0"
