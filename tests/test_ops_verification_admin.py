"""Identity verification workstation admin tests.

Covers the staff-only queue, review, approve/reject mutations (through the
domain services), private image endpoints, and authorization rules:
  * anonymous                -> redirect to admin login
  * non-staff (patient)      -> redirect to admin login
  * staff without agent role -> 403 on every page (including approve/reject)
  * superuser                -> may VIEW queue/review/images AND APPROVE/REJECT
  * verification agent       -> full access
"""

import io
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from accounts.models import User
from audit.models import AuditLog
from identities.models import IdentityDocument, IdentityDocumentEvent
from identities.services import persist_identity_upload
from patients.models import PatientProfile
from patients.services import create_patient_profile
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"


def make_user(*, role=User.Role.PATIENT, staff=False, email=None):
    user = UserFactory(
        email=email or f"op-{uuid.uuid4().hex[:10]}@example.com",
        role=role,
    )
    if staff:
        user.is_staff = True
        user.save(update_fields=("is_staff",))
    return user


def make_identity_document(
    *,
    document_number="DOC-123",
    national_number="NAT-9",
    family_number="",
    card_body="BODY-123",
    issue_date="2024-01-02",
    expiry_date="2034-01-01",
    replaces=None,
    back=True,
):
    owner = UserFactory(
        email=f"patient-op-{uuid.uuid4().hex[:10]}@example.com",
        role=User.Role.PATIENT,
    )
    profile = create_patient_profile(
        user=owner,
        full_name="Operations Test Patient",
        date_of_birth="1990-05-20",
        sex="MALE",
        nationality="IQ",
        blood_group="O+",
    )

    def jpeg(name):
        raw = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 30, 30)).save(raw, format="JPEG")
        return SimpleUploadedFile(name, raw.getvalue(), content_type="image/jpeg")

    front = persist_identity_upload(jpeg("front.jpg"))
    back = persist_identity_upload(jpeg("back.jpg")) if back else None
    doc = IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number=document_number,
        national_number=national_number,
        family_number=family_number,
        unique_card_body_number=card_body,
        issue_date=issue_date,
        expiry_date=expiry_date,
        issuing_country="IQ",
        front_image=front,
        back_image=back,
        replaces=replaces,
    )
    IdentityDocumentEvent.objects.create(
        document=doc,
        event_type=IdentityDocumentEvent.EventType.UPLOADED,
        actor=owner,
    )
    return doc, owner


@pytest.fixture
def client_with(client):
    return client


def login(client, user):
    client.force_login(user)
    return client


