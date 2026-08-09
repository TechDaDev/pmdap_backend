from rest_framework.exceptions import APIException


class HealthcareFacilityNotFound(APIException):
    status_code = 404
    default_detail = "Healthcare facility not found."
    default_code = "healthcare_facility_not_found"


class HealthcareFacilityInactive(APIException):
    status_code = 409
    default_detail = "Healthcare facility is inactive."
    default_code = "healthcare_facility_inactive"


class InvalidLocationHierarchy(APIException):
    status_code = 400
    default_detail = "Location hierarchy is invalid."
    default_code = "invalid_location_hierarchy"
