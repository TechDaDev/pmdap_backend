"""Production settings smoke tests (config.settings.production)."""

import importlib

import pytest

import config.settings.production as production
from common.middleware import HealthcheckRedirectExemptMiddleware


def test_production_requires_secret_key(monkeypatch):
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    with pytest.raises(KeyError):
        importlib.reload(production)


def test_production_requires_allowed_hosts(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "x" * 40)
    monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)
    with pytest.raises(RuntimeError, match="DJANGO_ALLOWED_HOSTS is required"):
        importlib.reload(production)


def test_production_hardens_https(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "production-test-secret-key-32bytes!")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com,healthcheck.railway.app")
    importlib.reload(production)

    assert production.DEBUG is False
    assert production.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert production.SECURE_SSL_REDIRECT is True
    assert production.SESSION_COOKIE_SECURE is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SECURE_HSTS_SECONDS > 0


class FakeHealthcheckRequest:
    def __init__(self, host):
        self._host = host
        self.META = {}

    def get_host(self):
        return self._host


def test_healthcheck_middleware_marks_railway_probe_secure():
    called = []

    def get_response(request):
        called.append(request.META.get("HTTP_X_FORWARDED_PROTO"))
        return "ok"

    middleware = HealthcheckRedirectExemptMiddleware(get_response)
    assert middleware(FakeHealthcheckRequest("healthcheck.railway.app")) == "ok"
    assert called == ["https"]


def test_healthcheck_middleware_leaves_other_hosts_alone():
    called = []

    def get_response(request):
        called.append(request.META.get("HTTP_X_FORWARDED_PROTO"))
        return "ok"

    middleware = HealthcheckRedirectExemptMiddleware(get_response)
    assert middleware(FakeHealthcheckRequest("pmdapbackend.up.railway.app")) == "ok"
    assert called == [None]
