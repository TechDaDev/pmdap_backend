from datetime import date

import pytest
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from tests.test_minor_medical_documents_api import (
    collection,
    minor,
    payload,
    relationship,
    verified_guardian,
)
from tests.test_pdf_text_api import persist_text

pytestmark = pytest.mark.django_db


def test_text_availability_reuses_exact_guardian_authority_wall(api_client, tmp_path):
    guardian = verified_guardian(
        email="text-guardian@example.com", digital_id="10000000000000901"
    )
    patient = minor(digital_id="30000000000000901")
    relationship(guardian, patient)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=guardian)
        created = api_client.post(collection(patient), payload(), format="multipart")
        document = patient.medical_documents.get()
        persist_text(document)
        detail_url = f"{collection(patient)}{created.data['data']['uuid']}/"
        allowed = api_client.get(detail_url)

        pending = verified_guardian(
            email="text-pending@example.com", digital_id="10000000000000902"
        )
        relationship(pending, patient, status="PENDING", active=False, kind="MOTHER")
        api_client.force_authenticate(user=pending)
        pending_denied = api_client.get(detail_url)

        rejected = verified_guardian(
            email="text-rejected@example.com", digital_id="10000000000000903"
        )
        relationship(
            rejected,
            patient,
            status="REJECTED",
            active=False,
            kind="LEGAL_GUARDIAN",
        )
        api_client.force_authenticate(user=rejected)
        rejected_denied = api_client.get(detail_url)

        unrelated = verified_guardian(
            email="text-unrelated@example.com", digital_id="10000000000000904"
        )
        api_client.force_authenticate(user=unrelated)
        unrelated_denied = api_client.get(detail_url)

        agent = User.objects.create_user(
            email="text-agent@example.com",
            password="A-complex-password-2026!",
            role=User.Role.IDENTITY_VERIFICATION_AGENT,
            status=User.Status.ACTIVE,
        )
        api_client.force_authenticate(user=agent)
        agent_denied = api_client.get(detail_url)

        api_client.force_authenticate(user=None)
        anonymous_denied = api_client.get(detail_url)

        today = timezone.localdate()
        patient.date_of_birth = date(today.year - 18, today.month, today.day)
        patient.save(update_fields=("date_of_birth", "updated_at"))
        api_client.force_authenticate(user=guardian)
        ageout_denied = api_client.get(detail_url)

    assert allowed.status_code == 200
    assert allowed.data["data"]["text_available"] is True
    for response in (
        pending_denied,
        rejected_denied,
        unrelated_denied,
        ageout_denied,
    ):
        assert response.status_code == 404
    assert agent_denied.status_code == 404
    assert anonymous_denied.status_code == 401
