from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle


class DynamicScopedRateThrottle(ScopedRateThrottle):
    def get_rate(self):
        return api_settings.DEFAULT_THROTTLE_RATES[self.scope]


class RegistrationIdentityExtractRateThrottle(DynamicScopedRateThrottle):
    scope = "registration_identity_extract"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)


class RegistrationIdentityPollRateThrottle(DynamicScopedRateThrottle):
    scope = "registration_identity_poll"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)
