# 0020 — M29.5 Reviewer Identity Correction Workflow

Status: Accepted (2026-08-27)
Deciders: PMDAP backend team
Replaces: n/a

## Context

OCR output is a machine suggestion. Human review is the authoritative
correction/confirmation, and verification approval promotes reviewed values to
authoritative verified identity. Today the Operations Console can only
approve or reject; the reviewer cannot correct structured OCR mistakes before
approval, and there is no controlled path to correct a verified identity.

## Decision

Three explicit authority layers, never conflated:

1. **RAW OCR evidence** — machine-produced extraction. Never persisted
   (privacy design); the authoritative value present before any reviewer
   correction is preserved as the `original_value` in `IdentityFieldCorrection`
   records. Raw OCR text/spans/images are never overwritten.
2. **Reviewed values** — staged on `IdentityDocument` via `reviewed_*`
   columns, with per-field provenance in `IdentityFieldCorrection`. The
   workstation edits only these staged values; the underlying authoritative
   values stay untouched until approval.
3. **Verified authoritative identity** — `PatientProfile` structured fields
   (names, DOB, sex, blood group, nationality) and `IdentityDocument` number
   fields. Approval promotes reviewed values here. Verified correction
   applies reviewed values immediately while keeping the document VERIFIED.

### Field ownership

| Field | Authoritative store | Staging (reviewed) |
|-------|--------------------|--------------------|
| given_name / father_name / grandfather_name / mother_name | PatientProfile | IdentityDocument.reviewed_* |
| date_of_birth / sex / blood_group / nationality | PatientProfile | IdentityDocument.reviewed_* |
| document_number / national_number / family_number / unique_card_body_number | IdentityDocument | IdentityDocument.reviewed_* |

### Family number

M27 remains authoritative. The reviewer MAY correct family_number during
review (compared against the physical verified card); the value is explicit,
validated, provenance-recorded, and once approved it becomes verified
authoritative data. It never comes from a patient/guardian client payload.
Guardian relationships are re-evaluated on approval/correction via
`revalidate_relationships_for_identity` (M27 policy).

### Concurrency

`IdentityDocument.review_version` increments on every reviewed-value save.
Write operations require the client-supplied `review_version` to match the
current value; a mismatch returns 409 (stale review). Approve/reject lock the
document row (`select_for_update`), so a stale editor cannot overwrite a
newer review or regress VERIFIED to PENDING.

### Verified correction

Separate high-risk action. Requires a reason category (non-blank) and note.
Applies reviewed values to authoritative stores, records
`IdentityFieldCorrection(verified_correction=True, reason)`, emits
`IDENTITY_VERIFIED_FIELDS_CORRECTED` event + audit, and re-evaluates guardian
evidence. Policy: the identity REMAINS VERIFIED after a successful correction
(trusted superuser / IDENTITY_VERIFICATION_AGENT), with the correction event
as the audit record. Direct Django admin editing stays read-only.

## Consequences

- Corrected values are never falsely labelled OCR-extracted; provenance
  (`source=REVIEWER_CORRECTION`, reviewer, timestamp) is preserved.
- Rejection never promotes reviewed values to the profile.
- DOB/name/family changes trigger dependent revalidation (age recompute via
  derived `is_minor`, guardian evidence recompute, duplicate number checks).
- M30 Operations APK consumes the same verification API (queue/detail/
  review-fields/approve/reject/correct-verified/images).
