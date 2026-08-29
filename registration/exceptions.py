from rest_framework.exceptions import APIException, NotFound


class RegistrationEmailNotVerified(APIException):
    status_code = 403
    default_detail = (
        "Email verification is required before identity upload can begin."
    )
    default_code = "registration_email_not_verified"


class RegistrationSessionNotFound(NotFound):
    default_detail = (
        "Registration session does not exist or has expired."
    )
    default_code = "registration_session_not_found"


class RegistrationSessionExpired(APIException):
    status_code = 410
    default_detail = (
        "Registration session has expired. Please start again."
    )
    default_code = "registration_session_expired"


class RegistrationSessionConflict(APIException):
    status_code = 409
    default_detail = (
        "Registration session cannot be reused."
    )
    default_code = "registration_session_conflict"


class RegistrationEmailAlreadyVerified(APIException):
    status_code = 409
    default_detail = "This email has already been verified."
    default_code = "registration_email_already_verified"


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
