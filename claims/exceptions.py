from rest_framework.exceptions import APIException, NotFound, PermissionDenied


class VerificationAgentRequired(PermissionDenied):
    default_detail = "Identity verification agent role is required."
    default_code = "verification_agent_required"


class AccountClaimNotFound(NotFound):
    default_detail = "Account claim does not exist."
    default_code = "not_found"


class AccountClaimConflict(APIException):
    status_code = 409
    default_detail = "Account claim cannot make that transition."
    default_code = "account_claim_transition_conflict"


class InvalidActivationToken(APIException):
    status_code = 400
    default_detail = "Activation token is invalid or unavailable."
    default_code = "invalid_activation_token"
