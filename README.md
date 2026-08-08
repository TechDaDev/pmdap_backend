# Patient Medical Document Archiving Platform Backend

Secure, patient-centered medical document archive backend. Phase 1 uses Django,
Django REST Framework, PostgreSQL, Redis, Celery, and versioned REST APIs.

## Delivery status

M0-M5 are accepted. M6 private medical file storage and document uploads are
implemented locally and await project-owner acceptance. M7 and later phases
remain blocked.

## Authoritative documents

- [Phase 1 scope](docs/specification/phase-1-scope.md)
- [M0 foundation design](docs/superpowers/specs/2026-08-08-m0-project-foundation-design.md)
- [M3 identity-document design](docs/superpowers/specs/2026-08-08-m3-identity-documents-design.md)
- [M4 minor/guardian design](docs/superpowers/specs/2026-08-08-m4-minors-guardians-design.md)
- [M5 adult account-claiming design](docs/superpowers/specs/2026-08-08-m5-adult-account-claiming-design.md)
- [M6 medical file storage design](docs/superpowers/specs/2026-08-09-m6-medical-file-storage-design.md)
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

M4-M6 marked concurrency suites must run against PostgreSQL, not SQLite:

```bash
DJANGO_SETTINGS_MODULE=config.settings.postgres_test \
POSTGRES_DB=pmdap POSTGRES_USER=pmdap POSTGRES_PASSWORD=... \
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 \
.venv/bin/pytest tests/test_postgresql_concurrency.py \
  tests/test_account_claim_concurrency.py \
  tests/test_medical_document_concurrency.py -m postgresql
```

## M5 account claiming

Public submission and activation expose only bounded receipt/credential
contracts:

- `POST /api/v1/account-claims/`
- `POST /api/v1/auth/activate-claimed-account/`

Exact-role identity verification agents use the protected queue, detail,
evidence, approve, reject, and request-more-information endpoints under
`/api/v1/verification/account-claims/`. Public claim status polling and anonymous
claim editing are intentionally omitted. Approval links a new
`PENDING_ACTIVATION` PATIENT account to the same lifelong PatientProfile;
activation tokens are random, short-lived, single-use, and stored only as
SHA-256 hashes. Claim evidence remains private evidence and never silently
replaces verified M3 identity truth.

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
- Medical originals: separate private persistent volume; authorized attachment
  streaming only

## M6 medical document storage

Adult patients manage their own active documents under `/api/v1/documents/`.
Verified guardians use the explicitly nested
`/api/v1/minors/{minor_uuid}/documents/` routes; the live M4 relationship and
minor age are rechecked for every operation. PDF, JPEG, and PNG are the only
accepted formats. Original bytes are retained unchanged, never exposed by a
public media URL, and soft deletion retains the blob for later retention-policy
work.

`documents.storage.PrivateMedicalStorage` is the replaceable Django storage
boundary. A future private S3-compatible backend can replace that configured
storage object while preserving randomized keys and application-authorized
streaming; no S3 integration is included in M6. Malware scanning is likewise a
declared interface but truthfully records `NOT_CONFIGURED` until a real scanner
and quarantine workflow are introduced.

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