class TestAuthz:
    def test_anonymous_redirects_to_login(self, client):
        for url in [
            reverse("admin:ops_verification_queue"),
            reverse("admin:ops_server_monitor"),
            reverse("admin:ops_server_monitor_data"),
        ]:
            response = client.get(url)
            assert response.status_code == 302
            assert "/login/" in response["Location"]

    def test_patient_non_staff_redirects_to_login(self, client):
        user = make_user()
        login(client, user)
        response = client.get(reverse("admin:ops_verification_queue"))
        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_staff_without_agent_role_forbidden(self, client):
        user = make_user(staff=True, email="staff-norole@example.com")
        login(client, user)
        assert client.get(reverse("admin:ops_verification_queue")).status_code == 403
        assert client.get(reverse("admin:ops_server_monitor")).status_code == 403
        assert client.get(reverse("admin:ops_server_monitor_data")).status_code == 403

    def test_superuser_can_view_queue(self, client):
        user = UserFactory(email="root@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        assert client.get(reverse("admin:ops_verification_queue")).status_code == 200

    def test_admin_index_shows_operations_console(self, client):
        user = UserFactory(email="root-idx@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        response = client.get(reverse("admin:index"))
        assert response.status_code == 200
        # One hierarchy: header "PMDAP Operations", content h1 "Operations
        # Console" rendered exactly once (old duplicate h1 removed).
        assert b"PMDAP Operations" in response.content
        assert b"<h1>Operations Console</h1>" in response.content
        assert b"PMDAP Operations Console" not in response.content
        assert b"Identity verification" in response.content
        assert b"Open verification queue" in response.content
        assert b"Server monitor" in response.content

    def test_agent_can_view_queue(self, client):
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_verification_queue"))
        assert response.status_code == 200


class TestQueue:
    def test_queue_shows_only_pending_current_oldest_first(self, client):
        make_identity_document()  # pending
        done = make_identity_document()[0]
        done.verification_status = IdentityDocument.VerificationStatus.VERIFIED
        done.verified_by = make_user(
            role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True
        )
        done.save(update_fields=("verification_status", "verified_by"))

        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_verification_queue"))
        assert response.status_code == 200
        # Only the pending document is listed.
        assert b"Operations Test Patient" in response.content
        assert response.content.count(b"Operations Test Patient") == 1

    def test_queue_does_not_render_document_numbers(self, client):
        make_identity_document(
            document_number="SECRET-DOCNUM-123", national_number="SECRET-NAT-456"
        )
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_verification_queue"))
        assert response.status_code == 200
        assert b"SECRET-DOCNUM-123" not in response.content
        assert b"SECRET-NAT-456" not in response.content

    def test_empty_queue(self, client):
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_verification_queue"))
        assert response.status_code == 200
        assert b"No pending identity documents" in response.content


class TestReviewAndImages:
    def test_review_shows_sensitive_fields(self, client):
        doc, _ = make_identity_document(
            document_number="DOCNUM-777", national_number="NATNUM-888"
        )
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_verification_review", args=[doc.pk]))
        assert response.status_code == 200
        assert b"DOCNUM-777" in response.content
        assert b"NATNUM-888" in response.content
        assert b"BODY-123" in response.content
        assert b"Document number</th>" not in response.content
        assert b"Issue date" in response.content
        assert b"Expiry date" in response.content
        assert b"Operations Test Patient" in response.content

    def test_agent_can_stream_front_image(self, client):
        doc, _ = make_identity_document()
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_identity_image_front", args=[doc.pk]))
        assert response.status_code == 200
        assert response["Content-Type"].startswith("image/")
        assert "no-store" in response["Cache-Control"]

    def test_staff_without_role_cannot_stream_image(self, client):
        doc, _ = make_identity_document()
        user = make_user(staff=True, email="noperm@example.com")
        login(client, user)
        response = client.get(reverse("admin:ops_identity_image_front", args=[doc.pk]))
        assert response.status_code == 403

    def test_missing_back_image_404(self, client):
        doc, _ = make_identity_document(back=False)
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_identity_image_back", args=[doc.pk]))
        assert response.status_code == 404


class TestApprove:
    def test_approve_get_renders_confirm_and_does_not_mutate(self, client):
        doc, _ = make_identity_document()
        user = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, user)
        response = client.get(reverse("admin:ops_verification_approve", args=[doc.pk]))
        assert response.status_code == 200
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING

    def test_approve_post_verifies_and_audits(self, client):
        doc, _ = make_identity_document()
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.post(reverse("admin:ops_verification_approve", args=[doc.pk]))
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.VERIFIED
        assert doc.verified_by_id == agent.pk
        assert IdentityDocumentEvent.objects.filter(
            document=doc, event_type=IdentityDocumentEvent.EventType.VERIFIED
        ).exists()
        assert AuditLog.objects.filter(
            action=AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED,
            resource_uuid=doc.uuid,
        ).exists()
        doc.patient.refresh_from_db()
        assert doc.patient.identity_status == PatientProfile.IdentityStatus.VERIFIED

    def test_approve_post_redirects_to_next_pending(self, client):
        first, _ = make_identity_document()
        second, _ = make_identity_document()
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.post(
            reverse("admin:ops_verification_approve", args=[first.pk])
        )
        assert response.status_code == 302
        assert (
            reverse("admin:ops_verification_review", args=[second.pk])
            in response["Location"]
        )

    def test_approve_replacement_marks_previous_replaced(self, client):
        previous, _ = make_identity_document(document_number="OLD-1")
        previous.verification_status = IdentityDocument.VerificationStatus.VERIFIED
        previous.save(update_fields=("verification_status",))
        replacement, _ = make_identity_document(
            document_number="NEW-1", replaces=previous
        )
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.post(
            reverse("admin:ops_verification_approve", args=[replacement.pk])
        )
        assert response.status_code == 302
        replacement.refresh_from_db()
        previous.refresh_from_db()
        assert (
            replacement.verification_status
            == IdentityDocument.VerificationStatus.VERIFIED
        )
        assert previous.status == IdentityDocument.LifecycleStatus.REPLACED

    def test_superuser_can_mutate(self, client):
        doc, _ = make_identity_document()
        root = UserFactory(email="root2@example.com")
        root.is_staff = True
        root.is_superuser = True
        root.save(update_fields=("is_staff", "is_superuser"))
        login(client, root)
        response = client.post(reverse("admin:ops_verification_approve", args=[doc.pk]))
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.VERIFIED
        assert doc.verified_by_id == root.pk
        # Audit + event actor is the real superuser (role untouched).
        assert AuditLog.objects.filter(
            action=AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED,
            resource_uuid=doc.uuid,
            actor=root,
        ).exists()
        assert IdentityDocumentEvent.objects.filter(
            document=doc,
            event_type=IdentityDocumentEvent.EventType.VERIFIED,
            actor=root,
        ).exists()
        doc.patient.refresh_from_db()
        assert doc.patient.identity_status == PatientProfile.IdentityStatus.VERIFIED
        assert root.role == User.Role.PATIENT  # role never mutated

    def test_superuser_can_reject(self, client):
        doc, _ = make_identity_document()
        root = UserFactory(email="root-reject@example.com")
        root.is_staff = True
        root.is_superuser = True
        root.save(update_fields=("is_staff", "is_superuser"))
        login(client, root)
        response = client.post(
            reverse("admin:ops_verification_reject", args=[doc.pk]),
            {"reason": "synthetic test rejection"},
        )
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.REJECTED
        assert doc.status == IdentityDocument.LifecycleStatus.REVOKED
        assert doc.rejection_reason == "synthetic test rejection"
        assert doc.verified_by_id == root.pk
        doc.patient.refresh_from_db()
        assert doc.patient.identity_status == PatientProfile.IdentityStatus.REJECTED

    def test_superuser_can_stream_images_and_review(self, client):
        doc, _ = make_identity_document()
        root = UserFactory(email="root-img@example.com")
        root.is_staff = True
        root.is_superuser = True
        root.save(update_fields=("is_staff", "is_superuser"))
        login(client, root)
        assert (
            client.get(
                reverse("admin:ops_verification_review", args=[doc.pk])
            ).status_code
            == 200
        )
        assert (
            client.get(
                reverse("admin:ops_identity_image_front", args=[doc.pk])
            ).status_code
            == 200
        )

    def test_ordinary_staff_cannot_approve(self, client):
        doc, _ = make_identity_document()
        staff = make_user(staff=True, email="staff-approve@example.com")
        login(client, staff)
        assert (
            client.post(
                reverse("admin:ops_verification_approve", args=[doc.pk])
            ).status_code
            == 403
        )
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING

    def test_ordinary_staff_cannot_reject(self, client):
        doc, _ = make_identity_document()
        staff = make_user(staff=True, email="staff-reject@example.com")
        login(client, staff)
        assert (
            client.post(
                reverse("admin:ops_verification_reject", args=[doc.pk]),
                {"reason": "nope"},
            ).status_code
            == 403
        )
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING

    def test_approve_non_pending_shows_error(self, client):
        doc, _ = make_identity_document()
        doc.verification_status = IdentityDocument.VerificationStatus.REJECTED
        doc.save(update_fields=("verification_status",))
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.post(reverse("admin:ops_verification_approve", args=[doc.pk]))
        # Service raises IdentityTransitionConflict -> redirect with error message.
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.REJECTED


class TestReject:
    def test_reject_requires_reason(self, client):
        doc, _ = make_identity_document()
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.post(reverse("admin:ops_verification_reject", args=[doc.pk]))
        assert response.status_code == 200
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING

    def test_reject_with_reason(self, client):
        doc, _ = make_identity_document()
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.post(
            reverse("admin:ops_verification_reject", args=[doc.pk]),
            {"reason": "Document appears altered"},
        )
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.REJECTED
        assert doc.status == IdentityDocument.LifecycleStatus.REVOKED
        assert doc.rejection_reason == "Document appears altered"
        assert AuditLog.objects.filter(
            action=AuditLog.Action.IDENTITY_DOCUMENT_REJECTED,
            resource_uuid=doc.uuid,
        ).exists()

    def test_reject_get_renders_form(self, client):
        doc, _ = make_identity_document()
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        login(client, agent)
        response = client.get(reverse("admin:ops_verification_reject", args=[doc.pk]))
        assert response.status_code == 200
        assert b"Reason" in response.content
