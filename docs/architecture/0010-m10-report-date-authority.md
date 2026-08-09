# ADR 0010: User-Authoritative Report Dates

- Status: Accepted
- Date: 2026-08-09
- Scope: M10 only

## Context

M9 produces explainable but advisory date candidates. Archival ordering needs one
human-authoritative report date without erasing algorithmic evidence, accepting
stale candidates, or weakening adult/minor authorization. Date decisions can race
with retries, reprocessing, and soft deletion.

## Decision

The existing `MedicalDocument` date fields remain the current authority. Selecting
a current candidate sets `USER_CONFIRMED`; explicit manual entry sets
`USER_CORRECTED`. Clients may submit exactly one candidate UUID or date and cannot
set authority, verification, patient, score, or status fields. Manual dates must be
real local-calendar dates no later than `timezone.localdate()`.

Every material decision creates an immutable `DocumentDateEvent` in the same
transaction as the document update. It records actor, previous/new date, authority
source, and an optional protected candidate reference. Exact decision retries are
idempotent and create no duplicate event. Later corrections/reconfirmations are
allowed and append history.

Candidate generations are retained. `candidate_set_uuid` groups a processing run;
`is_current` identifies the selectable generation. Reprocessing retires the prior
generation rather than deleting it. Confirmation locks the document first, then
the candidate, and rejects wrong-document candidates as not found and retired or
wrong-version candidates as stale. It never changes `is_suggested`.

M9 now finishes at `AWAITING_CONFIRMATION` when no verified date exists and
`DATE_CONFIRMED` when a verified M6/M10 date already exists. Reprocessing may
refresh candidates but never overwrites a verified date or its history. Explicit
manual correction is permitted from `AWAITING_CONFIRMATION`, legacy M9 terminal
states, `DATE_CONFIRMED`, and automatic-processing `FAILED`; pre-date-processing
states are rejected.

Adult owners and current verified active guardians of patients under 18 use the
same service through separate route contexts. Authorization is live on every
request. Verification agents and unrelated, inactive, pending, rejected, or aged-
out guardians receive no date authority.

## Consequences

- Algorithmic suggestion and user authority stay independently auditable.
- Document locking serializes confirmation, correction, reprocessing persistence,
  and soft deletion without partial event/current-state mismatches.
- Candidate history grows across explicit reprocessing and needs later retention
  policy work.
- Archive indexing, chronology, search, classification, and event APIs remain
  deferred to later milestones.
- PyMuPDF production licensing remains unresolved: AGPL/commercial dual license.
