from rest_framework.throttling import UserRateThrottle


class MedicalDocumentUploadThrottle(UserRateThrottle):
    scope = "medical_document_upload"
