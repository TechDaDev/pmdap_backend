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
