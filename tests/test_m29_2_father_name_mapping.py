import importlib

import pytest
from django.apps import apps as django_apps

from guardians.exceptions import GuardianRelationshipConflict
from guardians.services import (
    EVIDENCE_POLICY_VERSION,
    approve_guardian_relationship,
    can_approve_guardian_relationship,
)
from identities.services import approve_identity_document
from opsconsole.guardian_views import _identity_summary
from tests.factories import UserFactory
from tests.test_m29_1_guardian_workstation import pending_parent
from tests.test_minors_guardians import (
    create_minor,
    create_verified_guardian,
    national_card_payload,
)

pytestmark = pytest.mark.django_db


def evaluated_father(
    api_client,
    *,
    adult_given="A",
    adult_full="A B C",
    adult_father="B",
    minor_father="A",
):
    guardian, minor, child_card, agent, relationship = pending_parent(api_client)
    adult = guardian.patient_profile
    adult.given_name = adult_given
    adult.full_name = adult_full
    adult.father_name = adult_father
    adult.save(update_fields=("given_name", "full_name", "father_name", "updated_at"))
    minor.father_name = minor_father
    minor.save(update_fields=("father_name", "updated_at"))
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    return adult, minor, agent, relationship


@pytest.mark.parametrize(
    ("adult_given", "adult_full", "adult_father", "minor_father", "expected"),
    (
        ("A", "A", "B", "A", "MATCH"),
        ("A", "A B C", "B", "A", "MATCH"),
        ("A", "Different Display Name", "B", "A", "MATCH"),
        ("A", "A B C", "B", "B", "MISMATCH"),
        ("", "A B C", "B", "A", "UNAVAILABLE"),
    ),
)
def test_father_evidence_uses_only_adult_given_name(
    api_client,
    adult_given,
    adult_full,
    adult_father,
    minor_father,
    expected,
):
    _, _, _, relationship = evaluated_father(
        api_client,
        adult_given=adult_given,
        adult_full=adult_full,
        adult_father=adult_father,
        minor_father=minor_father,
    )

    decision = can_approve_guardian_relationship(relationship)

    assert decision.name_result == expected
    assert decision.eligible is (expected == "MATCH")


def test_workstation_first_name_uses_structured_given_name(api_client):
    adult, _, _, relationship = evaluated_father(
        api_client, adult_given="Given Compound", adult_full="Wrong Display Value"
    )

    summary = _identity_summary(adult, relationship.guardian_identity_document)

    assert summary["first_name"] == "Given Compound"


@pytest.mark.parametrize(
    ("adult_given", "minor_father", "expected"),
    (
        ("  عبد   الإله  ", "عبد الاله", "MATCH"),
        ("ﻋﺒﺪ أﺣﻤﺪ", "عبد احمد", "MATCH"),
        ("عَبْد أحمد", "عبد احمد", "MATCH"),
        ("فاطمة", "فاطمه", "MISMATCH"),
        ("علي", "على", "MISMATCH"),
    ),
)
def test_father_name_normalization_is_conservative(
    api_client, adult_given, minor_father, expected
):
    _, _, _, relationship = evaluated_father(
        api_client,
        adult_given=adult_given,
        minor_father=minor_father,
    )

    decision = can_approve_guardian_relationship(relationship)

    assert decision.name_result == expected


def test_unverified_adult_or_minor_is_unavailable_and_blocked(api_client):
    adult, minor, agent, relationship = evaluated_father(api_client)

    adult.identity_status = "UNVERIFIED"
    adult.save(update_fields=("identity_status", "updated_at"))
    adult_decision = can_approve_guardian_relationship(relationship)
    assert adult_decision.name_result == "UNAVAILABLE"
    assert adult_decision.eligible is False

    adult.identity_status = "VERIFIED"
    adult.save(update_fields=("identity_status", "updated_at"))
    minor.identity_status = "UNVERIFIED"
    minor.save(update_fields=("identity_status", "updated_at"))
    minor_decision = can_approve_guardian_relationship(relationship)
    assert minor_decision.name_result == "UNAVAILABLE"
    assert minor_decision.eligible is False
    with pytest.raises(GuardianRelationshipConflict):
        approve_guardian_relationship(relationship=relationship, agent=agent)


def test_mother_policy_does_not_require_father_name_match(api_client):
    guardian, minor, child_card, agent, relationship = pending_parent(
        api_client, relationship_type="MOTHER"
    )
    guardian.patient_profile.given_name = "Different Adult Given"
    guardian.patient_profile.save(update_fields=("given_name", "updated_at"))
    minor.father_name = "Unrelated Father Name"
    minor.save(update_fields=("father_name", "updated_at"))
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()

    decision = can_approve_guardian_relationship(relationship)

    assert decision.name_result == "UNAVAILABLE"
    assert decision.eligible is True


def test_stale_pending_evidence_recomputes_and_persists_on_review(api_client, client):
    from django.urls import reverse

    adult, _, agent, relationship = evaluated_father(api_client)
    relationship.name_evidence_result = "MISMATCH"
    relationship.evidence_policy_version = "M27_V1"
    relationship.save(
        update_fields=(
            "name_evidence_result",
            "evidence_policy_version",
            "updated_at",
        )
    )
    agent.is_staff = True
    agent.save(update_fields=("is_staff", "updated_at"))
    client.force_login(agent)

    page = client.get(reverse("admin:ops_guardian_review", args=[relationship.pk]))

    assert page.status_code == 200
    assert b"Minor father&#x27;s name matches adult given name" in page.content
    relationship.refresh_from_db()
    assert relationship.name_evidence_result == "MATCH"
    assert relationship.evidence_policy_version == EVIDENCE_POLICY_VERSION
    assert adult.given_name == "A"


def test_client_cannot_override_father_name_evidence(api_client):
    guardian, _, _ = create_verified_guardian()
    payload = national_card_payload()
    payload["father_name_match"] = True

    response = create_minor(api_client, guardian, payload=payload)

    assert response.status_code == 400
    assert "father_name_match" in response.json()["error"]["details"]


def test_given_name_backfill_recovers_only_exact_structured_suffix():
    from patients.services import create_patient_profile

    recoverable = create_patient_profile(
        user=UserFactory(email="recoverable@example.invalid"),
        full_name="Given Compound Father Grandfather",
        father_name="Father",
        grandfather_name="Grandfather",
        date_of_birth="1980-01-01",
        sex="MALE",
        nationality="IQ",
    )
    ambiguous = create_patient_profile(
        user=UserFactory(email="ambiguous@example.invalid"),
        full_name="Display Only",
        father_name="Father",
        grandfather_name="Grandfather",
        date_of_birth="1980-01-01",
        sex="MALE",
        nationality="IQ",
    )
    migration = importlib.import_module(
        "patients.migrations.0004_patientprofile_given_name"
    )

    migration.backfill_confirmed_given_names(django_apps, None)

    recoverable.refresh_from_db()
    ambiguous.refresh_from_db()
    assert recoverable.given_name == "Given Compound"
    assert ambiguous.given_name == ""
