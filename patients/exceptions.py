from rest_framework.exceptions import APIException, NotFound


class PatientRoleRequired(APIException):
    status_code = 403
    default_detail = "A PATIENT account is required."
    default_code = "patient_role_required"


class PatientProfileNotFound(NotFound):
    default_detail = "Patient profile does not exist."
    default_code = "patient_profile_not_found"


class PatientProfileExists(APIException):
    status_code = 409
    default_detail = "Patient profile already exists."
    default_code = "patient_profile_exists"
