# ADR-0001: Phase 1 Modular Monolith

## Status

Accepted

## Context

Phase 1 contains related patient identity, authorization, archive, processing,
and audit workflows with strong transactional consistency. Future doctor, AI,
hospital, and analytics systems must remain independently deployable services.
Current scope gives no evidence that Phase 1 domains require independent scale
or separate deployment teams.

## Decision drivers

- Preserve transactional patient and guardian workflows.
- Keep domain boundaries explicit.
- Minimize operations during initial delivery.
- Maintain versioned interfaces for future external consumers.
- Avoid coupling future systems into patient archive internals.

## Considered options

1. Flat Django application: simplest startup, weak domain boundaries.
2. Modular Django monolith: clear app boundaries with local transactions.
3. Phase 1 microservices: deployment isolation with distributed-system cost.

## Decision

Use modular Django monolith for Phase 1. Each domain owns a Django app and
communicates through explicit service contracts. Expose external behavior only
through `/api/v1/` REST contracts. Keep future doctor, AI, hospital, and
analytics capabilities outside this repository.

## Consequences

Positive:

- Simple local deployment and transactions
- Clear ownership boundaries
- Lower initial operational cost
- Straightforward extraction path if scaling evidence appears

Negative:

- One deployable unit for Phase 1 domains
- Boundaries need code-review discipline
- Shared database can invite accidental cross-domain coupling

Mitigations:

- Keep business rules in domain services.
- Avoid importing private internals across apps.
- Keep API and service contracts documented and tested.
- Add a superseding ADR before extracting a service.

## Revisit triggers

- A domain needs materially different scaling or availability.
- Separate teams need independent release cadence.
- Regulatory isolation requires distinct deployment/data boundaries.
- Monolith transactions or deployments become measured bottlenecks.
