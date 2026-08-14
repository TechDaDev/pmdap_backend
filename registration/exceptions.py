from rest_framework.exceptions import APIException, NotFound


class RegistrationIdentityJobNotFound(NotFound):
    default_detail = (
        "Registration identity session does not exist or has expired."
    )
    default_code = "registration_job_not_found"


class RegistrationIdentityJobExpired(APIException):
    status_code = 410
    default_detail = (
        "Registration identity session has expired. Please scan the card again."
    )
    default_code = "registration_job_expired"


class RegistrationIdentityJobConflict(APIException):
    status_code = 409
    default_detail = (
        "Registration identity session cannot be completed."
    )
    default_code = "registration_job_conflict"


class RegistrationIdentityStorageFailed(APIException):
    status_code = 503
    default_detail = "Identity file storage is temporarily unavailable."
    default_code = "registration_storage_failed"
