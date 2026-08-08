from rest_framework.exceptions import APIException, NotFound, PermissionDenied


class GuardianNotVerified(PermissionDenied):
    default_detail = "A verified adult patient guardian is required."
    default_code = "guardian_not_verified"


class PatientNotMinor(APIException):
    status_code = 400
    default_detail = "The patient must be younger than 18."
    default_code = "patient_not_minor"


class IdempotencyKeyRequired(APIException):
    status_code = 400
    default_detail = "Idempotency-Key header is required."
    default_code = "idempotency_key_required"


class InvalidIdempotencyKey(APIException):
    status_code = 400
    default_detail = "Idempotency-Key must contain 1 to 128 non-whitespace characters."
    default_code = "invalid_idempotency_key"


class IdempotencyConflict(APIException):
    status_code = 409
    default_detail = "Idempotency-Key was already used for a different request."
    default_code = "idempotency_conflict"


class RelationshipEvidenceRequired(APIException):
    status_code = 400
    default_detail = "Official evidence is required for a legal guardian."
    default_code = "relationship_evidence_required"


class GuardianRelationshipNotFound(NotFound):
    default_detail = "Guardian relationship does not exist."
    default_code = "not_found"


class GuardianRelationshipConflict(APIException):
    status_code = 409
    default_detail = "Guardian relationship cannot make that transition."
    default_code = "relationship_transition_conflict"
