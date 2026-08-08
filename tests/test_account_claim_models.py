from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from claims.models import (
    AccountActivation,
    PatientAccountClaim,
    PatientAccountClaimEvent,
)
from patients.models import PatientProfile
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def patient(*, digital_id="12345678901234567"):
    return PatientProfile.objects.create(
        digital_id=digital_id,
        full_name="Adult Patient",
        date_of_birth=date.today() - timedelta(days=30 * 365),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
        identity_status=PatientProfile.IdentityStatus.VERIFIED,
    )


def claim(profile, *, email="claim@example.com"):
    return PatientAccountClaim.objects.create(
        patient=profile,
        requested_email=email,
        requested_phone="+9647701234567",
        submitted_name=profile.full_name,
        submitted_date_of_birth=profile.date_of_birth,
    )


def test_claim_contract_exposes_controlled_states_and_comparisons():
    assert set(PatientAccountClaim.Status.values) == {
        "PENDING",
        "UNDER_REVIEW",
        "MORE_INFORMATION_REQUIRED",
        "APPROVED",
        "REJECTED",
        "CANCELLED",
    }
    assert set(PatientAccountClaim.Comparison.values) == {
        "MATCH",
        "MISMATCH",
        "UNAVAILABLE",
    }


def test_only_one_active_claim_exists_for_a_patient_and_email():
    first = patient()
    claim(first)
    with pytest.raises(IntegrityError), transaction.atomic():
        claim(first, email="other@example.com")

    second = patient(digital_id="22345678901234567")
    with pytest.raises(IntegrityError), transaction.atomic():
        claim(second, email="CLAIM@example.com")


def test_terminal_claim_does_not_block_new_claim():
    profile = patient()
    old = claim(profile)
    old.status = PatientAccountClaim.Status.REJECTED
    old.save(update_fields=("status", "updated_at"))
    assert claim(profile, email="claim@example.com").pk


def test_activation_stores_only_hash_and_expiry():
    user = UserFactory(status="PENDING_ACTIVATION")
    activation = AccountActivation.objects.create(
        claim=claim(patient()),
        user=user,
        token_hash="a" * 64,
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    assert activation.token_hash == "a" * 64
    assert not hasattr(activation, "token")


def test_claim_events_are_immutable():
    event = PatientAccountClaimEvent.objects.create(
        claim=claim(patient()),
        event_type=PatientAccountClaimEvent.EventType.SUBMITTED,
    )
    event.metadata = {"changed": True}
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        event.delete()
