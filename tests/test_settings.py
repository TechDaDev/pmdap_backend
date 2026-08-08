import importlib
import sys

import pytest


def reload_settings_module(module_name):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_local_settings_enable_debug():
    settings = reload_settings_module("config.settings.local")

    assert settings.DEBUG is True


def test_production_settings_require_secret_key(monkeypatch):
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)

    with pytest.raises(KeyError, match="DJANGO_SECRET_KEY"):
        reload_settings_module("config.settings.production")


def test_production_settings_require_allowed_hosts(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "production-test-secret")
    monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)

    with pytest.raises(RuntimeError, match="DJANGO_ALLOWED_HOSTS"):
        reload_settings_module("config.settings.production")


def test_production_settings_enable_https_controls(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "production-test-secret")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "api.example.test")

    settings = reload_settings_module("config.settings.production")

    assert settings.DEBUG is False
    assert settings.ALLOWED_HOSTS == ["api.example.test"]
    assert settings.SECURE_SSL_REDIRECT is True
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SECURE is True
    assert settings.SECURE_HSTS_SECONDS == 31_536_000
    assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert settings.SECURE_HSTS_PRELOAD is True
    assert settings.X_FRAME_OPTIONS == "DENY"
