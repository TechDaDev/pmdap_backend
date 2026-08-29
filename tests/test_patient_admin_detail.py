"""PatientProfile admin change-page regression tests.

The PatientProfile admin change page previously raised
``ValueError: Private avatar files do not have public URLs.`` whenever a
profile had an uploaded avatar: the private avatar storage has no public URL
and Django's default admin file widget probes ``value.url`` while building the
"Currently: <file>" row. These tests render the change page across the
admin-relevant data shapes (adult, no identity, unverified/verified identity,
minor, guardian relationship, legacy/null optional fields, avatar, superuser)
and assert the page returns 200 with no exception. The list page is covered
too. Only synthetic data is used.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"


@pytest.fixture(autouse=True)
def private_avatar_root(settings, tmp_path):
    settings.AVATAR_FILE_ROOT = tmp_path / "private-avatar"


@pytest.fixture
def superuser():
    user = UserFactory(email="admin@example.com", status="ACTIVE")
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=("is_staff", "is_superuser"))
    return user


@pytest.fixture
def admin_client(superuser):
    from django.test import Client

    client = Client()
    client.force_login(superuser)
    return client


def make_profile(*, user=None, full_name="Test Patient", dob="1990-01-01", **extra):
    from patients.services import create_patient_profile

    return create_patient_profile(
        user=user,
        full_name=full_name,
        date_of_birth=dob,
        sex="MALE",
        nationality="IQ",
        blood_group="O+",
        **extra,
    )


def change_page(client, profile):
    return client.get(f"/admin/patients/patientprofile/{profile.pk}/change/")


def attach_avatar(profile, name="avatar.png"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8)).save(stream, format="PNG")
    profile.avatar.save(
        name,
        SimpleUploadedFile(name, stream.getvalue(), content_type="image/png"),
        save=True,
    )
    profile.refresh_from_db()


def make_identity_document(profile, *, verification_status="PENDING"):
    from identities.models import IdentityDocument
    from identities.services import persist_identity_upload

    raw = io.BytesIO()
    Image.new("RGB", (8, 8)).save(raw, format="JPEG")
    front = persist_identity_upload(
        SimpleUploadedFile("front.jpg", raw.getvalue(), content_type="image/jpeg")
    )
    return IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number="DOC-12345678",
        national_number="NAT-87654321",
        family_number="FAM-0001",
        issuing_country="IQ",
        front_image=front,
        verification_status=verification_status,
    )


# --------------------------------------------------------------------------- #
# Change page across data shapes
# --------------------------------------------------------------------------- #


def test_change_page_normal_adult_patient(admin_client):
    user = UserFactory(status="ACTIVE")
    profile = make_profile(user=user)

    assert change_page(admin_client, profile).status_code == 200


def test_change_page_patient_without_identity_document(admin_client):
    user = UserFactory(status="ACTIVE")
    profile = make_profile(user=user)
    assert profile.identity_documents.count() == 0

    assert change_page(admin_client, profile).status_code == 200


def test_change_page_unverified_identity(admin_client):
    user = UserFactory(status="ACTIVE")
    profile = make_profile(user=user)
    make_identity_document(profile, verification_status="PENDING")
    assert profile.identity_status == profile.IdentityStatus.UNVERIFIED

    assert change_page(admin_client, profile).status_code == 200


def test_change_page_verified_identity(admin_client):
    user = UserFactory(status="ACTIVE")
    profile = make_profile(user=user)
    profile.identity_status = profile.IdentityStatus.VERIFIED
    profile.save(update_fields=("identity_status",))
    make_identity_document(profile, verification_status="VERIFIED")

    assert change_page(admin_client, profile).status_code == 200


def test_change_page_minor_patient(admin_client):
    minor = make_profile(user=None, full_name="Minor Child", dob="2012-05-01")
    assert minor.is_minor
    assert minor.user is None

    assert change_page(admin_client, minor).status_code == 200


def test_change_page_guardian_relationship(admin_client):
    guardian = UserFactory(status="ACTIVE")
    minor = make_profile(user=None, full_name="Minor Child", dob="2012-05-01")
    from guardians.models import GuardianRelationship

    GuardianRelationship.objects.create(
        guardian_user=guardian,
        minor_patient=minor,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )

    assert change_page(admin_client, minor).status_code == 200


def test_change_page_legacy_null_optional_fields(admin_client):
    # Legacy profile: no direct owner, blank optional identity fields.
    profile = make_profile(
        user=None,
        full_name="Legacy Patient",
        given_name="",
        father_name="",
        grandfather_name="",
        mother_name="",
        governorate="",
    )
    assert profile.user is None

    assert change_page(admin_client, profile).status_code == 200


def test_change_page_with_avatar_returns_200(admin_client):
    # Regression for the private-avatar storage 500: the change page must
    # render an uploaded avatar without calling storage.url().
    user = UserFactory(status="ACTIVE")
    profile = make_profile(user=user)
    attach_avatar(profile)

    response = change_page(admin_client, profile)

    assert response.status_code == 200


def test_change_page_superuser_access(admin_client):
    user = UserFactory(status="ACTIVE")
    profile = make_profile(user=user)

    assert change_page(admin_client, profile).status_code == 200


# --------------------------------------------------------------------------- #
# List page
# --------------------------------------------------------------------------- #


def test_list_page_returns_200(admin_client):
    user = UserFactory(status="ACTIVE")
    make_profile(user=user)

    response = admin_client.get("/admin/patients/patientprofile/")

    assert response.status_code == 200
