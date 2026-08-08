# Phase 1 Backend Scope

## Goal

Build secure Django REST backend where adult patients maintain their own medical
archive, guardians manage linked minors, every patient keeps permanent Digital
ID, and original medical files remain intact through deterministic text, OCR,
date-detection, confirmation, archive, and search workflows.

Phase 1 is archival only.

## Explicit exclusions

- Doctor accounts or access
- AI/LLM interpretation
- Diagnosis or treatment extraction/recommendations
- Clinical decision support
- Appointments, billing, insurance, telemedicine
- Hospital-management or analytics dashboards

## Technology contract

- Python 3.12+
- Django and Django REST Framework
- PostgreSQL
- SimpleJWT with refresh rotation/blacklisting in M1
- Celery and Redis
- PyMuPDF in M7
- PaddleOCR behind an engine interface in M8
- OpenCV/Pillow when processing needs them
- S3-compatible private storage abstraction
- pytest, pytest-django, pytest-cov, factory_boy, Faker
- Docker and Docker Compose
- drf-spectacular OpenAPI/Swagger
- Environment-variable configuration

Dependencies enter in their owning phase. Heavy OCR/runtime packages must not
be required for M0 startup or default unit tests.

## Architecture invariants

1. Patient identity, login identity, and authorization are separate concepts.
2. A patient keeps one `PatientProfile`, Digital ID, and archive for life.
3. Age is calculated from date of birth; it is never stored.
4. Public resource identifiers are immutable UUIDs. Digital ID contains no
   personal data and is not authentication proof.
5. Guardian access is explicit, verified, historical, and expires at adulthood.
6. Adulthood claims link an account to existing patient record transactionally.
7. Original medical files are private and never changed during processing.
8. Identity history is append-only; replacements create new records.
9. File duplication uses SHA-256 content, not filename.
10. OCR/date failures never delete uploads; manual metadata remains possible.
11. Date detection is deterministic and multilingual; no LLM is allowed.
12. Archive views derive from metadata, never duplicated folder trees.
13. Knowing UUID or Digital ID never grants access.
14. Audit records are immutable through normal APIs.
15. Critical multi-model workflows are transactional.

## Django boundaries

- `accounts`: custom user, authentication, roles, status
- `patients`: lifelong patient profile and Digital ID
- `identities`: government identity records and history
- `guardians`: minor relationships and permissions
- `claims`: adult account-claim lifecycle
- `documents`: uploads, stored files, metadata, duplicates
- `processing`: extraction, OCR, dates, state transitions
- `archive`: chronological views, filters, search behavior
- `facilities`: normalized healthcare locations
- `audit`: immutable events
- `common`: UUID models, errors, permissions, constants

These apps form one modular monolith in Phase 1. Future doctor, AI, hospital, and
analytics systems remain separate services consuming versioned APIs.

## API contract rules

- Every endpoint starts with `/api/v1/`.
- REST resources use stable noun-based paths and correct HTTP semantics.
- OpenAPI documents request, response, auth, and error schemas.
- Errors use one envelope:

```json
{
  "error": {
    "code": "MACHINE_READABLE_CODE",
    "message": "Safe human-readable message.",
    "details": {}
  }
}
```

- List endpoints use consistent pagination.
- Sensitive endpoints require authentication plus object/function-level
  authorization.
- Stack traces, secrets, storage paths, and private URLs never enter responses.

## Security baseline

- HTTPS-ready deployment settings and trusted proxy handling
- Environment-managed secrets with fail-closed production configuration
- Strong password validation and hashing
- JWT rotation/revocation in M1
- Object-level patient and guardian authorization
- Private storage and authorized downloads
- File content, size, integrity, and path validation
- API throttling, restrictive CORS, safe headers, sanitized errors
- No secrets or medical/identity contents in logs
- OWASP API authorization, authentication, resource-consumption, and
  misconfiguration risks covered by tests

## Phase gates

- M0: foundation and infrastructure
- M1: authentication
- M2: patient identity and Digital ID
- M3: identity documents
- M4: minors and guardians
- M5: adult account claim
- M6: stored files and medical uploads
- M7: PDF extraction
- M8: OCR subsystem
- M9: multilingual date engine
- M10: date verification
- M11: facilities and classification
- M12: archive
- M13: search and filters
- M14: audit and integrity hardening
- M15: full workflow and security testing
- M16: optional OCR benchmark harness
- M17: API stabilization

Each phase follows contract, failing tests, minimal implementation, targeted
tests, full suite, coverage, OpenAPI update, clean review, then report. Passing
tests and explicit owner approval are required before next phase.
