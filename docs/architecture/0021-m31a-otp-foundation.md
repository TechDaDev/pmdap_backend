# ADR-0021: PostgreSQL-authoritative OTP foundation

## Status

Accepted for M31A foundation.

## Context

Future email verification and password flows need one reusable OTP domain. Codes
must be one-time, purpose-bound, target-bound, and independent of Redis and
Celery result storage. M31A must not expose a client-selected generic OTP API.

## Decision

- Keep target state, challenges, authorization artifacts, and rate-limit buckets
  in PostgreSQL.
- Store only keyed target hashes, salted and peppered code hashes, and hashed
  authorization tokens. Deliver plaintext codes only through `OtpDeliveryService`.
- Serialize issuance by target, purpose, and channel with a locked target-state
  row. Enforce one unfinished challenge with a partial unique constraint.
- Consume challenge during verification. Return a separate five-minute,
  purpose-specific authorization token for later application flows.
- Enforce resend cooldown plus target, account, and request-source issuance
  limits in PostgreSQL. Redis may add API throttling later but cannot authorize
  or recover OTP state.
- Keep M31A internal. Purpose-specific endpoints belong to later milestones.

## Trade-offs

- PostgreSQL writes cost more than cache-only throttling, but preserve security
  state across Redis and Celery incidents.
- Keyed target hashes prevent direct email lookup in OTP tables, but rotating
  `SECRET_KEY` invalidates outstanding OTPs. Ten-minute challenge lifetime makes
  this acceptable.
- Synchronous SMTP can add request latency. It avoids premature queue coupling;
  async delivery can be added after durable challenge creation.

## Consequences

- No account-existence signal is exposed by M31A because no public OTP endpoint
  exists.
- Future endpoints must choose purpose server-side and pass trusted account and
  request-source context into service calls.
- Real SMTP acceptance remains separate from locmem-backed domain acceptance.
