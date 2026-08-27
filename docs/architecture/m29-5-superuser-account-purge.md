# M29.5 Superuser Account Purge Policy

## Decision

Account deletion is a privileged domain operation, not Django generic deletion.
Only an active Django superuser may call `purge_user_account_as_superuser`.
Self-purge and purge of the last active superuser are blocked.

Purge tombstones account and patient profile, hard-deletes authentication
artifacts and private medical records, and removes private file bytes. Immutable
identity, guardian, claim, and audit history stays linked to stable UUIDs while
all mutable PII is scrubbed. This preserves event integrity without retaining
an active authorization path or weakening existing FK protection.

## Existing delete graph

Rules audited before implementation:

- `PatientProfile.user`: `PROTECT`, nullable.
- `IdentityDocument.patient`, `front_image`: `PROTECT`.
- `IdentityDocument.back_image`: nullable `PROTECT`.
- `IdentityDocument.verified_by`, `IdentityDocument.replaces`: nullable
  `PROTECT`.
- `IdentityDocumentEvent.document`: `PROTECT`; event remains immutable.
- `IdentityDocumentEvent.actor`: nullable `PROTECT`; retained against the
  disabled user tombstone.
- `IdentityExtractionJob.user`: `CASCADE`; purge also removes staging bytes and
  cached extraction payload.
- `GuardianRelationship.guardian_user`, `minor_patient`: `PROTECT`.
- Guardian relationship identity and verifier links: nullable `PROTECT`.
- `GuardianEvidence.relationship` and event relationship: `PROTECT`.
- Guardian event actor: nullable `PROTECT`; retained against the disabled user
  tombstone.
- Minor creation request guardian/profile/relationship links: `PROTECT`.
- `PatientAccountClaim.patient`: `PROTECT`.
- Claim reviewer and approved-user links: nullable `PROTECT`.
- Claim evidence, activation, and claim event links: `PROTECT`.
- Claim event actor: nullable `PROTECT`; retained against the disabled user
  tombstone.
- `MedicalDocument.patient`, `stored_file`: `PROTECT`.
- `MedicalDocument.uploaded_by`: `PROTECT`.
- `MedicalDocument.deleted_by`: nullable `PROTECT`.
- Medical document pages: `CASCADE`.
- Medical/date events, OCR text, date candidates, and lab extraction document
  links: `PROTECT`.
- Date-event actor: `PROTECT`.
- `AuditLog.actor` and `AuditLog.patient`: nullable `SET_NULL`.
- Django admin log user: `CASCADE`; JWT outstanding-token user: nullable
  `SET_NULL`.

No `RESTRICT` rule exists in this graph. Existing `CASCADE` is limited to
transient/auth or medical page children. All audited `PROTECT` rules remain.

## Retention and deletion policy

- User: revoke sessions/tokens; replace email with UUID tombstone; clear phone,
  names, verification and privilege flags; disable login and password.
- Patient profile: replace Digital ID with UUID tombstone; clear names,
  demographics, identity state, and avatar; avatar bytes removed after commit.
- Medical documents: call existing `purge_medical_document`; processing rows,
  events, stored-file row, and stored bytes are removed.
- Identity documents: retain UUID/type/subject/event history; blank document,
  national, family, and card-body numbers; remove dates and image bytes; scrub
  `IdentityFile` metadata; mark revoked/rejected.
- Guardian relationships: end before tombstoning; retain relationship and
  immutable events; remove private evidence rows/files and evidence results.
- Minor creation requests: delete when target or affected relationship/profile
  owns them.
- Claims: cancel and scrub contact/name/date/comparison/review text; remove
  activation and private evidence; retain claim UUID and immutable events.
- Audit: retain immutable rows and stable tombstone references.
  Purge-requested and purge-completed records store actor FK, target UUID,
  reason code, timestamp, and safe counts only. Optional
  free-text detail is validated but not persisted because it could contain PII.

No explicit service/system-account marker exists on `accounts.User`. Current
schema therefore has no separately identifiable service-user class. If one is
introduced, it must add an explicit non-purgeable marker before using this flow.

## Admin safety

Generic user `delete_selected` and object deletion permission are disabled.
Superusers receive `System purge selected users` and `System purge account`.
Both render POST forms protected by CSRF, require reason plus irreversible-action
checkbox, and process each selected user in its own transaction. Outcomes are
reported as `SUCCESS`, `BLOCKED`, or `FAILED`; one failure does not roll back
other completed users.
