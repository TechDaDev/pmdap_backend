"""Server monitor admin views (dashboard page + JSON data endpoint).

The data endpoint reads ONLY the Redis rolling buffer; it never calls Railway
and never exposes the Railway token to the browser.

Authorization: staff AND (superuser OR opsconsole.view_server_monitor permission).
"""

from __future__ import annotations

import logging
import time

from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from opsconsole import buffer
from opsconsole.collector import get_retention_seconds, get_sample_seconds
from opsconsole.context import admin_context
from opsconsole.railway_client import (
    ALL_MEASUREMENTS,
    RailwayMetricsClient,
)

logger = logging.getLogger(__name__)


def _can_view_monitor(user):
    return user.is_active and (
        user.is_superuser or user.has_perm("opsconsole.view_server_monitor")
    )


def _monitor_guard(user):
    if not _can_view_monitor(user):
        raise PermissionDenied


@require_GET
def server_monitor(request):
    _monitor_guard(request.user)
    client = RailwayMetricsClient()
    return render(
        request,
        "admin/ops/server_monitor.html",
        admin_context(
            request,
            configured=client.enabled,
            sample_seconds=get_sample_seconds(),
            retention_seconds=get_retention_seconds(),
        ),
    )


@require_GET
def server_monitor_data(request):
    _monitor_guard(request.user)
    now = int(time.time())
    client = RailwayMetricsClient()
    if not client.enabled:
        return _json({"status": "DISABLED", "now": now})
    try:
        status = buffer.get_collector_status()
        service_names = [
            name for name in (status.get("services") or "").split(",") if name
        ]
        services = {}
        for name in service_names:
            services[name] = {
                metric: buffer.read_series(name, metric)
                for metric in ALL_MEASUREMENTS
            }
        payload = {
            "status": status.get("status", "STALE"),
            "updated_at": int(status.get("updated_at") or 0),
            "last_ok_at": int(status.get("last_ok_at") or 0),
            "sample_seconds": get_sample_seconds(),
            "retention_seconds": get_retention_seconds(),
            "services": services,
            "now": now,
        }
        return _json(payload)
    except Exception as exc:
        logger.warning("ops.railway data endpoint unavailable: %s", exc)
        return _json({"status": "REDIS_UNAVAILABLE", "now": now}, status=503)


def _json(payload, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Robots-Tag"] = "noindex"
    return response
