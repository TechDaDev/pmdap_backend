# Patient Medical Document Archiving Platform Backend

Secure, patient-centered medical document archive backend. Phase 1 uses Django,
Django REST Framework, PostgreSQL, Redis, Celery, and versioned REST APIs.

## Delivery status

M0-M3 are accepted. M4 minor identities and guardian relationships are
implemented locally and await project-owner acceptance. M5 and later phases
remain blocked.

## Authoritative documents

- [Phase 1 scope](docs/specification/phase-1-scope.md)
- [M0 foundation design](docs/superpowers/specs/2026-08-08-m0-project-foundation-design.md)
- [M3 identity-document design](docs/superpowers/specs/2026-08-08-m3-identity-documents-design.md)
- [M4 minor/guardian design](docs/superpowers/specs/2026-08-08-m4-minors-guardians-design.md)
- [Architecture decisions](docs/architecture/README.md)
- [Contributor and agent rules](AGENTS.md)

## Scope boundary

Phase 1 archives patient medical documents. It does not provide doctor access,
AI/LLM interpretation, diagnosis extraction, treatment recommendations,
appointments, billing, insurance, hospital management, telemedicine, or
analytics dashboards.

## Local test setup

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements/dev.txt
.venv/bin/pytest
```

Quality and framework checks:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python manage.py check --settings=config.settings.test
.venv/bin/python manage.py makemigrations --check --dry-run \
  --settings=config.settings.test
```

M4's marked concurrency suite must run against PostgreSQL, not SQLite:

```bash
DJANGO_SETTINGS_MODULE=config.settings.postgres_test \
POSTGRES_DB=pmdap POSTGRES_USER=pmdap POSTGRES_PASSWORD=... \
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 \
.venv/bin/pytest tests/test_postgresql_concurrency.py -m postgresql
```

## Docker Compose

Requires Docker with Compose. Create local environment file, then replace every
`replace-with-...` placeholder. Never commit `.env`.

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

Services:

- API: `http://localhost:8000`
- Health: `http://localhost:8000/api/v1/health/`
- OpenAPI: `http://localhost:8000/api/v1/schema/`
- Swagger UI: `http://localhost:8000/api/v1/docs/`
- PostgreSQL and Redis: container-network only; no host ports exposed
- Identity images: private persistent volume; authorized API streaming only

Useful verification:

```bash
docker compose exec web python manage.py migrate --check
docker compose exec redis redis-cli ping
docker compose exec worker celery -A config inspect ping --timeout 5
docker compose logs web worker
```

Stop services without deleting persistent data:

```bash
docker compose down
```
