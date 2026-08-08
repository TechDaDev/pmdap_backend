from rest_framework.exceptions import APIException, NotFound, PermissionDenied


class IdentityDocumentNotFound(NotFound):
    default_detail = "Identity document does not exist."
    default_code = "not_found"


class VerificationAgentRequired(PermissionDenied):
    default_detail = "Identity verification agent role is required."
    default_code = "verification_agent_required"


class IdentityDocumentConflict(APIException):
    status_code = 409
    default_detail = "Use the explicit replacement workflow for this document type."
    default_code = "identity_document_conflict"


class IdentityTransitionConflict(APIException):
    status_code = 409
    default_detail = "Identity document cannot make that transition."
    default_code = "identity_transition_conflict"
