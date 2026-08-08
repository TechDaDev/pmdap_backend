# M0 Project Foundation Design

## Status

Approved for implementation from supplied project build specification and
explicit instruction dated 2026-08-08.

## Objective

Create smallest production-shaped Django foundation supporting later Phase 1
work without implementing authentication or medical archive features.

## Considered approaches

### A. Flat single-settings Django scaffold

Low initial file count. Environment differences become conditional branches in
one module, increasing secret and deployment drift risk.

### B. Modular monolith with split settings and phase-scoped dependencies

Selected. Keeps domain boundaries visible, test settings deterministic, and
heavy OCR dependencies deferred to their phases. Costs more initial scaffolding.

### C. Separate service per future domain

Strong deployment isolation. Rejected for Phase 1 because distributed
transactions, operations, and API coordination add complexity without current
independent scaling needs.

## Structure

```text
config/
  settings/{base,local,test,production}.py
  urls.py
  asgi.py
  wsgi.py
  celery.py
accounts/
patients/
identities/
guardians/
claims/
documents/
processing/
archive/
facilities/
audit/
common/
tests/
```

Apps contain standard Django configuration and migrations packages. M0 adds
only custom User foundation under `accounts`, abstract UUID/timestamp base under
`common`, and health API behavior. Other apps remain intentional boundaries,
not placeholder domain models.

## Runtime architecture

- Django API uses PostgreSQL.
- Celery worker uses Redis broker/result backend.
- Docker Compose starts API, worker, PostgreSQL, and Redis.
- API container runs migrations before development server startup.
- Local host execution may use explicit test settings; normal runtime stays on
  PostgreSQL.
- Configuration loads from environment variables with documented safe local
  defaults. Production secrets have no unsafe defaults.

## M0 API contract

`GET /api/v1/health/`

- Authentication: none
- Success: HTTP 200
- Body: `{"status": "ok"}`
- Purpose: process liveness, not disclosure of dependency or system details

OpenAPI endpoints expose schema and Swagger UI under `/api/v1/schema/` and
`/api/v1/docs/`. No M1 auth endpoints exist.

## Data foundation

Custom `accounts.User` exists from first migration and is configured through
`AUTH_USER_MODEL`. It supplies UUID public identity plus email-oriented login
foundation and required account fields, without exposing M1 API behavior.

`common.UUIDModel` is abstract and supplies immutable UUID primary key plus
created/updated timestamps for future models. Raw integer resource IDs are not
introduced.

## Failure and security behavior

- Missing required production settings stop startup.
- Secrets remain outside version control; `.env.example` contains placeholders.
- Django production security flags support HTTPS and trusted reverse proxies.
- Debug is disabled outside local/test settings.
- Health response reveals no database, Redis, hostname, versions, or exceptions.
- OpenAPI describes only implemented routes.

## Test strategy

Test-first behavior:

1. Health endpoint contract fails before route/view exists.
2. User/UUID model foundation tests fail before models exist.
3. Settings/routing checks fail before configuration exists.

Verification after implementation:

- focused pytest cycles
- full pytest suite with coverage threshold at least 90%
- `manage.py check`
- migration drift check
- Docker Compose configuration validation
- container build and startup
- PostgreSQL migration proof
- Redis ping and Celery worker connectivity proof
- live health and OpenAPI requests

## M0 acceptance criteria

- Django application starts.
- All eleven Django app boundaries exist.
- Custom User and UUID base foundations exist with migrations where applicable.
- API v1, health, schema, and Swagger routes work.
- PostgreSQL migrates successfully.
- Redis and Celery connect successfully.
- Docker Compose supports local development.
- pytest, factories, lint/format, and coverage configuration work.
- Full tests pass with at least 90% meaningful coverage.
- No M1 behavior or excluded Phase 1 features exist.

## Known boundaries

- Health is liveness only; readiness/observability expansion needs later scope.
- M0 does not implement registration, login, JWT endpoints, patient models,
  uploads, OCR, or archive behavior.
- S3 integration begins with medical/identity file ownership phases, not M0.
