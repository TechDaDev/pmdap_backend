import uuid

from audit.services import set_request_id


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
