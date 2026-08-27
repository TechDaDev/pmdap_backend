import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.admin import CustomUserAdmin
from accounts.purge import (
    can_system_purge_users,
    is_last_active_superuser,
    preview_user_purge,
    purge_user_account_as_superuser,
)
from audit.models import AuditLog
from audit.services import record_audit
from claims.models import PatientAccountClaim, PatientAccountClaimEvent
from documents.models import MedicalDocument, StoredFile
from guardians.models import GuardianRelationship, GuardianRelationshipEvent
from identities.models import IdentityDocument, IdentityDocumentEvent, IdentityFile
from patients.services import create_patient_profile
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def private_storage_roots(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "identity"
    settings.MEDICAL_FILE_ROOT = tmp_path / "medical"
    settings.AVATAR_FILE_ROOT = tmp_path / "avatar"


def make_superuser(email="root@example.com"):
    user = UserFactory(email=email, status="ACTIVE")
    user.is_active = True
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=("is_active", "is_staff", "is_superuser"))
    return user


def make_profile(user, *, dob="1990-01-01"):
    return create_patient_profile(
        user=user,
        full_name="Synthetic Purge Patient",
        date_of_birth=dob,
        sex="MALE",
        nationality="IQ",
        blood_group="O+",
    )


@pytest.mark.django_db
def test_system_purge_permission_is_superuser_only():
    patient = UserFactory(status="ACTIVE")
    staff = UserFactory(status="ACTIVE")
    staff.is_staff = True
    staff.save(update_fields=("is_staff",))
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    root = make_superuser()

    assert can_system_purge_users(root)
    assert not can_system_purge_users(patient)
    assert not can_system_purge_users(staff)
    assert not can_system_purge_users(agent)
    with pytest.raises(PermissionDenied):
        preview_user_purge(actor=staff, target=patient)


@pytest.mark.django_db
def test_self_purge_is_blocked_even_for_last_active_superuser():
    root = make_superuser()
    assert is_last_active_superuser(root)
    with pytest.raises(ValidationError, match="Self-purge"):
        purge_user_account_as_superuser(
            actor=root, target=root, reason="TEST_ACCOUNT_CLEANUP"
        )


@pytest.mark.django_db(transaction=True)
def test_domain_purge_removes_private_data_and_retains_scrubbed_history():
    actor = make_superuser()
    target = UserFactory(email="purge-target@example.com", status="ACTIVE")
    profile = make_profile(target)
    minor = create_patient_profile(
        user=None,
        full_name="Synthetic Minor",
        date_of_birth="2015-01-01",
        sex="FEMALE",
        nationality="IQ",
        blood_group="A+",
    )

    identity_file = IdentityFile.objects.create(
        file=SimpleUploadedFile("card.jpg", b"private-card-bytes"),
        original_name="card.jpg",
        media_type="image/jpeg",
        size=18,
        sha256="a" * 64,
    )
    identity_storage = identity_file.file.storage
    identity_name = identity_file.file.name
    identity = IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number="PRIVATE-DOC",
        national_number="PRIVATE-NATIONAL",
        family_number="PRIVATE-FAMILY",
        issuing_country="IQ",
        front_image=identity_file,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
    )
    identity_event = IdentityDocumentEvent.objects.create(
        document=identity,
        event_type=IdentityDocumentEvent.EventType.VERIFIED,
        actor=target,
        metadata={},
    )

    relationship = GuardianRelationship.objects.create(
        guardian_user=target,
        minor_patient=minor,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )
    relationship_event = GuardianRelationshipEvent.objects.create(
        relationship=relationship,
        event_type=GuardianRelationshipEvent.EventType.VERIFIED,
        actor=target,
        metadata={},
    )

    claim = PatientAccountClaim.objects.create(
        patient=profile,
        requested_email=target.email,
        requested_phone="07700000000",
        submitted_name="Private Name",
        submitted_date_of_birth="1990-01-01",
    )
    claim_event = PatientAccountClaimEvent.objects.create(
        claim=claim,
        event_type=PatientAccountClaimEvent.EventType.SUBMITTED,
        actor=target,
        metadata={},
    )

    stored = StoredFile.objects.create(
        file=SimpleUploadedFile("medical.pdf", b"private-medical-bytes"),
        original_filename="medical.pdf",
        mime_type="application/pdf",
        size_bytes=21,
        sha256="b" * 64,
    )
    medical_storage = stored.file.storage
    medical_name = stored.file.name
    MedicalDocument.objects.create(
        patient=profile,
        uploaded_by=target,
        stored_file=stored,
        content_sha256="c" * 64,
        document_type=MedicalDocument.DocumentType.MEDICAL_REPORT,
    )
    old_audit = record_audit(
        action=AuditLog.Action.ACCOUNT_CREATED,
        actor=target,
        patient=profile,
        resource_type="USER",
        resource_uuid=target.pk,
    )

    result = purge_user_account_as_superuser(
        actor=actor,
        target=target,
        reason="USER_REQUESTED_DELETION",
        reason_detail="Synthetic fixture cleanup",
    )

    User = get_user_model()
    assert result.status == "SUCCESS"
    target.refresh_from_db()
    profile.refresh_from_db()
    assert target.is_active is False
    assert target.status == User.Status.DISABLED
    assert target.email.startswith("purged+")
    assert target.phone == ""
    assert not target.has_usable_password()
    assert profile.full_name == "Purged account"
    assert profile.digital_id.startswith("P")
    assert not MedicalDocument.objects.filter(pk__isnull=False).exists()
    assert not StoredFile.objects.filter(pk=stored.pk).exists()
    assert not medical_storage.exists(medical_name)
    identity_file.refresh_from_db()
    assert identity_file.file.name == ""
    assert identity_file.original_name == ""
    assert not identity_storage.exists(identity_name)

    identity.refresh_from_db()
    identity_event.refresh_from_db()
    assert identity.patient_id == profile.pk
    assert identity.document_number == ""
    assert identity.front_image_id == identity_file.pk
    assert identity_event.actor_id == target.pk

    relationship.refresh_from_db()
    relationship_event.refresh_from_db()
    assert relationship.guardian_user_id == target.pk
    assert relationship.minor_patient_id == minor.pk
    assert relationship.active is False
    assert relationship_event.actor_id == target.pk

    claim.refresh_from_db()
    claim_event.refresh_from_db()
    assert claim.patient_id == profile.pk
    assert claim.status == PatientAccountClaim.Status.CANCELLED
    assert claim.requested_email == ""
    assert claim_event.actor_id == target.pk

    old_audit.refresh_from_db()
    assert old_audit.actor_id == target.pk
    assert old_audit.patient_id == profile.pk
    actions = AuditLog.objects.filter(resource_uuid=target.pk).values_list(
        "action", flat=True
    )
    assert AuditLog.SUPERUSER_ACCOUNT_PURGE_REQUESTED in actions
    assert AuditLog.SUPERUSER_ACCOUNT_PURGE_COMPLETED in actions


