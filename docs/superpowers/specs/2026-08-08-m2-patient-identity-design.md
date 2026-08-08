# M2 Patient Identity and Digital ID Design

## Status

Approved by explicit M2 acceptance dated 2026-08-08. ADR 0002 records creation,
ownership, compatibility, identifier, enum, and DOB decisions.

## Scope

M2 adds PatientProfile, Digital ID generation, transactional PATIENT
registration integration, legacy profile completion, and own-profile read/update
APIs. It adds identity-status foundation only. No identity documents, minors,
guardians, claims, medical documents, uploads, OCR, archive, or facilities.

## API contract

- Registration request adds required nested `patient`: `full_name`,
  `date_of_birth`, `sex`, `nationality`, and `blood_group`.
- `POST /api/v1/patients/me/`: complete profile once for an existing M1 PATIENT
  user only.
- `GET /api/v1/patients/me/`: retrieve directly owned profile.
- `PATCH /api/v1/patients/me/`: partial self-service update.
- PUT and other unsupported methods return 405.

Success/error envelopes retain M1 conventions. Public PatientProfile output:
`uuid`, `digital_id`, `full_name`, `date_of_birth`, `age`, `is_minor`, `sex`,
`nationality`, `blood_group`, `identity_status`, `created_at`, `updated_at`.
User relation and internal state are not serialized.

## Security and invariants

- Ownership derives only from authenticated user; APIs accept no patient key.
- User, UUID, Digital ID, identity status, timestamps, age, and is_minor are
  rejected as client input.
- Nested/unexpected input is rejected.
- Digital ID uses secure randomness, unique DB constraint, and bounded collision
  retries. It is immutable and contains no source identity data.
- User/profile creation shares one database transaction.
- Non-PATIENT users cannot use profile completion or patient self APIs.
- Future DOB is rejected at API and model-validation boundaries.
