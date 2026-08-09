from rest_framework.exceptions import APIException


class DuplicateMedicalDocument(APIException):
    status_code = 409
    default_detail = "This medical document is already active for the patient."
    default_code = "duplicate_medical_document"


class MedicalDocumentNotFound(APIException):
    status_code = 404
    default_detail = "Medical document not found."
    default_code = "medical_document_not_found"


class MedicalFileStorageFailed(APIException):
    status_code = 503
    default_detail = "Medical file storage is temporarily unavailable."
    default_code = "medical_file_storage_failed"


class MedicalFileUnavailable(APIException):
    status_code = 409
    default_detail = "Medical file is unavailable because integrity checks failed."
    default_code = "medical_file_unavailable"


class InvalidDateConfirmation(APIException):
    status_code = 400
    default_detail = "Provide exactly one of candidate_id or date."
    default_code = "invalid_date_confirmation"


class InvalidDocumentDate(APIException):
    status_code = 400
    default_detail = "Document date is invalid."
    default_code = "invalid_document_date"


class DateCandidateNotFound(APIException):
    status_code = 404
    default_detail = "Date candidate not found."
    default_code = "date_candidate_not_found"


class DateCandidateStale(APIException):
    status_code = 409
    default_detail = "Date candidate is no longer current."
    default_code = "date_candidate_stale"


class InvalidDateConfirmationState(APIException):
    status_code = 409
    default_detail = "Document is not ready for date confirmation."
    default_code = "invalid_date_confirmation_state"
