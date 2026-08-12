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


class IdentityFileStorageFailed(APIException):
    status_code = 503
    default_detail = "Identity file storage is temporarily unavailable."
    default_code = "identity_file_storage_failed"


class IdentityExtractionJobNotFound(NotFound):
    default_detail = "Identity extraction job does not exist or has expired."
    default_code = "extraction_job_not_found"
