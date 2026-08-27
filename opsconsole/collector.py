"""Self-rescheduling Railway metrics collector.

A single collector chain runs at the configured sample interval (default 5s),
backed by a Redis SETNX guard so multiple web/worker processes never create
duplicate chains. The task re-schedules itself with ``apply_async(countdown=...)``
so no ``celery beat`` process is required.

Failure handling:
  * RATE_LIMITED  -> exponential backoff stored in collector status (no Retry-After)
  * CONFIG_ERROR  -> collector stops hammering; status shown in the dashboard
  * UPSTREAM_ERROR -> stale buffer retained so the dashboard can still render
  * Redis outage   -> status REDIS_UNAVAILABLE, safe no-op

All values are metric numbers + service names only. No tokens or credentials
are ever written to logs or Redis beyond the required status fields.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from celery import shared_task

from opsconsole import buffer
from opsconsole.railway_client import (
    ALL_MEASUREMENTS,
    RailwayMetricsClient,
    RailwayMetricsError,
)

logger = logging.getLogger(__name__)

DISCOVERY_TTL = 300  # seconds between service-discovery refreshes
MAX_BACKOFF = 300  # cap on exponential backoff (seconds)


def get_sample_seconds():
    # Railway's metrics API rejects sample rates below 30s ("Invalid input").
    return max(30, int(os.getenv("RAILWAY_METRICS_SAMPLE_SECONDS", "30")))


def get_retention_seconds():
    return max(60, int(os.getenv("RAILWAY_METRICS_RETENTION_SECONDS", "1800")))


def chain_ttl():
    return max(90, get_sample_seconds() * 4)


def ensure_collector():
    """Bootstrap the single collector chain (called from AppConfig.ready())."""
    if os.getenv("RAILWAY_METRICS_ENABLED", "").strip().lower() != "true":
        return
    try:
        if buffer.acquire_chain(chain_ttl()):
            collect_railway_metrics.delay()
    except Exception:  # pragma: no cover - Redis unavailable at startup
        logger.warning("ops.railway collector bootstrap unavailable", exc_info=True)


def _discover(client):
    services = buffer.cached_services()
    if services:
        return services
    services = client.discover_services()
    if services:
        try:
            buffer.cache_services(services, DISCOVERY_TTL)
        except Exception:  # pragma: no cover - defensive
            pass
    return services


def _backoff(interval):
    return min(MAX_BACKOFF, max(get_sample_seconds(), interval * 2))


@shared_task(name="ops.railway.collect_metrics", queue="celery", ignore_result=True)
def collect_railway_metrics():
    client = RailwayMetricsClient()
    if not client.enabled:
        return
    interval = get_sample_seconds()
    retention = get_retention_seconds()
    next_interval = interval
    services = []

    try:
        now = int(time.time())
        status = buffer.get_collector_status()
        next_allowed = status.get("next_allowed_at")
        if next_allowed:
            try:
                if now < float(next_allowed):
                    next_interval = max(1, float(next_allowed) - now)
                    buffer.renew_chain(chain_ttl())
                    # NOTE: do NOT schedule here. The finally block below
                    # schedules exactly ONE successor; scheduling here too
                    # would spawn 2 tasks per execution (exponential growth).
                    return
            except (TypeError, ValueError):
                pass

        services = _discover(client)
        if not services:
            raise RailwayMetricsError("CONFIG_ERROR", "no services discovered")

        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=retention)
        payload = client.fetch_all_metrics(
            services, ALL_MEASUREMENTS, start, end, interval
        )
        total = 0
        for service in payload:
            for metric, points in payload[service].items():
                total += buffer.replace_series(service, metric, points, retention)

        buffer.set_collector_status(
            status="OK",
            updated_at=now,
            last_ok_at=now,
            sample_seconds=interval,
            retention_seconds=retention,
            services=[s["name"] for s in services],
        )
        logger.info(
            "ops.railway metrics OK services=%d points=%d", len(payload), total
        )

    except RailwayMetricsError as exc:
        logger.warning("ops.railway collector %s: %s", exc.code, exc.detail)
        if exc.code == "RATE_LIMITED":
            next_interval = _backoff(interval)
            # Railway suggests a concrete retry window in the error text
            # (e.g. "Please retry in 120 seconds").
            match = re.search(r"retry in (\d+) seconds", exc.detail or "", re.IGNORECASE)
            if match:
                next_interval = int(match.group(1))
            buffer.set_collector_status(
                status="RATE_LIMITED",
                next_allowed_at=time.time() + next_interval,
                sample_seconds=interval,
                retention_seconds=retention,
                services=[s["name"] for s in services],
            )
        else:
            buffer.set_collector_status(
                status=exc.code,
                sample_seconds=interval,
                retention_seconds=retention,
                services=[s["name"] for s in services],
                detail=exc.detail,
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("ops.railway collector failure: %s", exc)
        try:
            buffer.set_collector_status(
                status="UPSTREAM_ERROR",
                sample_seconds=interval,
                retention_seconds=retention,
                services=[s["name"] for s in services],
            )
        except Exception:
            pass
    finally:
        try:
            buffer.renew_chain(chain_ttl())
        except Exception:  # pragma: no cover - defensive
            pass
        collect_railway_metrics.apply_async(countdown=max(1, int(next_interval)))
