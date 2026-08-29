from config.settings.base import *  # noqa: F403

SECRET_KEY = "test-only-secret-key-at-least-thirty-two-bytes"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pmdap-tests",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Tests must never attempt a real provider send. Forcing the key empty makes
# the M31B OTP delivery resolver pick Django's (locmem) delivery deterministically,
# regardless of host environment variables.
RESEND_API_KEY = ""
RESEND_FROM_EMAIL = "onboarding@resend.dev"
