"""Railway metrics GraphQL client (server-side only).

Talks to Railway's metrics API. The token is read from environment variables
server-side and is NEVER exposed to the browser or logged.

Verified schema facts (live introspection, see docs/architecture):
  * Query.project(id: String!) -> Project { id name services { edges { node { id name } } } }
  * Query.metrics(serviceId: String!, startDate: DateTime!, endDate: DateTime!,
                  measurements: [MetricMeasurement!]!, sampleRateSeconds: Int,
                  environmentId: String)
      -> [MetricsResult!]! with MetricsResult { tags {...} values: [Metric!]! }
      and Metric { ts: Int (epoch seconds), value: Float }.
      One MetricsResult is returned PER MEASUREMENT, in the requested order.
  * MetricMeasurement enum: CPU_LIMIT, CPU_USAGE, CPU_USAGE_2, MEMORY_LIMIT_GB,
    MEMORY_USAGE_GB, DISK_USAGE_GB, EPHEMERAL_DISK_USAGE_GB, NETWORK_RX_GB,
    NETWORK_TX_GB, BACKUP_USAGE_GB, MEASUREMENT_UNSPECIFIED.
  * No rate-limit headers are exposed, so the collector uses a conservative
    default sample interval plus exponential backoff on 429 (no Retry-After).

Auth: RAILWAY_METRICS_TOKEN_TYPE=bearer (default, account token) or
      =project (Project-Access-Token header).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

RAILWAY_GRAPHQL_URL = os.getenv(
    "RAILWAY_GRAPHQL_URL", "https://backboard.railway.com/graphql/v2"
)

# Cloudflare in front of backboard returns HTTP 403 (error 1010) for the
# default urllib user-agent, so send a standard browser signature.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Metric names the monitor charts. Units are documented next to each constant.
CPU_USAGE = "CPU_USAGE"  # vCPU
MEMORY_USAGE_GB = "MEMORY_USAGE_GB"  # GiB
NETWORK_RX_GB = "NETWORK_RX_GB"  # GiB, cumulative counter
NETWORK_TX_GB = "NETWORK_TX_GB"  # GiB, cumulative counter
DISK_USAGE_GB = "DISK_USAGE_GB"  # GiB

ALL_MEASUREMENTS = [
    CPU_USAGE,
    MEMORY_USAGE_GB,
    NETWORK_RX_GB,
    NETWORK_TX_GB,
    DISK_USAGE_GB,
]


class RailwayMetricsError(Exception):
    """Raised for any upstream failure; carries a stable machine code.

    Codes:
      * CONFIG_ERROR    - auth/token/project configuration problem (401 or GraphQL auth error)
      * RATE_LIMITED    - 429 from upstream (apply backoff)
      * UPSTREAM_ERROR  - 5xx / parse / unexpected error (retain stale cache)
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


