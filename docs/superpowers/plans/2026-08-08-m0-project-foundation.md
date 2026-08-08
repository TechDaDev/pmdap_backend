# M0 Project Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build tested Django M0 foundation with PostgreSQL, Redis, Celery, Docker Compose, custom UUID user base, versioned API, OpenAPI, and health endpoint.

**Architecture:** Use one modular Django monolith with eleven domain apps and split settings. PostgreSQL is normal runtime database, Redis backs Celery, and phase-scoped dependencies keep OCR outside M0. REST contracts live under `/api/v1/`; health is public liveness while future APIs deny access by default.

**Tech Stack:** Python 3.12, Django 5.2 LTS, Django REST Framework, PostgreSQL 17, Redis 7, Celery 5.6, drf-spectacular, pytest, factory_boy, Ruff, Docker Compose.

## Global Constraints

- Implement M0 only; no registration, login, JWT endpoints, patient models, uploads, OCR, or archive behavior.
- Use UUID primary keys for public resources; never expose integer resource IDs.
- Keep secrets in environment variables; `.env.example` contains placeholders only.
- Default API permission is authenticated; health explicitly opts into public access.
- Overall measured coverage must be at least 90%.
- All eleven domain app boundaries must exist without placeholder domain models.

---

### Task 1: Dependency and Tooling Foundation

**Files:**
- Create: `requirements/base.txt`
- Create: `requirements/dev.txt`
- Create: `pyproject.toml`
- Create: `pytest.ini`
- Create: `.coveragerc`
- Create: `.env.example`

**Interfaces:**
- Consumes: Python 3.12 and pip.
- Produces: reproducible M0 runtime/dev environments and commands `pytest`, `coverage`, `ruff`.

- [ ] **Step 1: Pin M0 runtime dependencies**

Use Django 5.2 LTS, DRF, drf-spectacular, Celery, Redis client, psycopg binary,
and Gunicorn only. Do not add SimpleJWT, PyMuPDF, PaddleOCR, OpenCV, or S3 SDK.

- [ ] **Step 2: Pin test and quality dependencies**

Add pytest, pytest-django, pytest-cov, factory_boy, Faker, and Ruff.

- [ ] **Step 3: Configure test and quality commands**

Set `DJANGO_SETTINGS_MODULE=config.settings.test`, strict pytest markers,
`--cov-fail-under=90`, branch coverage, Python 3.12 Ruff target, 88-column lines.

- [ ] **Step 4: Install dependencies**

Run: `python3 -m venv .venv && .venv/bin/pip install -r requirements/dev.txt`

Expected: exit 0; Django and pytest import successfully.

- [ ] **Step 5: Commit**

```bash
git add requirements pyproject.toml pytest.ini .coveragerc .env.example
git commit -m "build: add M0 Python toolchain"
```

### Task 2: Write Foundation Contract Tests

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_app_structure.py`
- Create: `tests/test_models.py`
- Create: `tests/test_health.py`
- Create: `tests/test_openapi.py`
- Create: `tests/factories.py`

**Interfaces:**
- Consumes: Django test client and app registry.
- Produces: executable M0 contracts for app boundaries, UUID models, health, OpenAPI, and factory setup.

- [ ] **Step 1: Write app-boundary tests**

Assert all names in this exact tuple are installed:

```python
PROJECT_APPS = (
    "accounts", "patients", "identities", "guardians", "claims",
    "documents", "processing", "archive", "facilities", "audit", "common",
)
```

- [ ] **Step 2: Write model foundation tests**

Assert `accounts.User` primary key is named `uuid`, generated as UUID, email is
unique, username is absent, and `common.models.UUIDModel` is abstract with UUID
primary key plus `created_at` and `updated_at`.

- [ ] **Step 3: Write health and OpenAPI contract tests**

```python
def test_health_returns_public_liveness(api_client):
    response = api_client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

Assert schema endpoint returns 200 and generated schema contains
`/api/v1/health/`.

- [ ] **Step 4: Write factory foundation**

Define `UserFactory` with `DjangoModelFactory`, unique Faker email, and password
set through `set_password`.

- [ ] **Step 5: Verify RED**

Run: `.venv/bin/pytest tests -q`

Expected: collection fails because `config`, project apps, and models do not yet
exist. This is expected missing-foundation failure.

- [ ] **Step 6: Commit failing contracts**

```bash
git add tests
git commit -m "test: define M0 foundation contracts"
```

### Task 3: Django Project, Settings, Apps, and Models

**Files:**
- Create: `manage.py`
- Create: `config/{__init__,urls,asgi,wsgi,celery}.py`
- Create: `config/settings/{__init__,base,local,test,production}.py`
- Create: each app's `__init__.py`, `apps.py`, `migrations/__init__.py`
- Create: `accounts/models.py`, `accounts/managers.py`, `accounts/admin.py`
- Create: `accounts/migrations/0001_initial.py`
- Create: `common/models.py`

**Interfaces:**
- Consumes: environment variables documented in `.env.example`.
- Produces: `config.settings.*`, `accounts.User`, `common.UUIDModel`, Celery app.

- [ ] **Step 1: Create settings split**

