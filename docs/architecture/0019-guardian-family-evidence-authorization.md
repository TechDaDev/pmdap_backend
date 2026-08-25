# ADR 0019: Guardian family evidence and authorization

Status: Accepted

## Context

Family numbers are sensitive identity evidence. They are shared by a household and cannot independently prove that a user may access a minor's medical record. Existing PMDAP medical routes already authorize a verified, active guardian relationship; M27 preserves that behavior while hardening relationship creation, review, replacement, and revocation.

## Decision

Authorization is one central predicate: the requester is an eligible verified adult patient and has a `VERIFIED`, active, unended `GuardianRelationship` to a patient currently younger than 18. Family evidence is not checked on each medical request and never grants access without that relationship.

For `FATHER` and `MOTHER`, approval requires an exact match after conservative normalization of the two CURRENT+VERIFIED Unified National Cards. Normalization trims outer whitespace, removes internal whitespace, and uppercases Latin letters. It never guesses visually similar characters. `MOTHER` remains a human-reviewed transition. `FATHER` also records deterministic exact supporting name evidence when the verified profile data supplies it; fuzzy comparison is prohibited.

`LEGAL_GUARDIAN` is the exceptional path. Family evidence may be mismatched or unavailable, but at least one persisted official guardian evidence record is mandatory and approval is always manual.

Minor date of birth is provisional until a primary child identity is verified. Approval requires the minor profile to be identity-verified and still younger than 18. This records the existing V1 human-confirmation boundary: the identity reviewer confirms the profile and its primary identity; OCR is not run during guardian review.

Evidence records persist only outcomes, source document references, check time, and policy version. They do not duplicate raw family numbers. Client-supplied `family_number` is rejected by the minor-create contract. A family number on an identity document becomes authoritative only after that document is verified.

Approval, rejection, revocation, and identity-driven invalidation are service-layer transitions. Review authorization is shared with identity verification: superusers and active identity-verification agents may review; ordinary staff may not. Review queues omit dates of birth and raw family, national, and card numbers.

Revocation is atomic, records the end timestamp/reason plus immutable event and audit records, and immediately fails the central authorization predicate. A guardian may revoke their own link; a verification agent or superuser may revoke any link. GET never mutates state.

When a verified national card replacement changes authoritative family evidence, active parent relationships are re-evaluated in the identity-approval transaction. A non-match or unavailable result ends the relationship as `RELATIONSHIP_INVALIDATED`; V1 uses the existing ended lifecycle rather than adding a review-required state. Legal-guardian links are not family-revalidated.

A PostgreSQL partial unique constraint permits only one unended `PENDING` or `VERIFIED` tuple for guardian, minor, and relationship type. Rejected or ended history remains immutable and a new request may be submitted.

## Security matrix

| Actor/state | Queue/detail | Approve/reject | Revoke | Medical access |
|---|---:|---:|---:|---:|
| Verification agent | Yes | Yes | Yes | No, unless independently linked as patient guardian |
| Superuser | Yes | Yes | Yes | No, unless independently linked as patient guardian |
| Ordinary staff | No | No | No | No |
| Relationship owner | No | No | Own link only | Yes only while central predicate passes |
| Unrelated patient | No | No | No (not-found) | No |
| Pending/rejected/ended relationship | Reviewer only | State-dependent | No | No |
| Family match without verified relationship | No | No | No | No |

## Consequences

Existing verified active guardian medical access remains available without per-request identity joins. Parent relationship approval becomes stricter and older tests or clients that supplied family numbers during minor creation must migrate to identity verification. Identity replacement can immediately terminate parent access, which is intentionally fail-closed and auditable.
