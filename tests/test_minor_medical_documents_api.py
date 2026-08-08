import io
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image

from accounts.models import User
from guardians.models import GuardianRelationship
from identities.models import IdentityDocument, IdentityFile
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db


def png_upload(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return SimpleUploadedFile(
        "minor-report.png", output.getvalue(), content_type="image/png"
    )


def verified_guardian(*, email, digital_id):
    user = User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Verified Guardian",
        date_of_birth=date(1985, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
        identity_status=PatientProfile.IdentityStatus.VERIFIED,
    )
    identity_file = IdentityFile.objects.create(
        file=f"identity/{digital_id}.png",
        original_name="card.png",
        media_type="image/png",
        size=10,
        sha256="a" * 64,
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
    return user


def minor(*, digital_id="30000000000000001", date_of_birth=None):
    return PatientProfile.objects.create(
        digital_id=digital_id,
        full_name="Minor Patient",
        date_of_birth=date_of_birth or date(2015, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )


def relationship(guardian, patient, *, status="VERIFIED", active=True, kind="FATHER"):
    return GuardianRelationship.objects.create(
        guardian_user=guardian,
        minor_patient=patient,
        relationship=kind,
        verification_status=status,
        active=active,
    )


def collection(patient):
    return f"/api/v1/minors/{patient.uuid}/documents/"


def payload(color="navy"):
    return {"file": png_upload(color), "document_type": "MEDICAL_REPORT"}


def assert_not_found(response):
    assert response.status_code == 404
    assert response.data["error"]["code"] in {
        "guardian_relationship_not_found",
        "medical_document_not_found",
        "not_found",
    }


def test_verified_guardian_uploads_to_minor_and_can_manage_document(
    api_client, tmp_path
):
    guardian = verified_guardian(
        email="father@example.com", digital_id="10000000000000001"
    )
    patient = minor()
    relationship(guardian, patient)
    api_client.force_authenticate(user=guardian)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(collection(patient), payload(), format="multipart")
        document_uuid = created.data["data"]["uuid"]
        detail_url = f"{collection(patient)}{document_uuid}/"
        listing = api_client.get(collection(patient))
        detail = api_client.get(detail_url)
        updated = api_client.patch(detail_url, {"title": "Child report"}, format="json")
        streamed = api_client.get(f"{detail_url}file/")
        content = b"".join(streamed.streaming_content)
        deleted = api_client.delete(detail_url)

    document = patient.medical_documents.get()
    assert created.status_code == 201
    assert document.patient == patient
    assert document.uploaded_by == guardian
    assert listing.data["data"]["count"] == 1
    assert detail.status_code == 200
    assert updated.data["data"]["title"] == "Child report"
    assert streamed.status_code == 200 and content
    assert deleted.status_code == 204


def test_mother_and_father_have_independent_live_authority(api_client, tmp_path):
    father = verified_guardian(
        email="father2@example.com", digital_id="10000000000000002"
    )
    mother = verified_guardian(
        email="mother@example.com", digital_id="10000000000000003"
    )
    patient = minor(digital_id="30000000000000002")
    relationship(father, patient, kind="FATHER")
    relationship(mother, patient, kind="MOTHER")

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=father)
        created = api_client.post(
            collection(patient), payload(), format="multipart"
        ).data["data"]
        api_client.force_authenticate(user=mother)
        response = api_client.get(f"{collection(patient)}{created['uuid']}/")

    assert response.status_code == 200


@pytest.mark.parametrize("state", ["unrelated", "pending", "rejected", "inactive"])
def test_non_live_guardian_authority_is_denied(api_client, tmp_path, state):
    guardian = verified_guardian(
        email=f"{state}@example.com",
        digital_id={
            "unrelated": "10000000000000004",
            "pending": "10000000000000005",
            "rejected": "10000000000000006",
            "inactive": "10000000000000007",
        }[state],
    )
    patient = minor(digital_id="30000000000000003")
    if state in {"pending", "rejected"}:
        relationship(guardian, patient, status=state.upper(), active=False)
    if state == "inactive":
        relationship(guardian, patient)
        guardian.status = User.Status.DISABLED
        guardian.save(update_fields=("status",))
    api_client.force_authenticate(user=guardian)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(collection(patient), payload(), format="multipart")

    assert_not_found(response)


def test_guardian_loses_detail_file_update_delete_and_list_on_exact_18th_birthday(
    api_client,
    tmp_path,
):
    guardian = verified_guardian(
        email="ageout@example.com", digital_id="10000000000000008"
    )
    patient = minor(digital_id="30000000000000004")
    relationship(guardian, patient)
    api_client.force_authenticate(user=guardian)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            collection(patient), payload(), format="multipart"
        ).data["data"]
        today = timezone.localdate()
        patient.date_of_birth = today.replace(year=today.year - 18)
        patient.save(update_fields=("date_of_birth", "updated_at"))
        detail_url = f"{collection(patient)}{created['uuid']}/"
        responses = [
            api_client.get(collection(patient)),
            api_client.get(detail_url),
            api_client.get(f"{detail_url}file/"),
            api_client.patch(detail_url, {"title": "No"}, format="json"),
            api_client.delete(detail_url),
        ]

    for response in responses:
        assert_not_found(response)


def test_guardian_cannot_use_minor_route_for_unrelated_document(api_client, tmp_path):
    guardian = verified_guardian(
        email="isolation@example.com", digital_id="10000000000000009"
    )
    authorized = minor(digital_id="30000000000000005")
    unrelated = minor(digital_id="30000000000000006")
    relationship(guardian, authorized)
    relationship(guardian, unrelated, kind="MOTHER")
    api_client.force_authenticate(user=guardian)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            collection(unrelated), payload(), format="multipart"
        ).data["data"]
        response = api_client.get(f"{collection(authorized)}{created['uuid']}/")

    assert_not_found(response)