Base settings configure project apps, PostgreSQL environment fields,
`AUTH_USER_MODEL = "accounts.User"`, DRF authenticated default permissions,
drf-spectacular schema class, Celery Redis URLs, UTC, and static/media paths.
Local settings enable debug. Test settings use in-memory SQLite and fast password
hasher. Production settings require non-placeholder secret and allowed hosts,
enable secure cookies, HSTS, SSL redirect, and proxy HTTPS header.

- [ ] **Step 2: Create app boundaries**

Each app exposes one `AppConfig`; only `accounts` and `common` receive M0 model
code. No future-domain placeholders or endpoints.

- [ ] **Step 3: Implement UUID foundations**

`UUIDModel` is abstract with `uuid = UUIDField(primary_key=True,
default=uuid.uuid4, editable=False)`, `created_at`, and `updated_at`.

`User` subclasses `AbstractUser`, removes username, uses UUID primary key and
unique email login, and adds nullable phone plus role/status/verification fields
defined in supplied Phase 1 contract. Custom manager implements
`create_user(email, password, **extra_fields)` and `create_superuser(...)`.

- [ ] **Step 4: Generate and inspect migration**

Run: `.venv/bin/python manage.py makemigrations accounts --settings=config.settings.test`

Expected: one `accounts/migrations/0001_initial.py` creating custom User.

- [ ] **Step 5: Verify GREEN for foundation tests**

Run: `.venv/bin/pytest tests/test_app_structure.py tests/test_models.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Run Django checks**

Run: `.venv/bin/python manage.py check --settings=config.settings.test`

Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 7: Commit**

```bash
git add manage.py config accounts patients identities guardians claims documents processing archive facilities audit common
git commit -m "feat: scaffold M0 Django foundation"
```

### Task 4: Health and OpenAPI APIs

**Files:**
- Create: `common/api.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: DRF `APIView`, drf-spectacular schema views.
- Produces: `HealthView.get()` and exact `/api/v1/health/`, `/api/v1/schema/`, `/api/v1/docs/` routes.

- [ ] **Step 1: Verify endpoint tests remain RED**

Run: `.venv/bin/pytest tests/test_health.py tests/test_openapi.py -q`

Expected: 404 failures for missing routes.

- [ ] **Step 2: Implement liveness endpoint**

Create `HealthView` with `AllowAny`, no authentication classes, documented
`{"status": "ok"}` response, and no dependency details.

- [ ] **Step 3: Add versioned routes**

Route health, schema, and Swagger UI under `/api/v1/`. Keep Django admin outside
API namespace.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_health.py tests/test_openapi.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add common/api.py config/urls.py tests
git commit -m "feat(api): add M0 health and OpenAPI"
```

### Task 5: Docker Runtime

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `docker/entrypoint.sh`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `.env` variables and requirements.
- Produces: `web`, `worker`, `db`, and `redis` services with health/dependency gates.

- [ ] **Step 1: Build Python image**

Use `python:3.12-slim`, non-root runtime user, pinned requirements, unbuffered
Python, and no source-secret copy. Development source is bind-mounted by Compose.

- [ ] **Step 2: Configure services**

Use PostgreSQL 17 and Redis 7 Alpine images with health checks. Web waits for
database, migrates, then starts on `0.0.0.0:8000`. Worker starts
`celery -A config worker`. Do not expose PostgreSQL or Redis host ports by
default.

- [ ] **Step 3: Validate Compose**

Run: `docker compose config --quiet`

Expected: exit 0.

- [ ] **Step 4: Build and start**

Run: `docker compose up -d --build`

Expected: all four services start; db and redis become healthy.

- [ ] **Step 5: Verify runtime**

Run migrations in web container, ping Redis, inspect Celery ping, request health
and schema endpoints, then inspect container status/logs for startup errors.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml docker .dockerignore
git commit -m "build: add M0 Docker runtime"
```

### Task 6: Acceptance, Review, and Reporting

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-08-m0-project-foundation.md`

**Interfaces:**
- Consumes: complete M0 implementation.
- Produces: reproducible local commands and acceptance evidence.

- [ ] **Step 1: Run static checks**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`

- [ ] **Step 2: Run Django checks and migration drift check**

Run: `.venv/bin/python manage.py check --settings=config.settings.test`

Run: `.venv/bin/python manage.py makemigrations --check --dry-run --settings=config.settings.test`

- [ ] **Step 3: Run full tests and coverage**

Run: `.venv/bin/pytest`

Expected: zero failures and total branch-aware coverage at least 90%.

- [ ] **Step 4: Re-run Docker acceptance fresh**

Run Compose validation, build/start, migration, Redis ping, Celery ping, HTTP
health, OpenAPI schema, then `docker compose down` without deleting volumes.

- [ ] **Step 5: Review diff and scope**

Run `git diff --check`, inspect changed files, scan for secrets/placeholders,
confirm no M1 endpoints/models or excluded features entered repository.

- [ ] **Step 6: Update README with exact commands**

Document environment setup, local tests, Docker startup, health URL, OpenAPI URL,
and M0/M1 phase gate.

- [ ] **Step 7: Commit acceptance docs**

```bash
git add README.md docs/superpowers/plans/2026-08-08-m0-project-foundation.md
git commit -m "docs: add M0 run and verification guide"
```

- [ ] **Step 8: Report**

Use required phase report fields with exact test count, coverage, commands,
security notes, limitations, and `NEXT PHASE: M1 — blocked pending approval`.