class RailwayMetricsClient:
    """Minimal GraphQL client for the Railway metrics API."""

    def __init__(
        self,
        *,
        token=None,
        token_type=None,
        project_id=None,
        environment_id=None,
    ):
        self.token = token if token is not None else os.getenv("RAILWAY_METRICS_TOKEN", "")
        self.token_type = (
            token_type or os.getenv("RAILWAY_METRICS_TOKEN_TYPE", "bearer")
        ).strip().lower()
        self.project_id = project_id or os.getenv("RAILWAY_METRICS_PROJECT_ID", "")
        self.environment_id = environment_id or os.getenv(
            "RAILWAY_METRICS_ENVIRONMENT_ID", ""
        )

    @property
    def enabled(self) -> bool:
        return os.getenv("RAILWAY_METRICS_ENABLED", "").strip().lower() == "true"

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self.token_type == "project":
            headers["Project-Access-Token"] = self.token
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _post(self, query, variables):
        if not self.token:
            raise RailwayMetricsError("CONFIG_ERROR", "RAILWAY_METRICS_TOKEN is not set")
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            RAILWAY_GRAPHQL_URL,
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status = response.status
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 401:
                raise RailwayMetricsError("CONFIG_ERROR", "unauthorized (401)") from exc
            if status == 429:
                raise RailwayMetricsError("RATE_LIMITED", "rate limited (429)") from exc
            raise RailwayMetricsError(
                "UPSTREAM_ERROR", f"upstream error ({status})"
            ) from exc
        except urllib.error.URLError as exc:
            raise RailwayMetricsError("UPSTREAM_ERROR", str(exc.reason)) from exc
        except OSError as exc:
            raise RailwayMetricsError("UPSTREAM_ERROR", str(exc)) from exc
        if status >= 500:
            raise RailwayMetricsError("UPSTREAM_ERROR", f"upstream error ({status})")
        try:
            payload_data = json.loads(payload)
        except ValueError as exc:
            raise RailwayMetricsError("UPSTREAM_ERROR", "invalid JSON response") from exc
        errors = payload_data.get("errors")
        if errors:
            message = (errors[0].get("message", "") or "")[:200]
            lowered = message.lower()
            if "too many" in lowered or "rate limit" in lowered:
                raise RailwayMetricsError("RATE_LIMITED", message)
            raise RailwayMetricsError("CONFIG_ERROR", message)
        return payload_data.get("data") or {}

    def discover_services(self):
        """Return [{id, name}] for every service in the configured project."""
        data = self._post(
            """query($pid:String!) {
                project(id:$pid) {
                  id
                  name
                  services { edges { node { id name } } }
                }
              }""",
            {"pid": self.project_id},
        )
        project = data.get("project") or {}
        nodes = []
        for edge in (project.get("services") or {}).get("edges", []):
            node = edge.get("node") or {}
            if node.get("id"):
                nodes.append({"id": node["id"], "name": node.get("name") or node["id"]})
        return nodes

    def fetch_all_metrics(
        self,
        services,
        measurements,
        start_dt,
        end_dt,
        sample_rate_seconds,
    ):
        """Fetch metrics for all services sequentially (one HTTP call each).

        Railway enforces a limit of 19 concurrent metric queries per client,
        and a single ``metrics`` call counts as one metric query PER requested
        measurement. Batching many services/measurements into aliases exceeded
        that limit (4 services x 5 measurements = 20), so services are fetched
        one at a time (max 5 concurrent metric queries per call).

        Returns {service_name: {measurement: [(ts_epoch, value), ...], ...}}.
        """
        out = {}
        for svc in services:
            name = svc.get("name") or svc.get("id")
            out[name] = self._fetch_service_metrics(
                svc["id"], measurements, start_dt, end_dt, sample_rate_seconds
            )
        return out

    def _fetch_service_metrics(
        self, service_id, measurements, start_dt, end_dt, sample_rate_seconds
    ):
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
        # MetricMeasurement is a GraphQL enum: values must be bare identifiers
        # (NOT quoted strings) when inlined as literals in the query text.
        enum_list = ",".join(measurements)
        env_part = ""
        if self.environment_id:
            env_part = ',environmentId:"%s"' % self.environment_id
        query = (
            "query{ metrics(serviceId:%s,startDate:%s,endDate:%s,"
            "measurements:[%s],sampleRateSeconds:%d%s){ values { ts value } } }"
            % (
                json_quote(service_id),
                json_quote(start_iso),
                json_quote(end_iso),
                enum_list,
                int(sample_rate_seconds),
                env_part,
            )
        )
        data = self._post(query, {})
        results = data.get("metrics") or []
        series = {}
        for index, result in enumerate(results):
            if index >= len(measurements):
                break
            points = [
                (item["ts"], item["value"])
                for item in (result.get("values") or [])
                if isinstance(item, dict) and "ts" in item and "value" in item
            ]
            series[measurements[index]] = points
        return series


def json_quote(value):
    import json

    return json.dumps(value)
