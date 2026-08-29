"""Synthetic fixture for PMDAP Operations Android end-to-end acceptance.

Run only against an isolated disposable database. No real PII.
"""

import io
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from guardians.models import GuardianRelationship
from identities.models import IdentityDocument
from identities.services import (
    approve_identity_document,
    persist_identity_upload,
    submit_identity_document,
)
from patients.services import create_patient_profile

REVIEWER_EMAIL = "m30-reviewer@example.test"
REVIEWER_PASSWORD = "M30-Synthetic-Only!"
PATIENT_EMAIL = "m30-patient@example.test"
PATIENT_PASSWORD = "M30-Patient-Synthetic!"


def _image(name: str, *, image_format: str = "PNG"):
    raw = io.BytesIO()
    Image.new("RGB", (8, 8), (65, 100, 140)).save(raw, format=image_format)
    content_type = "image/png" if image_format == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, raw.getvalue(), content_type=content_type)


def _identity_document(*, tag: str, given_name: str):
    owner = User.objects.create_user(
        email=f"m30-{tag}-patient@example.test",
        password=None,
        role=User.Role.PATIENT,
        status=User.Status.ACTIVE,
    )
    profile = create_patient_profile(
        user=owner,
        full_name=f"{given_name} Ali Hassan",
        given_name=given_name,
        father_name="Ali",
        grandfather_name="Hassan",
        mother_name="Fatima",
        date_of_birth="1990-05-20",
        sex="MALE",
        nationality="IQ",
        blood_group="O+",
    )
    return IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number=f"M30-DOC-{tag.upper()}",
        national_number=f"M30-NAT-{tag.upper()}",
        family_number=f"M30-FAM-{tag.upper()}",
        unique_card_body_number=f"M30-BODY-{tag.upper()}",
        issue_date="2024-01-02",
        expiry_date="2034-01-01",
        issuing_country="IQ",
        front_image=persist_identity_upload(
            _image(f"m30-{tag}-front.jpg", image_format="JPEG")
        ),
        back_image=persist_identity_upload(
            _image(f"m30-{tag}-back.jpg", image_format="JPEG")
        ),
        verification_status=IdentityDocument.VerificationStatus.PENDING,
    )


def _verified_guardian(*, tag: str, reviewer):
    guardian = User.objects.create_user(
        email=f"m30-{tag}-guardian@example.test",
        password=None,
        role=User.Role.PATIENT,
        status=User.Status.ACTIVE,
    )
    profile = create_patient_profile(
        user=guardian,
        full_name=f"Layla {tag.title()}",
        given_name="Layla",
        date_of_birth="1988-01-15",
        sex="FEMALE",
        nationality="IQ",
        blood_group="A+",
    )
    document = submit_identity_document(
        patient=profile,
        actor=guardian,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": f"M30-ADULT-CARD-{tag.upper()}",
            "national_number": f"M30-ADULT-NAT-{tag.upper()}",
            "family_number": "FAM-100",
            "issuing_country": "IQ",
            "issue_date": date(2022, 1, 1),
            "expiry_date": date(2032, 1, 1),
            "front_image": _image(f"m30-{tag}-adult-front.png"),
            "back_image": _image(f"m30-{tag}-adult-back.png"),
        },
    )
    approve_identity_document(document=document, agent=reviewer)
    return guardian


def _guardian_fixture(*, tag: str, reviewer, ready: bool):
    guardian = _verified_guardian(tag=tag, reviewer=reviewer)
    client = APIClient()
    access = str(RefreshToken.for_user(guardian).access_token)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    response = client.post(
        "/api/v1/minors/",
        {
            "full_name": f"Synthetic Minor {tag.title()}",
            "date_of_birth": "2015-05-10",
            "sex": "FEMALE",
            "nationality": "IQ",
            "blood_group": "O+",
            "relationship": "MOTHER",
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": f"M30-CHILD-CARD-{tag.upper()}",
            "national_number": f"M30-CHILD-NAT-{tag.upper()}",
            "issuing_country": "IQ",
            "issue_date": "2022-01-01",
            "expiry_date": "2032-01-01",
            "front_image": _image(f"m30-{tag}-child-front.png"),
            "back_image": _image(f"m30-{tag}-child-back.png"),
        },
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=f"m30-{tag}-minor",
    )
    if response.status_code != 201:
        raise RuntimeError(f"Could not seed {tag} minor: {response.status_code}")
    relationship = GuardianRelationship.objects.get(guardian_user=guardian)
    if ready:
        minor = relationship.minor_patient
        document = IdentityDocument.objects.get(patient=minor)
        document.family_number = "FAM-100"
        document.save(update_fields=("family_number", "updated_at"))
        approve_identity_document(document=document, agent=reviewer)
        minor.mother_name = "Layla"
        minor.save(update_fields=("mother_name", "updated_at"))
    return relationship


def seed():
    """Create deterministic synthetic records in a fresh disposable database."""

    database_name = str(connection.settings_dict["NAME"])
    if not database_name.startswith("pmdap_m30_e2e_"):
        raise RuntimeError("M30 E2E fixture requires a pmdap_m30_e2e_* database")

    if User.objects.filter(email__startswith="m30-").exists():
        raise RuntimeError("M30 E2E fixture already exists; use a fresh database")

    reviewer = User.objects.create_user(
        email=REVIEWER_EMAIL,
        password=REVIEWER_PASSWORD,
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    User.objects.create_user(
        email=PATIENT_EMAIL,
        password=PATIENT_PASSWORD,
        role=User.Role.PATIENT,
        status=User.Status.ACTIVE,
    )

    _identity_document(tag="approve", given_name="SyntheticApprove")

    _identity_document(tag="reject", given_name="SyntheticReject")

    _guardian_fixture(
        tag="approve",
        reviewer=reviewer,
        ready=True,
    )
    _guardian_fixture(
        tag="ineligible",
        reviewer=reviewer,
        ready=False,
    )
    _guardian_fixture(
        tag="reject",
        reviewer=reviewer,
        ready=True,
    )

    return {
        "reviewer_email": REVIEWER_EMAIL,
        "patient_email": PATIENT_EMAIL,
        "identity_targets": ["SyntheticApprove", "SyntheticReject"],
        "guardian_targets": ["Approve", "Ineligible", "Reject"],
    }
