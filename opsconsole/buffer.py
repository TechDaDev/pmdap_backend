"""Redis rolling buffer for Railway metrics.

Each service x metric is stored as a capped Redis list of "[ts,value]" JSON
entries (oldest first). Collector status lives in a single hash. All access is
lazy via a redis-py client so the app never needs Redis at import time.

Redis failures surface as redis exceptions; callers (collector/data endpoint)
handle them explicitly so a Redis outage never crashes a request or the task.
"""

from __future__ import annotations

import json
import logging
import os
import time

import redis as redis_lib

logger = logging.getLogger(__name__)

SERIES_PREFIX = "pmdap:ops:railway:metrics:"
COLLECTOR_KEY = "pmdap:ops:railway:collector"
CHAIN_KEY = "pmdap:ops:railway:chain"
DISCOVERY_KEY = "pmdap:ops:railway:services"


def redis_url():
    return os.getenv("DJANGO_CACHE_URL", "redis://localhost:6379/2")


_connection = None


def redis_client():
    """Return a shared redis-py client (thread-safe via connection pool)."""
    global _connection
    if _connection is None:
        _connection = redis_lib.Redis.from_url(
            redis_url(), socket_connect_timeout=2, socket_timeout=3
        )
    return _connection


def series_key(service, metric):
    return "%s%s:%s" % (SERIES_PREFIX, service, metric)


def replace_series(service, metric, points, retention_seconds):
    """Replace a series with points (list of (ts, value)), deduped + sorted asc."""
    conn = redis_client()
    key = series_key(service, metric)
    deduped = {}
    for ts, value in points:
        try:
            deduped[int(ts)] = float(value)
        except (TypeError, ValueError):
            continue
    ordered = sorted(deduped.items())
    conn.delete(key)
    if ordered:
        conn.rpush(key, *[json.dumps([ts, value]) for ts, value in ordered])
        conn.expire(key, int(retention_seconds) + 60)
    return len(ordered)


def read_series(service, metric, max_points=720):
    conn = redis_client()
    key = series_key(service, metric)
    raw = conn.lrange(key, 0, max_points - 1)
    out = []
    for item in raw:
        try:
            ts, value = json.loads(item)
            out.append([int(ts), float(value)])
        except (TypeError, ValueError):
            continue
    return out


def set_collector_status(
    *,
    status,
    updated_at=None,
    last_ok_at=None,
    sample_seconds=None,
    retention_seconds=None,
    next_allowed_at=None,
    services=None,
    detail="",
):
    conn = redis_client()
    now = int(updated_at if updated_at is not None else time.time())
    data = {"status": status, "updated_at": str(now)}
    if last_ok_at:
        data["last_ok_at"] = str(int(last_ok_at))
    if sample_seconds:
        data["sample_seconds"] = str(int(sample_seconds))
    if retention_seconds:
        data["retention_seconds"] = str(int(retention_seconds))
    if next_allowed_at:
        data["next_allowed_at"] = str(float(next_allowed_at))
    if services:
        data["services"] = ",".join(sorted(services))
    if detail:
        data["detail"] = str(detail)[:200]
    conn.hset(COLLECTOR_KEY, mapping=data)
    ttl = (int(retention_seconds) + 600) if retention_seconds else 3600
    conn.expire(COLLECTOR_KEY, ttl)


def get_collector_status():
    conn = redis_client()
    raw = conn.hgetall(COLLECTOR_KEY)
    if not raw:
        return {}
    out = {}
    for key, value in raw.items():
        k = key.decode() if isinstance(key, bytes) else key
        v = value.decode() if isinstance(value, bytes) else value
        out[k] = v
    return out


def acquire_chain(ttl):
    conn = redis_client()
    return bool(conn.set(CHAIN_KEY, "1", nx=True, ex=int(ttl)))


def renew_chain(ttl):
    try:
        conn = redis_client()
        conn.expire(CHAIN_KEY, int(ttl))
    except Exception:  # pragma: no cover - defensive
        logger.warning("ops.railway chain renew failed", exc_info=True)


def cache_services(services, ttl):
    conn = redis_client()
    conn.set(DISCOVERY_KEY, json.dumps(services), ex=int(ttl))


def cached_services():
    conn = redis_client()
    raw = conn.get(DISCOVERY_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
