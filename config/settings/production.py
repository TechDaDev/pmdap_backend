import os

from config.settings.base import *  # noqa: F403

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")  # noqa: F405

if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS is required in production")

DEBUG = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
# Railway's startup healthcheck probes over plain HTTP using the
# healthcheck.railway.app hostname. Keep HTTPS redirects for real traffic but
# exempt the healthcheck host so it can return a 200 liveness response.
SECURE_REDIRECT_EXEMPT = env_list("DJANGO_SECURE_REDIRECT_EXEMPT")  # noqa: F405
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