@pytest.mark.django_db
def test_admin_removes_generic_delete_and_hides_purge_from_staff(rf):
    model_admin = CustomUserAdmin(get_user_model(), admin.site)
    staff = UserFactory(status="ACTIVE")
    staff.is_staff = True
    staff.save(update_fields=("is_staff",))
    request = rf.get("/admin/accounts/user/")
    request.user = staff
    assert "delete_selected" not in model_admin.get_actions(request)
    assert "system_purge_selected_users" not in model_admin.get_actions(request)
    assert not model_admin.has_delete_permission(request)


@pytest.mark.django_db
def test_admin_system_purge_get_never_mutates(client):
    actor = make_superuser()
    target = UserFactory(status="ACTIVE")
    client.force_login(actor)
    response = client.get(f"/admin/accounts/user/{target.pk}/system-purge/")
    assert response.status_code == 200
    assert get_user_model().objects.filter(pk=target.pk).exists()
    assert b"I understand this cannot be undone" in response.content


@pytest.mark.django_db
def test_admin_system_purge_post_requires_reason_and_confirmation(client):
    actor = make_superuser()
    target = UserFactory(status="ACTIVE")
    client.force_login(actor)
    path = f"/admin/accounts/user/{target.pk}/system-purge/"

    response = client.post(path, {"reason": "TEST_ACCOUNT_CLEANUP"})
    assert response.status_code == 200
    assert get_user_model().objects.filter(pk=target.pk).exists()

    response = client.post(
        path,
        {"reason": "TEST_ACCOUNT_CLEANUP", "confirm": "on"},
    )
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.is_active is False
    assert target.email.startswith("purged+")


@pytest.mark.django_db
def test_admin_system_purge_url_denies_ordinary_staff(client):
    staff = UserFactory(status="ACTIVE")
    staff.is_staff = True
    staff.save(update_fields=("is_staff",))
    target = UserFactory(status="ACTIVE")
    client.force_login(staff)

    response = client.get(f"/admin/accounts/user/{target.pk}/system-purge/")
    assert response.status_code == 403
    assert get_user_model().objects.filter(pk=target.pk).exists()


@pytest.mark.django_db
def test_admin_system_purge_requires_csrf():
    from django.test import Client

    actor = make_superuser()
    target = UserFactory(status="ACTIVE")
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(actor)
    response = csrf_client.post(
        f"/admin/accounts/user/{target.pk}/system-purge/",
        {"reason": "TEST_ACCOUNT_CLEANUP", "confirm": "on"},
    )
    assert response.status_code == 403
    assert get_user_model().objects.filter(pk=target.pk).exists()
