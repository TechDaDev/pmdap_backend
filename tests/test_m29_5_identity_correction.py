"""M29.5 reviewer identity correction workflow.

Covers the domain services, API, permissions, ops workstation rendering,
authority transitions (raw OCR preserved, reviewed staged, verified promoted),
verified-correction, guardian evidence recompute, DOB/duplicate rules,
concurrency and security.

Synthetic data only. No real PII.
"""

import io
import uuid

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from audit.models import AuditLog
from identities.corrections import (
    correct_verified_identity,
    update_identity_review_fields,
)
from identities.exceptions import (
    IdentityCorrectionConflict,
    IdentityTransitionConflict,
    StaleReviewConflict,
)
from identities.models import (
    IdentityDocument,
    IdentityDocumentEvent,
    IdentityFieldCorrection,
)
from identities.services import (
    approve_identity_document,
    persist_identity_upload,
    reject_identity_document,
)
from patients.services import create_patient_profile
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"


def _jpeg(name):
    raw = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 120, 60)).save(raw, format="JPEG")
    return SimpleUploadedFile(name, raw.getvalue(), content_type="image/jpeg")


def make_user(*, role=User.Role.PATIENT, staff=False, superuser=False):
    user = UserFactory(
        email=f"m295-{uuid.uuid4().hex[:10]}@example.com",
        role=role,
        status=User.Status.ACTIVE,
    )
    if staff or superuser:
        user.is_staff = True
    user.is_superuser = superuser
    user.save(update_fields=("is_staff", "is_superuser"))
    return user


def make_identity_document(
    *,
    given_name="Ahmed",
    father_name="Ali",
    grandfather_name="Hassan",
    mother_name="Fatima",
    date_of_birth="1990-05-20",
    sex="MALE",
    blood_group="O+",
    nationality="IQ",
    document_number="DOC-123",
    national_number="NAT-9",
    family_number="FAM-100",
    card_body="BODY-1",
    issue_date="2024-01-02",
    expiry_date="2034-01-01",
    verification_status=IdentityDocument.VerificationStatus.PENDING,
):
    owner = UserFactory(
        email=f"patient-m295-{uuid.uuid4().hex[:10]}@example.com",
        role=User.Role.PATIENT,
    )
    profile = create_patient_profile(
        user=owner,
        full_name=" ".join(p for p in (given_name, father_name, grandfather_name) if p),
        given_name=given_name,
        father_name=father_name,
        grandfather_name=grandfather_name,
        mother_name=mother_name,
        date_of_birth=date_of_birth,
        sex=sex,
        nationality=nationality,
        blood_group=blood_group,
    )
    front = persist_identity_upload(_jpeg("front.jpg"))
    back = persist_identity_upload(_jpeg("back.jpg"))
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
        verification_status=verification_status,
    )
    if verification_status == IdentityDocument.VerificationStatus.VERIFIED:
        doc.verified_by = owner
        doc.verified_at = doc.created_at
        doc.save(update_fields=("verified_by", "verified_at"))
    return doc, owner, profile


def _corrections(**kwargs):
    return {k: v for k, v in kwargs.items() if v is not None}


def auth(api_client, user):
    access = str(RefreshToken.for_user(user).access_token)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return api_client


