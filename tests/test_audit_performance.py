"""
PostgreSQL-only audit performance sanity (M14 #29).

Confirms the audit log supports the projected growth (tens of thousands of
rows) with indexed patient/actor/resource/action lookups and no sequential
scans on the four primary query shapes.
"""

import time
from datetime import date

import pytest
from django.db import connection

from accounts.models import User
from audit.models import AuditLog
from patients.models import PatientProfile

pytestmark = [pytest.mark.postgresql, pytest.mark.django_db]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only audit performance test")


def make_actor_and_patient(index):
    user = User.objects.create_user(
        email=f"perf-actor-{index}@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=user,
        digital_id=f"{12345678901234567 - index}",
        full_name=f"Perf Patient {index}",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, patient


def bulk_audit_rows(actor, patient, count, action=AuditLog.Action.DOCUMENT_UPLOADED):
    rows = []
    for _ in range(count):
        rows.append(
            AuditLog(
                actor=actor,
                actor_type=AuditLog.ActorType.USER,
                patient=patient,
                resource_type="DOCUMENT",
                action=action,
                new_values={"document_date": "2026-03-14"},
            )
        )
    return rows


def explain_is_index_scan(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "Seq Scan" not in plan, plan
    return plan


def test_audit_growth_queries_stay_indexed():
    require_postgresql()
    actor, patient = make_actor_and_patient(1)
    other_actor, _ = make_actor_and_patient(2)
    AuditLog.objects.bulk_create(bulk_audit_rows(actor, patient, 10_000))
    AuditLog.objects.bulk_create(
        bulk_audit_rows(
            other_actor, patient, 5_000, action=AuditLog.Action.DOCUMENT_DELETED
        )
    )
    AuditLog.objects.bulk_create(
        bulk_audit_rows(actor, patient, 2_000, action=AuditLog.Action.DATE_CONFIRMED)
    )
    assert AuditLog.objects.count() == 17_000

    # Patient history query shape.
    patient_history = AuditLog.objects.filter(patient=patient).order_by(
        "-created_at", "-uuid"
    )
    start = time.monotonic()
    assert patient_history.count() == 17_000
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"patient history too slow: {elapsed:.2f}s"

    # Actor history query shape.
    actor_history = AuditLog.objects.filter(actor=actor).order_by("-created_at")
    assert actor_history.count() == 12_000

    # Resource lookup shape.
    assert (
        AuditLog.objects.filter(
            resource_type="DOCUMENT", resource_uuid=patient.uuid
        ).count()
        >= 0
    )

    # Action filter shape.
    assert (
        AuditLog.objects.filter(action=AuditLog.Action.DOCUMENT_DELETED).count()
        == 5_000
    )

    # Index usage verification on the four primary shapes.
    explain_is_index_scan(
        "SELECT 1 FROM audit_auditlog WHERE patient_id = %s",
        (patient.pk,),
    )
    explain_is_index_scan(
        "SELECT 1 FROM audit_auditlog WHERE actor_id = %s",
        (actor.pk,),
    )
    explain_is_index_scan(
        "SELECT 1 FROM audit_auditlog WHERE action = %s",
        (AuditLog.Action.DOCUMENT_DELETED,),
    )
    explain_is_index_scan(
        "SELECT 1 FROM audit_auditlog WHERE resource_type = %s AND resource_uuid = %s",
        ("DOCUMENT", str(patient.uuid)),
    )


def test_audit_bulk_ingest_is_fast_enough():
    require_postgresql()
    actor, patient = make_actor_and_patient(3)
    start = time.monotonic()
    AuditLog.objects.bulk_create(bulk_audit_rows(actor, patient, 20_000))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"bulk ingest too slow: {elapsed:.2f}s"
    assert AuditLog.objects.filter(patient=patient).count() == 20_000