def test_non_authoritative_guardians_cannot_manage_existing_minor_document(
    api_client,
    tmp_path,
):
    uploader = verified_guardian(
        email="authority-uploader@example.com", digital_id="10000000000000010"
    )
    pending = verified_guardian(
        email="authority-pending@example.com", digital_id="10000000000000011"
    )
    rejected = verified_guardian(
        email="authority-rejected@example.com", digital_id="10000000000000012"
    )
    unrelated = verified_guardian(
        email="authority-unrelated@example.com", digital_id="10000000000000013"
    )
    patient = minor(digital_id="30000000000000007")
    relationship(uploader, patient, kind="FATHER")
    relationship(pending, patient, status="PENDING", active=False, kind="MOTHER")
    relationship(
        rejected,
        patient,
        status="REJECTED",
        active=False,
        kind="LEGAL_GUARDIAN",
    )

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=uploader)
        created = api_client.post(
            collection(patient), payload(), format="multipart"
        ).data["data"]
        detail_url = f"{collection(patient)}{created['uuid']}/"

        for guardian in (pending, rejected, unrelated):
            api_client.force_authenticate(user=guardian)
            responses = (
                api_client.get(detail_url),
                api_client.get(f"{detail_url}file/"),
                api_client.patch(detail_url, {"title": "Denied"}, format="json"),
                api_client.delete(detail_url),
            )
            for response in responses:
                assert_not_found(response)

        active_relationship = GuardianRelationship.objects.get(
            guardian_user=uploader,
            minor_patient=patient,
            active=True,
        )
        active_relationship.active = False
        active_relationship.ended_at = timezone.now()
        active_relationship.ended_reason = GuardianRelationship.EndedReason.REVOKED
        active_relationship.save(
            update_fields=("active", "ended_at", "ended_reason", "updated_at")
        )
        api_client.force_authenticate(user=uploader)
        for response in (
            api_client.get(detail_url),
            api_client.get(f"{detail_url}file/"),
            api_client.patch(detail_url, {"title": "Denied"}, format="json"),
            api_client.delete(detail_url),
        ):
            assert_not_found(response)
