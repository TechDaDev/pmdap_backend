import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError

from accounts.models import User
from documents.exceptions import MedicalDocumentNotFound
from documents.models import MedicalDocument, MedicalDocumentEvent
from documents.services import soft_delete_medical_document, update_medical_document
from facilities.exceptions import HealthcareFacilityInactive
from facilities.models import HealthcareFacility
from facilities.services import deactivate_healthcare_facility
from tests.test_m11_document_metadata import make_facility
from tests.test_medical_document_concurrency import run_concurrently
from tests.test_medical_document_services import actor_and_patient, create

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only M11 constraint and concurrency tests")


def classification(document_uuid, actor_uuid, document_type):
    def operation():
        document = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        result = update_medical_document(
            document=document,
            actor=actor,
            metadata={"document_type": document_type},
        )
        return result.document_type

    return operation


def assignment(document_uuid, actor_uuid, facility_uuid):
    def operation():
        document = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        result = update_medical_document(
            document=document,
            actor=actor,
            metadata={"healthcare_facility_id": facility_uuid},
        )
        return result.healthcare_facility_id

    return operation


def deletion(document_uuid, actor_uuid):
    def operation():
        document = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        return soft_delete_medical_document(document=document, actor=actor).uuid

    return operation


def deactivation(facility_uuid):
    def operation():
        facility = HealthcareFacility.objects.get(uuid=facility_uuid)
        return deactivate_healthcare_facility(facility=facility).active

    return operation


def test_two_classification_updates_serialize_to_event_order(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    document = create(patient=patient, actor=actor)
    results, failures = run_concurrently(
        classification(document.uuid, actor.uuid, "RADIOLOGY"),
        classification(document.uuid, actor.uuid, "MEDICAL_REPORT"),
    )
    document.refresh_from_db()
    events = list(
        document.events.filter(
            event_type=MedicalDocumentEvent.EventType.DOCUMENT_TYPE_CHANGED
        ).order_by("created_at", "uuid")
    )
    assert not failures
    assert len(results) == 2
    assert len(events) == 2
    assert document.document_type == events[-1].metadata["new_type"]


def test_facility_assignment_vs_soft_delete_never_resurrects_document(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    document = create(patient=patient, actor=actor)
    facility = make_facility()
    results, failures = run_concurrently(
        assignment(document.uuid, actor.uuid, facility.uuid),
        deletion(document.uuid, actor.uuid),
    )
    document.refresh_from_db()
    assert len(results) + len(failures) == 2
    assert all(isinstance(exc, MedicalDocumentNotFound) for exc in failures)
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED


def test_facility_deactivation_vs_assignment_has_only_valid_outcomes(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    document = create(patient=patient, actor=actor)
    facility = make_facility()
    results, failures = run_concurrently(
        assignment(document.uuid, actor.uuid, facility.uuid),
        deactivation(facility.uuid),
    )
    document.refresh_from_db()
    facility.refresh_from_db()
    assert len(results) + len(failures) == 2
    assert all(isinstance(exc, HealthcareFacilityInactive) for exc in failures)
    assert facility.active is False
    assert document.healthcare_facility_id in {None, facility.uuid}


def test_postgresql_fk_and_hierarchy_constraints_are_authoritative():
    require_postgresql()
    facility = make_facility()
    duplicate = make_facility(active=False, name="Different Synthetic Center")
    with pytest.raises(IntegrityError), transaction.atomic():
        HealthcareFacility.objects.filter(pk=facility.pk).update(region=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        HealthcareFacility.objects.filter(pk=duplicate.pk).update(
            country=facility.country,
            city=facility.city,
            normalized_name=facility.normalized_name,
        )
    with pytest.raises(ProtectedError):
        facility.country.delete()
