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

import logging
import os

import requests

logger = logging.getLogger(__name__)

RAILWAY_GRAPHQL_URL = os.getenv(
    "RAILWAY_GRAPHQL_URL", "https://backboard.railway.com/graphql/v2"
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
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return os.getenv("RAILWAY_METRICS_ENABLED", "").strip().lower() == "true"

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token_type == "project":
            headers["Project-Access-Token"] = self.token
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _post(self, query, variables):
        if not self.token:
            raise RailwayMetricsError("CONFIG_ERROR", "RAILWAY_METRICS_TOKEN is not set")
        try:
            resp = self._session.post(
                RAILWAY_GRAPHQL_URL,
                headers=self._headers(),
                json={"query": query, "variables": variables},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise RailwayMetricsError("UPSTREAM_ERROR", str(exc)) from exc
        if resp.status_code == 401:
            raise RailwayMetricsError("CONFIG_ERROR", "unauthorized (401)")
        if resp.status_code == 429:
            raise RailwayMetricsError("RATE_LIMITED", "rate limited (429)")
        if resp.status_code >= 500:
            raise RailwayMetricsError(
                "UPSTREAM_ERROR", f"upstream error ({resp.status_code})"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RailwayMetricsError("UPSTREAM_ERROR", "invalid JSON response") from exc
        errors = payload.get("errors")
        if errors:
            raise RailwayMetricsError("CONFIG_ERROR", errors[0].get("message", "graphql error"))
        return payload.get("data") or {}

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
        """Fetch metrics for all services in a SINGLE GraphQL request.

        Returns {service_name: {measurement: [(ts_epoch, value), ...], ...}}.
        Batching with aliases keeps upstream request volume low
        (one HTTP call per collection cycle regardless of service count).
        """
        if not services:
            return {}
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
        quoted = ",".join('"%s"' % m for m in measurements)
        env_part = ""
        if self.environment_id:
            env_part = ',environmentId:"%s"' % self.environment_id
        aliases = []
        for index, svc in enumerate(services):
            aliases.append(
                "s%d: metrics(serviceId:%s,startDate:%s,endDate:%s,"
                "measurements:[%s],sampleRateSeconds:%d%s){ values { ts value } }"
                % (
                    index,
                    json_quote(svc["id"]),
                    json_quote(start_iso),
                    json_quote(end_iso),
                    quoted,
                    int(sample_rate_seconds),
                    env_part,
                )
            )
        query = "query{ %s }" % " ".join(aliases)
        data = self._post(query, {})
        out = {}
        for index, svc in enumerate(services):
            name = svc.get("name") or ("service-%d" % index)
            results = data.get("s%d" % index) or []
            series = {}
            for j, result in enumerate(results):
                if j >= len(measurements):
                    break
                points = [
                    (item["ts"], item["value"])
                    for item in (result.get("values") or [])
                    if isinstance(item, dict) and "ts" in item and "value" in item
                ]
                series[measurements[j]] = points
            out[name] = series
        return out


def json_quote(value):
    import json

    return json.dumps(value)
