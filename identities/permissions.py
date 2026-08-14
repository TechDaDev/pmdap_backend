"""Single authoritative identity-verification authorization rule.

Used by the domain services (approve/reject), the operations-console views
(queue/review/mutations/images) and the admin UI button rendering so the UI
and the service can never disagree about who may verify.
"""
from accounts.models import User


def can_verify_identity(user) -> bool:
    """A verifier is authorized when authenticated + active + (superuser OR
    IDENTITY_VERIFICATION_AGENT).

    Plain ``is_staff`` deliberately does NOT grant verification authority —
    ordinary staff must stay blocked from identity-verification actions.
    """
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and user.is_active
        and (
            user.is_superuser
            or user.role == User.Role.IDENTITY_VERIFICATION_AGENT
        )
    )
