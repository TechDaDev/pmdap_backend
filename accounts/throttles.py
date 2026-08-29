from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle


class DynamicScopedRateThrottle(ScopedRateThrottle):
    def get_rate(self):
        return api_settings.DEFAULT_THROTTLE_RATES[self.scope]


class RegisterRateThrottle(DynamicScopedRateThrottle):
    scope = "auth_register"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)


class LoginRateThrottle(DynamicScopedRateThrottle):
    scope = "auth_login"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)


class PasswordResetRequestRateThrottle(DynamicScopedRateThrottle):
    scope = "password_reset_request"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)


class PasswordResetVerifyRateThrottle(DynamicScopedRateThrottle):
    scope = "password_reset_verify"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)


class PasswordResetConfirmRateThrottle(DynamicScopedRateThrottle):
    scope = "password_reset_confirm"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)