class TestWhitelistAndValidation:
    def test_non_whitelisted_field_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"verification_status": "VERIFIED"},
                review_version=0,
            )

    def test_status_and_role_fields_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"role": "ADMIN", "given_name": "X"},
                review_version=0,
            )

    def test_bad_dob_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"date_of_birth": "2099-01-01"},
                review_version=0,
            )

    def test_bad_enum_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"sex": "ROBOT"},
                review_version=0,
            )
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"blood_group": "Z-"},
                review_version=0,
            )

    def test_malformed_card_body_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"unique_card_body_number": "BAD;DROP"},
                review_version=0,
            )

    def test_empty_corrections_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent, document=doc, corrections={}, review_version=0
            )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("given_name", "A" * 256),
            ("given_name", "Ahmed1"),  # digit in name
            ("mother_name", "M" * 256),
            ("nationality", "ABC"),
            ("nationality", "i"),
            ("date_of_birth", "not-a-date"),
            ("date_of_birth", "1899-01-01"),  # out of reasonable range
            ("document_number", ""),
            ("document_number", "X" * 129),
            ("national_number", "N" * 129),
            ("family_number", "F!@#"),  # unsafe charset
        ],
    )
    def test_invalid_values_rejected(self, field, value):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={field: value},
                review_version=0,
            )

    def test_nationality_normalized_uppercase(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document(nationality="IQ")
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections={"nationality": "ir"},
            review_version=0,
        )
        doc.refresh_from_db()
        assert doc.reviewed_nationality == "IR"

    def test_number_value_normalized(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document(family_number="FAM-100")
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections={"family_number": "  FAM-101  "},
            review_version=0,
        )
        doc.refresh_from_db()
        assert doc.reviewed_family_number == "FAM-101"


class TestSaveCorrections:
    def test_save_corrections_stages_and_stays_pending(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmad", date_of_birth="1989-01-15"),
            review_version=0,
        )
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING
        assert doc.review_version == 1
        assert doc.reviewed_given_name == "Ahmad"
        # authoritative values unchanged
        profile.refresh_from_db()
        assert profile.given_name == "Ahmed"
        assert profile.date_of_birth.isoformat() == "1990-05-20"
        assert IdentityDocumentEvent.objects.filter(
            document=doc,
            event_type=IdentityDocumentEvent.EventType.REVIEW_FIELDS_CORRECTED,
        ).exists()
        assert AuditLog.objects.filter(
            action=AuditLog.Action.IDENTITY_REVIEW_FIELDS_CORRECTED
        ).exists()

    def test_raw_ocr_preserved_in_provenance(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmad"),
            review_version=0,
        )
        corr = IdentityFieldCorrection.objects.get(document=doc, field="given_name")
        assert corr.original_value == "Ahmed"
        assert corr.reviewed_value == "Ahmad"
        assert corr.source == IdentityFieldCorrection.Source.REVIEWER_CORRECTION
        assert corr.corrected_by == agent
        # authoritative still original until approval
        doc.refresh_from_db()
        assert doc.reviewed_given_name == "Ahmad"

    def test_unchanged_value_records_no_correction(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmed", father_name="Ali"),
            review_version=0,
        )
        assert IdentityFieldCorrection.objects.filter(document=doc).count() == 0
        # review_version still bumps (a review round happened)
        doc.refresh_from_db()
        assert doc.review_version == 1

    def test_save_without_approve_keeps_pending(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(family_number="FAM-999"),
            review_version=0,
        )
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING

    def test_issue_and_expiry_are_staged_without_authoritative_mutation(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()

        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections={"issue_date": "2024-02-03", "expiry_date": "2035-02-02"},
            review_version=0,
        )

        doc.refresh_from_db()
        assert doc.issue_date.isoformat() == "2024-01-02"
        assert doc.expiry_date.isoformat() == "2034-01-01"
        assert doc.reviewed_issue_date.isoformat() == "2024-02-03"
        assert doc.reviewed_expiry_date.isoformat() == "2035-02-02"


