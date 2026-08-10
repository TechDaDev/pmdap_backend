"""Production settings smoke tests (config.settings.production)."""

import importlib
import re

import pytest

import config.settings.production as production


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


def test_production_hardens_https_and_keeps_healthcheck_exempt(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "production-test-secret-key-32bytes!")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com,healthcheck.railway.app")
    monkeypatch.setenv("DJANGO_SECURE_REDIRECT_EXEMPT", "healthcheck.railway.app")
    importlib.reload(production)

    assert production.DEBUG is False
    assert production.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert production.SECURE_SSL_REDIRECT is True
    assert production.SESSION_COOKIE_SECURE is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SECURE_HSTS_SECONDS > 0
    assert "healthcheck.railway.app" in production.SECURE_REDIRECT_EXEMPT
    assert re.fullmatch("healthcheck.railway.app", "healthcheck.railway.app")


def test_production_redirect_exempt_defaults_empty(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "y" * 40)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.delenv("DJANGO_SECURE_REDIRECT_EXEMPT", raising=False)
    importlib.reload(production)
    assert production.SECURE_REDIRECT_EXEMPT == []

