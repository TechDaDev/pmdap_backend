# M4 Minors and Guardian Relationships Design

## Scope

M4 adds independent minor PatientProfiles, guardian relationships, relationship
evidence and review, family-number signals, live age-based authorization,
guardian access to permitted identity actions, idempotent creation, and
PostgreSQL concurrency tests. It excludes adult claiming, medical documents,
OCR, archive, facilities, doctors, and AI.

## Minor creation contract

`POST /api/v1/minors/` requires multipart content and `Idempotency-Key` header.
Request combines PatientProfile fields, relationship type, one primary
IdentityDocument payload, and optional relationship evidence:

- Minor: `full_name`, `date_of_birth`, `sex`, `nationality`, optional
  `blood_group`.
- Relationship: `FATHER`, `MOTHER`, or `LEGAL_GUARDIAN`.
- Primary identity: `UNIFIED_NATIONAL_CARD` or `BIRTH_DOCUMENT`; M3 document
  fields plus images retain their existing validation.
- Legal evidence: controlled `evidence_type` plus JPEG/PNG `evidence_file`.
  Required for LEGAL_GUARDIAN and optional supporting evidence for parents.

Service order: lock/revalidate guardian, reserve idempotency key, validate live
minor age, create user-null PatientProfile through M2 service, submit identity
through M3 service, create relationship/evidence/events, and bind replay result.
Any failure rolls back database state and removes newly stored blobs.

Same guardian/key/request returns existing result with HTTP 200. Initial create
returns 201. Same guardian/key with different canonical fields or image hashes
returns 409 `idempotency_conflict`.

## Identity rules

Minor profile becomes VERIFIED through current verified Unified National Card
or current verified Birth Document. Passport remains secondary. Identity review
uses M3 agent endpoints. Guardian relationship approval additionally requires
one verified current primary minor document.

Birth Documents require document number, issuing country, and front image. They
do not require family number, expiry date, National Card numbers, or back image.
`OTHER_GOVERNMENT_ID` and Passport cannot serve as creation primary evidence.

## Guardian APIs

- `POST /api/v1/minors/`: create minor plus pending relationship.
- `GET /api/v1/minors/`: list own pending or currently authorized relationships;
  rejected/inactive/adult-boundary records excluded.
- `GET /api/v1/minors/{uuid}/`: full profile, identity summaries, and own
  relationship status only for verified active live guardian.

M3 identity detail/image/replace routes accept verified active live guardians
for linked minor documents. Collection routes remain adult-owner-only. Failures
return 404 when revealing resource existence would create IDOR signal.

## Verification APIs

- `GET /api/v1/verification/guardian-relationships/`
- `GET /api/v1/verification/guardian-relationships/{uuid}/`
- `POST /api/v1/verification/guardian-relationships/{uuid}/approve/`
- `POST /api/v1/verification/guardian-relationships/{uuid}/reject/`
- `GET /api/v1/verification/guardian-relationships/{uuid}/evidence/{uuid}/file/`

Exact IDENTITY_VERIFICATION_AGENT role required. Queue supports only controlled
status filtering. Detail exposes guardian/minor identity evidence needed for
review but omits guardian email/phone, storage fields, hashes, and unrelated
relationships.

## Relationship decisions

Approval locks relationship, guardian account/profile, minor profile, and
relevant evidence. It rechecks guardian eligibility, live minor age, verified
minor primary identity, and legal evidence. Success records VERIFIED, active,
reviewer, timestamp, and immutable event.

Rejection records REJECTED, inactive, reviewer, timestamp, bounded reason, and
event. Minor identity survives. Same-agent repeated approval is idempotent;
conflicting or repeated rejection is 409.

## Age-18 rule

Every management authorization calls existing PatientProfile `is_minor` logic
against current date. Exact 18th birthday removes access immediately. No state,
Digital ID, document, or relationship history is changed by authorization.

## PostgreSQL acceptance lane

`config.settings.postgres_test` plus `tests/test_postgresql_concurrency.py` run
against Compose PostgreSQL. Tests use separate thread connections and assert:

1. Competing M3 replacement approvals yield one verified-current card.
2. Competing relationship approvals yield one consistent verified relationship
   and one verification event.
3. Concurrent identical idempotency-key minor creation yields one minor,
   document, relationship, and creation request.

SQLite remains fast default; PostgreSQL tests skip unless vendor is PostgreSQL.