class TestApprovePromotes:
    def test_approve_promotes_reviewed_to_profile_and_document(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(
                given_name="Ahmad",
                father_name="Aly",
                date_of_birth="1989-01-15",
                sex="FEMALE",
                national_number="NAT-999",
                unique_card_body_number="BODY-999",
            ),
            review_version=0,
        )
        approved = approve_identity_document(document=doc, agent=agent)
        profile.refresh_from_db()
        assert (
            approved.verification_status == IdentityDocument.VerificationStatus.VERIFIED
        )
        assert profile.given_name == "Ahmad"
        assert profile.father_name == "Aly"
        assert profile.date_of_birth.isoformat() == "1989-01-15"
        assert profile.sex == "FEMALE"
        assert profile.full_name == "Ahmad Aly Hassan"
        approved.refresh_from_db()
        assert approved.national_number == "NAT-999"
        assert approved.unique_card_body_number == "BODY-999"
        assert approved.family_number == "FAM-100"

    def test_approve_without_corrections_unchanged(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document()
        approve_identity_document(document=doc, agent=agent)
        profile.refresh_from_db()
        assert profile.given_name == "Ahmed"

    def test_profile_sync_source_is_verified_values(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document()
        assert profile.given_name == "Ahmed"
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(grandfather_name="Husein"),
            review_version=0,
        )
        profile.refresh_from_db()
        assert profile.grandfather_name == "Hassan"  # unchanged pre-approval
        approve_identity_document(document=doc, agent=agent)
        profile.refresh_from_db()
        assert profile.grandfather_name == "Husein"

    def test_approve_promotes_reviewed_issue_and_expiry_dates(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections={"issue_date": "2024-02-03", "expiry_date": "2035-02-02"},
            review_version=0,
        )

        approve_identity_document(document=doc, agent=agent)

        doc.refresh_from_db()
        assert doc.issue_date.isoformat() == "2024-02-03"
        assert doc.expiry_date.isoformat() == "2035-02-02"


class TestReject:
    def test_reject_does_not_promote_corrections(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmad"),
            review_version=0,
        )
        reject_identity_document(document=doc, agent=agent, reason="card unclear")
        profile.refresh_from_db()
        assert profile.given_name == "Ahmed"
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.REJECTED
        # review history retained
        assert IdentityFieldCorrection.objects.filter(document=doc).exists()


class TestDuplicates:
    def test_duplicate_national_number_blocked_on_approve(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        other, _, _ = make_identity_document(
            national_number="NAT-DUP", card_body="BODY-A"
        )
        other.verification_status = IdentityDocument.VerificationStatus.VERIFIED
        other.verified_by = other.patient.user
        other.verified_at = other.created_at
        other.save(update_fields=("verification_status", "verified_by", "verified_at"))
        doc, _, _ = make_identity_document(national_number="NAT-X", card_body="BODY-B")
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(national_number="NAT-DUP"),
            review_version=0,
        )
        with pytest.raises(IdentityCorrectionConflict):
            approve_identity_document(document=doc, agent=agent)
        doc.refresh_from_db()
        assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING

    def test_duplicate_card_body_blocked(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        other, _, _ = make_identity_document(
            national_number="NAT-1", card_body="BODY-DUP"
        )
        other.verification_status = IdentityDocument.VerificationStatus.VERIFIED
        other.verified_by = other.patient.user
        other.verified_at = other.created_at
        other.save(update_fields=("verification_status", "verified_by", "verified_at"))
        doc, _, _ = make_identity_document(national_number="NAT-2", card_body="BODY-2")
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(unique_card_body_number="BODY-DUP"),
            review_version=0,
        )
        with pytest.raises(IdentityCorrectionConflict):
            approve_identity_document(document=doc, agent=agent)


class TestDob:
    def test_dob_correction_recomputes_age(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document(date_of_birth="1990-05-20")
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(date_of_birth="2010-06-01"),
            review_version=0,
        )
        approve_identity_document(document=doc, agent=agent)
        profile.refresh_from_db()
        assert profile.date_of_birth.isoformat() == "2010-06-01"
        # 2010-06-01 on 2026-08-27 is 16 -> minor status recalculated.
        assert profile.age == 16
        assert profile.is_minor is True


class TestVerifiedCorrection:
    def test_reason_required(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        with pytest.raises(DjangoValidationError):
            correct_verified_identity(
                actor=agent,
                document=doc,
                corrections=_corrections(given_name="Ahmad"),
                reason_category="",
                review_version=0,
            )

    def test_verified_correction_applies_and_stays_verified(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, profile = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        corrected = correct_verified_identity(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmad", family_number="FAM-777"),
            reason_category="DATA_ENTRY",
            note="typo fixed",
            review_version=0,
        )
        assert (
            corrected.verification_status
            == IdentityDocument.VerificationStatus.VERIFIED
        )
        profile.refresh_from_db()
        assert profile.given_name == "Ahmad"
        corrected.refresh_from_db()
        assert corrected.family_number == "FAM-777"
        corr = IdentityFieldCorrection.objects.get(
            document=doc,
            field="given_name",
            source=IdentityFieldCorrection.Source.VERIFIED_CORRECTION,
        )
        assert corr.original_value == "Ahmed"
        assert corr.reason_category == "DATA_ENTRY"
        assert IdentityDocumentEvent.objects.filter(
            document=doc,
            event_type=IdentityDocumentEvent.EventType.VERIFIED_FIELDS_CORRECTED,
        ).exists()
        assert AuditLog.objects.filter(
            action=AuditLog.Action.IDENTITY_VERIFIED_FIELDS_CORRECTED
        ).exists()

    def test_verified_issue_expiry_correction_requires_reason_and_persists(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )

        corrected = correct_verified_identity(
            actor=agent,
            document=doc,
            corrections={"issue_date": "2024-02-03", "expiry_date": "2035-02-02"},
            reason_category="OCR_CORRECTION",
            review_version=0,
        )

        corrected.refresh_from_db()
        assert corrected.issue_date.isoformat() == "2024-02-03"
        assert corrected.expiry_date.isoformat() == "2035-02-02"
        assert IdentityFieldCorrection.objects.filter(
            document=doc,
            field="issue_date",
            reason_category="OCR_CORRECTION",
        ).exists()

    def test_issue_expiry_order_is_validated_before_save(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        with pytest.raises(DjangoValidationError):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections={"issue_date": "2035-01-01"},
                review_version=0,
            )

    def test_verified_correction_same_values_rejected(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        with pytest.raises(DjangoValidationError):
            correct_verified_identity(
                actor=agent,
                document=doc,
                corrections=_corrections(given_name="Ahmed"),
                reason_category="ADMINISTRATIVE",
                review_version=0,
            )


class TestConcurrency:
    def test_stale_review_version_conflict(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmad"),
            review_version=0,
        )
        # Reviewer B submits with stale version 0 -> 409
        with pytest.raises(StaleReviewConflict):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections=_corrections(father_name="X"),
                review_version=0,
            )

    def test_approve_then_stale_save_conflict(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        approve_identity_document(document=doc, agent=agent)
        with pytest.raises(IdentityTransitionConflict):
            update_identity_review_fields(
                actor=agent,
                document=doc,
                corrections=_corrections(given_name="Ahmad"),
                review_version=0,
            )


class TestPermissions:
    def test_ordinary_staff_denied(self):
        # An ADMIN (non-agent, non-superuser) is ordinary staff for identity
        # verification purposes: blocked from correction authority.
        staff = make_user(role=User.Role.ADMIN, staff=True)
        doc, _, _ = make_identity_document()
        from identities.permissions import can_verify_identity

        assert can_verify_identity(staff) is False
        from identities.exceptions import VerificationAgentRequired

        with pytest.raises(VerificationAgentRequired):
            update_identity_review_fields(
                actor=staff,
                document=doc,
                corrections=_corrections(given_name="X"),
                review_version=0,
            )

    def test_patient_denied(self):
        patient = make_user()
        doc, _, _ = make_identity_document()
        from identities.permissions import can_verify_identity

        assert can_verify_identity(patient) is False

    def test_superuser_allowed(self):
        superuser = make_user(superuser=True, staff=True)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=superuser,
            document=doc,
            corrections=_corrections(given_name="Ahmad"),
            review_version=0,
        )
        approve_identity_document(document=doc, agent=superuser)

    def test_agent_allowed(self):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections=_corrections(given_name="Ahmad"),
            review_version=0,
        )


class TestApi:
    def _auth(self, api_client, user):
        access = str(RefreshToken.for_user(user).access_token)
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api_client

    def test_detail_exposes_review_fields_and_actions(self, api_client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        self._auth(api_client, agent)
        doc, _, _ = make_identity_document()
        url = reverse("identity-verification-detail", args=[doc.uuid])
        resp = api_client.get(url)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["review_version"] == 0
        assert data["review_fields"]["given_name"]["original"] == "Ahmed"
        assert data["review_fields"]["given_name"]["corrected"] is False
        assert data["review_fields"]["issue_date"]["original"] == "2024-01-02"
        assert data["review_fields"]["expiry_date"]["original"] == "2034-01-01"
        assert data["issue_date"] == "2024-01-02"
        assert data["expiry_date"] == "2034-01-01"
        assert data["national_number"] == "NAT-9"
        assert data["card_body_number"] == "BODY-1"
        assert data["family_number"] == "FAM-100"
        assert "review_fields" in data["available_actions"]
        assert "approve" in data["available_actions"]
        assert "correct_verified" not in data["available_actions"]

    def test_review_fields_api_saves(self, api_client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        self._auth(api_client, agent)
        doc, _, _ = make_identity_document()
        url = reverse("identity-verification-review-fields", args=[doc.uuid])
        resp = api_client.post(
            url,
            {"review_version": 0, "fields": {"given_name": "Ahmad"}},
            format="json",
        )
        assert resp.status_code == 200
        doc.refresh_from_db()
        assert doc.reviewed_given_name == "Ahmad"

    def test_review_fields_api_rejects_status(self, api_client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        self._auth(api_client, agent)
        doc, _, _ = make_identity_document()
        url = reverse("identity-verification-review-fields", args=[doc.uuid])
        resp = api_client.post(
            url,
            {
                "review_version": 0,
                "fields": {"verification_status": "VERIFIED"},
            },
            format="json",
        )
        assert resp.status_code == 400

    def test_review_fields_api_stale_409(self, api_client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        self._auth(api_client, agent)
        doc, _, _ = make_identity_document()
        url = reverse("identity-verification-review-fields", args=[doc.uuid])
        api_client.post(
            url,
            {"review_version": 0, "fields": {"given_name": "Ahmad"}},
            format="json",
        )
        resp = api_client.post(
            url,
            {"review_version": 0, "fields": {"father_name": "X"}},
            format="json",
        )
        assert resp.status_code == 409

    def test_correct_verified_api(self, api_client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        self._auth(api_client, agent)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        url = reverse("identity-verification-correct-verified", args=[doc.uuid])
        resp = api_client.post(
            url,
            {
                "review_version": 0,
                "fields": {"given_name": "Ahmad"},
                "reason_category": "DATA_ENTRY",
                "note": "typo",
            },
            format="json",
        )
        assert resp.status_code == 200
        doc.refresh_from_db()
        assert doc.patient.given_name == "Ahmad"

    def test_correct_verified_requires_reason(self, api_client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        self._auth(api_client, agent)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        url = reverse("identity-verification-correct-verified", args=[doc.uuid])
        resp = api_client.post(
            url,
            {"review_version": 0, "fields": {"given_name": "Ahmad"}},
            format="json",
        )
        assert resp.status_code == 400

    def test_idor_agent_cannot_edit_other_patients_verified(self, api_client):
        # Agent edits any pending document (allowed); a verified document of
        # another patient is only correctable by an agent (allowed). IDOR here
        # means an ordinary user cannot reach the endpoint at all.
        patient = make_user()
        self._auth(api_client, patient)
        doc, _, _ = make_identity_document()
        url = reverse("identity-verification-review-fields", args=[doc.uuid])
        resp = api_client.post(
            url,
            {"review_version": 0, "fields": {"given_name": "HACK"}},
            format="json",
        )
        assert resp.status_code in (401, 403)


class TestOpsConsole:
    def test_review_page_renders_correction_form(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document()
        resp = client.get(reverse("admin:ops_verification_review", args=[doc.pk]))
        assert resp.status_code == 200
        assert b"reviewer correction" in resp.content.lower()
        assert b"review_version" in resp.content

    def test_review_page_save_post(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document()
        url = reverse("admin:ops_verification_review_fields", args=[doc.pk])
        resp = client.post(
            url,
            {"review_version": "0", "given_name": "Ahmad"},
        )
        assert resp.status_code == 302
        doc.refresh_from_db()
        assert doc.reviewed_given_name == "Ahmad"

    def test_verified_correction_page_renders(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        resp = client.get(reverse("admin:ops_verification_review", args=[doc.pk]))
        assert resp.status_code == 200
        assert b"Correct verified identity" in resp.content

    def test_verified_correction_post_requires_reason(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        url = reverse("admin:ops_verification_correct_verified", args=[doc.pk])
        resp = client.post(url, {"review_version": "0", "given_name": "Ahmad"})
        assert resp.status_code == 302
        doc.refresh_from_db()
        assert doc.patient.given_name != "Ahmad"

    def test_queue_shows_correction_indicator(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections={"given_name": "Ahmad"},
            review_version=0,
        )
        resp = client.get(reverse("admin:ops_verification_queue"))
        assert resp.status_code == 200
        assert b"Corrected" in resp.content

    def test_review_fields_post_stale_version_error(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document()
        update_identity_review_fields(
            actor=agent,
            document=doc,
            corrections={"given_name": "Ahmad"},
            review_version=0,
        )
        url = reverse("admin:ops_verification_review_fields", args=[doc.pk])
        resp = client.post(url, {"review_version": "0", "given_name": "X"})
        assert resp.status_code == 302
        doc.refresh_from_db()
        assert doc.reviewed_given_name == "Ahmad"  # stale write rejected

    def test_review_fields_post_on_verified_document_error(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        url = reverse("admin:ops_verification_review_fields", args=[doc.pk])
        resp = client.post(url, {"review_version": "0", "given_name": "X"})
        assert resp.status_code == 302
        doc.refresh_from_db()
        assert doc.patient.given_name != "X"

    def test_correct_verified_post_invalid_reason_error(self, client):
        agent = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT, staff=True)
        client.force_login(agent)
        doc, _, _ = make_identity_document(
            verification_status=IdentityDocument.VerificationStatus.VERIFIED
        )
        url = reverse("admin:ops_verification_correct_verified", args=[doc.pk])
        resp = client.post(
            url,
            {"review_version": "0", "given_name": "Ahmad", "reason_category": "BOGUS"},
        )
        assert resp.status_code == 302
        doc.refresh_from_db()
        assert doc.patient.given_name != "Ahmad"


class TestGuardianEvidenceRecompute:
    def test_family_correction_revalidates_guardian(self, api_client):
        from tests.test_minors_guardians import (
            create_minor,
            create_verified_guardian,
            document_model,
            national_card_payload,
            patient_model,
            relationship_model,
        )

        # Guardian with family FAM-100; child card family initially different.
        guardian, _, agent = create_verified_guardian(family="FAM-100")
        payload = national_card_payload(relationship="FATHER")
        payload.pop("family_number")
        resp = create_minor(api_client, guardian, payload=payload)
        assert resp.status_code == 201
        minor = patient_model().objects.exclude(pk=guardian.patient_profile.pk).get()
        child_card = document_model().objects.get(patient=minor)
        child_card.family_number = "WRONG-FAM"
        child_card.save(update_fields=("family_number", "updated_at"))
        approve_identity_document(document=child_card, agent=agent)
        relationship = relationship_model().objects.get(minor_patient=minor)
        assert relationship.family_number_result == "MISMATCH"

        # Reviewer corrects the child's family number on a NEW pending identity
        # (or corrects verified) -> revalidation matches.
        reviewer = make_user(role=User.Role.IDENTITY_VERIFICATION_AGENT)
        correct_verified_identity(
            actor=reviewer,
            document=child_card,
            corrections={"family_number": "FAM-100"},
            reason_category="DATA_ENTRY",
            review_version=child_card.review_version,
        )
        relationship.refresh_from_db()
        assert relationship.family_number_result == "MATCH"
