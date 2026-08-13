# 0016 - Operations Console (admin workstation + Railway monitor)

- **Status:** Accepted (implemented)
- **Date:** 2025
- **Related:** M3 identity verification, M6 private file storage, M14 audit

## Context

PMDAP needs an internal staff console to (a) verify pending identity documents
through a focused workflow and (b) monitor Railway infrastructure health.
Both must live inside Django Admin (staff-only) and never leak patient PII or
the Railway access token to the browser or logs.

## Decisions

### 1. New `opsconsole` app with a replaced `AdminSite`

A dedicated app (`opsconsole`) hosts the workstation and monitor. Its `admin.py`
defines `OpsAdminSite(AdminSite)` which replaces the default Django admin site
at import time (both `django.contrib.admin.sites.site` and the `admin` package
attribute). It is listed **first** in `PROJECT_APPS` so every other app's
`@admin.register` targets the replacement site during autodiscovery.

Custom routes are added in `OpsAdminSite.get_urls()`:

| Path | View | Purpose |
| --- | --- | --- |
| `/admin/identity-verification/` | queue | PENDING + CURRENT, oldest first, no numbers |
| `/admin/identity-verification/<uuid>/` | review | single-document sensitive view + images |
| `/admin/identity-verification/<uuid>/approve/` | approve | GET confirm / POST mutate |
| `/admin/identity-verification/<uuid>/reject/` | reject | GET form / POST mutate |
| `/admin/operations/identity/<uuid>/image/front\|back/` | image | private no-store stream |
| `/admin/operations/server-monitor/` | monitor | Chart.js dashboard |
| `/admin/operations/server-monitor/data/` | data | JSON from Redis only |

`OpsAdminSite.index()` renders `admin/ops/index.html` which adds the Operations
cards (queue count, monitor link) above the standard app list.

### 2. Verification workstation authorization

- **View** (queue / review / images): `is_staff` (admin site) AND (superuser OR
  `role == IDENTITY_VERIFICATION_AGENT`). Superusers may view for oversight.
- **Mutate** (approve / reject): additionally requires
  `role == IDENTITY_VERIFICATION_AGENT`, mirroring enforcement inside
  `identities.services.approve_identity_document` / `reject_identity_document`
  (defense in depth). There is **no bulk approve** and no direct
  `verification_status` mutation from admin.
- Mutations are POST-only (`admin_view` adds CSRF + never-cache); GET renders a
  confirmation page / rejection form with a mandatory reason.
- Queue page never renders document/national/family numbers; sensitive fields
  appear only on the single-document review page.
- Identity images stream through authenticated endpoints with
  `Cache-Control: private, no-store` and `X-Robots-Tag: noindex`.

### 3. Railway server monitor

**Collector** (`opsconsole/collector.py`, Celery task `ops.railway.collect_metrics`
on the default queue) is **self-rescheduling** (`apply_async(countdown=interval)`)
so no `celery beat` process is required. A Redis `SETNX` chain guard
(`pmdap:ops:railway:chain`) ensures exactly one chain exists even with multiple
web/worker processes.

**Upstream client** (`opsconsole/railway_client.py`) talks to
`https://backboard.railway.com/graphql/v2` with `Bearer` (account or API token)
or `Project-Access-Token` auth, verified by live introspection:

- `Query.project(id: String!)` -> `services { edges { node { id name } } }`
- `Query.metrics(serviceId, startDate, endDate, measurements, sampleRateSeconds)`
  -> one `MetricsResult` per measurement, in request order, with
  `values { ts (epoch s) value (Float) }`.
- Services are fetched **sequentially** (one HTTP call each): Railway enforces
  max 19 concurrent metric queries per client and counts every measurement in a
  call as one query, so a batched alias query (4 services x 5 measurements = 20)
  exceeded the limit. Sequential fetches stay at 5 concurrent.
- `sampleRateSeconds` must be >= 30 (lower values return "Invalid input").
- `MetricMeasurement` enum values are inlined as bare identifiers (not quoted
  strings) in query text.
- Units: CPU in vCPU, memory/disk in GB, network in cumulative GB (rate derived
  client-side).
- The API is behind Cloudflare: a browser `User-Agent` is required (the urllib
  default UA is blocked with HTTP 403 error 1010).
- The account token from `~/.railway/config.json` is IP-allowlisted (rejected
  from Railway egress); use a workspace API token created via the
  `apiTokenCreate` GraphQL mutation instead.

**Buffer** (`opsconsole/buffer.py`) keeps a capped Redis rolling list per
service x metric (`pmdap:ops:railway:metrics:<svc>:<metric>`), retention
default 30 min, plus a collector-status hash. The **data endpoint** reads only
Redis and returns JSON with `Cache-Control: no-store`; it never calls Railway
and never includes the token.

**Failure handling**: 429 -> exponential backoff (stored in status, no
Retry-After exists); 401/GraphQL auth error -> `CONFIG_ERROR` (stops
hammering); 5xx -> `UPSTREAM_ERROR` (stale buffer retained); Redis outage ->
`REDIS_UNAVAILABLE` (503). Collector failure never crashes the task.

**Dashboard** (`admin/ops/server_monitor.html` + vendored Chart.js 4.4.7,
no CDN) polls the data endpoint every 3s while the tab is visible, uses
`AbortController` + `visibilitychange` to stop polling when hidden, renders
CPU/Memory/Disk/Network charts with numeric current + peak values (never
colour-only) and a stale indicator.

### 4. Security invariants

- The Railway token is server-side only (`RAILWAY_METRICS_TOKEN` env) and never
  reaches the browser or logs.
- Monitor access: staff AND (superuser OR `opsconsole.view_server_monitor`
  permission). The permission is registered via a `managed=False` model.
- No PII in logs; synthetic fixtures only in tests.

## Environment variables

```
RAILWAY_METRICS_ENABLED=true
RAILWAY_METRICS_TOKEN=<workspace API or project token>
RAILWAY_METRICS_TOKEN_TYPE=bearer|project
RAILWAY_METRICS_PROJECT_ID=8610197a-...
RAILWAY_METRICS_ENVIRONMENT_ID=ad64bcc8-...
RAILWAY_METRICS_SAMPLE_SECONDS=30
RAILWAY_METRICS_RETENTION_SECONDS=1800
```

## Consequences

- All existing admin registrations continue to work; the replacement site is a
  plain `AdminSite` subclass.
- One upstream GraphQL call per sample cycle (plus a cached 5-min service
  discovery) keeps well under Railway rate limits.
- Without `RAILWAY_METRICS_ENABLED=true` the monitor renders a "not configured"
  notice and the collector does nothing.

## Tests

- `tests/test_ops_verification_admin.py` — authz matrix, queue contents,
  number-free queue, image streaming, approve/reject through domain services,
  replacement, no-mutation-on-GET, superuser-cannot-mutate.
- `tests/test_ops_server_monitor.py` — collector OK/backoff/config/early-return/
  chain-guard, buffer semantics, data endpoint authz/disabled/OK/no-token/503,
  all with mocked Railway + Redis.
