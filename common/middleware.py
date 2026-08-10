import uuid

from audit.services import set_request_id


class HealthcheckRedirectExemptMiddleware:
    """Let Railway's plain-HTTP startup healthcheck pass the HTTPS redirect.

    Railway probes ``/api/v1/health/`` over plain HTTP from the
    ``healthcheck.railway.app`` host without an ``X-Forwarded-Proto`` header,
    so ``SECURE_SSL_REDIRECT`` would 301 it and the deploy would never become
    healthy. Mark those requests secure so the redirect is skipped while real
    traffic keeps full HTTPS enforcement.
    """

    HEALTHCHECK_HOST = "healthcheck.railway.app"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.get_host().lower() == self.HEALTHCHECK_HOST:
            request.META["HTTP_X_FORWARDED_PROTO"] = "https"
        return self.get_response(request)


class AuditRequestIdMiddleware:
    """Attach a lightweight per-request correlation ID for audit records."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid.uuid4().hex
        set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            set_request_id("")
        response["X-Request-Id"] = request_id
        return response
