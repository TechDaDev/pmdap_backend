from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from accounts.services import token_matches_current_session
from common.exceptions import AccountUnavailable


class ActiveAccountJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        try:
            user = super().get_user(validated_token)
        except AuthenticationFailed as exc:
            raise AccountUnavailable() from exc

        if user.status != user.Status.ACTIVE:
            raise AccountUnavailable()
        if not token_matches_current_session(validated_token, user):
            raise AccountUnavailable()
        return user


class ActiveAccountJWTScheme(OpenApiAuthenticationExtension):
    target_class = ActiveAccountJWTAuthentication
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
