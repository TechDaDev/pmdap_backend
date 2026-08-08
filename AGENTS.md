# Project Instructions

## Scope

- Implement only the active phase in `docs/specification/phase-1-scope.md`.
- Stop after each phase. Do not begin the next phase without explicit approval.
- Keep patient identity, authentication, and authorization separate.
- Preserve one lifelong `PatientProfile` and one lifelong archive per patient.
- Never add doctor workflows or AI/LLM medical interpretation in Phase 1.

## Delivery

- Define API contracts before implementation.
- Use test-first development for behavior.
- Keep business rules in services, not only serializers.
- Use UUIDs for public resources; never expose raw integer IDs.
- Preserve original uploads and identity-document history.
- Use environment variables for configuration. Never commit secrets.
- Run targeted tests, full tests, coverage, migrations, and deployment checks
  before claiming a phase complete.
- Target overall coverage of at least 90%; prioritize meaningful security and
  authorization coverage.
- Keep commits clean and phase-scoped.

## M0 gate

M0 includes project/app scaffolding, split settings, Docker Compose,
PostgreSQL, Redis, Celery, custom user skeleton, UUID base model, API v1 routing,
OpenAPI, pytest/factories, and tested health endpoint. No M1 authentication
endpoints belong in M0.
