from rest_framework.exceptions import (
    APIException,
    MethodNotAllowed,
    NotAuthenticated,
    Throttled,
    ValidationError,
)


class UnauthorizedAPIException(APIException):
    status_code = 401
    default_detail = "Authentication failed."
    default_code = "authentication_failed"


class InvalidCredentials(UnauthorizedAPIException):
    default_detail = "Invalid credentials."
    default_code = "invalid_credentials"


class AccountUnavailable(UnauthorizedAPIException):
    default_detail = "Account is unavailable."
    default_code = "account_unavailable"


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return str(value)


def api_exception_handler(exc, context):
    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    if response is None:
        return None

    raw = _plain(response.data)
    if isinstance(exc, ValidationError):
        code = "validation_error"
        message = "Validation failed."
        details = raw
    else:
        codes = exc.get_codes() if hasattr(exc, "get_codes") else None
        code = codes if isinstance(codes, str) else getattr(exc, "default_code", None)
        code = str(code or "api_error")
        if isinstance(exc, NotAuthenticated):
            code = "not_authenticated"
        elif isinstance(exc, MethodNotAllowed):
            code = "method_not_allowed"
        elif isinstance(exc, Throttled):
            code = "throttled"

        detail = raw.get("detail") if isinstance(raw, dict) else raw
        message = str(detail or "Request failed.")
        details = (
            {key: value for key, value in raw.items() if key not in {"detail", "code"}}
            if isinstance(raw, dict)
            else {}
        )

    response.data = {"error": {"code": code, "message": message, "details": details}}
    return response
