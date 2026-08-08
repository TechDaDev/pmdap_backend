from accounts.throttles import DynamicScopedRateThrottle


class AccountClaimSubmitThrottle(DynamicScopedRateThrottle):
    scope = "account_claim_submit"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)


class AccountClaimActivationThrottle(DynamicScopedRateThrottle):
    scope = "account_claim_activation"

    def get_cache_key(self, request, view):
        view.throttle_scope = self.scope
        return super().get_cache_key(request, view)
