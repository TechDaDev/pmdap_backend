"""
Consolidated IDOR regression sweep (M14).

Re-verifies the actor matrix across the key patient-scoped endpoints in one
place, so the M14 audit wiring cannot silently introduce an authorization
regression. Individual endpoints are covered in depth by their own phase
tests (M6/M10/M12/M13); this file is the cross-cutting safety net.
"""

from datetime import date

import pytest
from django.test import override_settings

from accounts.models import User
from identities.models import IdentityDocument, IdentityFile
from patients.models import PatientProfile
from tests.test_medical_documents_api import COLLECTION, patient_user
from tests.test_minor_medical_documents_api import (
    png_upload,
    verified_guardian,
)

pytestmark = pytest.mark.django_db

MINORS = "/api/v1/minors/"


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def verified_patient(user, digital_id):
    profile = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Sweep Patient",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
        identity_status=PatientProfile.IdentityStatus.VERIFIED,
    )
    identity_file = IdentityFile.objects.create(
        file=f"identity/{digital_id}.png",
        original_name="card.png",
        media_type="image/png",
        size=10,
        sha256="b" * 64,
    )
    IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number=f"CARD-{digital_id}",
        national_number=f"NAT-{digital_id}",
        family_number="FAM-1",
        issuing_country="IQ",
        front_image=identity_file,
        back_image=identity_file,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
        status=IdentityDocument.LifecycleStatus.CURRENT,
    )
    return profile


def make_document(api_client, user, profile, tmp_path):
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=user)
        response = api_client.post(
            COLLECTION,
            {
                "document_type": "LABORATORY",
                "title": "IDOR Sweep Panel",
                "file": png_upload(),
            },
            format="multipart",
        )
        assert response.status_code == 201, response.content
        document = response.data["data"]
        return document


def test_medical_document_actor_matrix(api_client, tmp_path):
    owner, owner_profile = patient_user()
    doc = make_document(api_client, owner, owner_profile, tmp_path)
    detail_url = f"{COLLECTION}{doc['uuid']}/"
    file_url = f"{detail_url}file/"

    # Owner.
    api_client.force_authenticate(user=owner)
    assert api_client.get(detail_url).status_code == 200
    # Unauthenticated.
    api_client.force_authenticate()
    assert api_client.get(detail_url).status_code == 401
    # Unrelated verified patient.
    other, _ = patient_user(
        email="idor-other@example.com", digital_id="76543210987654321"
    )
    api_client.force_authenticate(user=other)
    assert api_client.get(detail_url).status_code == 404
    assert api_client.get(file_url).status_code == 404
    # Verification agent must not impersonate a patient.
    agent = User.objects.create_user(
        email="idor-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    api_client.force_authenticate(user=agent)
    assert api_client.get(detail_url).status_code in (403, 404)


def test_identity_document_actor_matrix(api_client):
    owner = verified_guardian(
        email="idor-identity-owner@example.com", digital_id="12345678901234501"
    )
    owner_profile = PatientProfile.objects.get(user=owner)
    identity = IdentityDocument.objects.get(patient=owner_profile)
    detail = f"/api/v1/identity-documents/{identity.uuid}/"

    api_client.force_authenticate(user=owner)
    assert api_client.get(detail).status_code == 200

    other = verified_guardian(
        email="idor-identity-other@example.com", digital_id="12345678901234502"
    )
    api_client.force_authenticate(user=other)
    assert api_client.get(detail).status_code == 404


def test_minor_document_actor_matrix(api_client, tmp_path):
    from guardians.models import GuardianRelationship

    guardian = verified_guardian(
        email="idor-minor-guardian@example.com", digital_id="12345678901234503"
    )
    minor_user = User.objects.create_user(
        email="idor-minor-child@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    minor_profile = PatientProfile.objects.create(
        user=minor_user,
        digital_id="12345678901234504",
        full_name="Child Patient",
        date_of_birth=date(2015, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
        identity_status=PatientProfile.IdentityStatus.VERIFIED,
    )
    relationship = GuardianRelationship.objects.create(
        guardian_user=guardian,
        minor_patient=minor_profile,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )
    assert relationship.pk
    doc = make_document(api_client, minor_user, minor_profile, tmp_path)
    minor_url = f"{MINORS}{minor_profile.uuid}/documents/{doc['uuid']}/"

    # Verified guardian of the minor.
    api_client.force_authenticate(user=guardian)
    assert api_client.get(minor_url).status_code == 200

    # Unrelated verified guardian.
    unrelated = verified_guardian(
        email="idor-minor-unrelated@example.com", digital_id="12345678901234505"
    )
    api_client.force_authenticate(user=unrelated)
    assert api_client.get(minor_url).status_code == 404

    # Unauthenticated.
    api_client.force_authenticate()
    assert api_client.get(minor_url).status_code == 401


def test_archive_and_search_actor_matrix(api_client, tmp_path):
    owner, owner_profile = patient_user()
    make_document(api_client, owner, owner_profile, tmp_path)
    archive_url = "/api/v1/archive/summary/"
    search_url = "/api/v1/search/"

    api_client.force_authenticate(user=owner)
    assert api_client.get(archive_url).status_code == 200

    from django.db import connection

    if connection.vendor == "postgresql":
        assert api_client.get(search_url, {"q": "Panel"}).status_code == 200

    # Unauthenticated.
    api_client.force_authenticate()
    assert api_client.get(archive_url).status_code == 401
    if connection.vendor == "postgresql":
        assert api_client.get(search_url, {"q": "Panel"}).status_code == 401
