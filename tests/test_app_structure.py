from django.apps import apps

PROJECT_APPS = (
    "accounts",
    "patients",
    "identities",
    "guardians",
    "claims",
    "documents",
    "processing",
    "archive",
    "facilities",
    "audit",
    "common",
)


def test_all_phase_one_app_boundaries_are_installed():
    missing = [app_name for app_name in PROJECT_APPS if not apps.is_installed(app_name)]

    assert missing == []
