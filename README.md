# Patient Medical Document Archiving Platform Backend

Secure, patient-centered medical document archive backend. Phase 1 uses Django,
Django REST Framework, PostgreSQL, Redis, Celery, and versioned REST APIs.

## Delivery status

Project specification approved. M0 foundation is next. M1 and later phases are
blocked until M0 acceptance passes and the project owner explicitly approves
continuation.

## Authoritative documents

- [Phase 1 scope](docs/specification/phase-1-scope.md)
- [M0 foundation design](docs/superpowers/specs/2026-08-08-m0-project-foundation-design.md)
- [Architecture decisions](docs/architecture/README.md)
- [Contributor and agent rules](AGENTS.md)

## Scope boundary

Phase 1 archives patient medical documents. It does not provide doctor access,
AI/LLM interpretation, diagnosis extraction, treatment recommendations,
appointments, billing, insurance, hospital management, telemedicine, or
analytics dashboards.
