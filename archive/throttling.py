from rest_framework.throttling import UserRateThrottle


class MedicalSearchThrottle(UserRateThrottle):
    scope = "medical_search"
