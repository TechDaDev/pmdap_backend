import pytest
from django.contrib.auth import get_user_model
from django.db import connection, transaction

from claims.models import AccountActivation, PatientAccountClaim
from claims.serializers import AccountClaimSubmissionSerializer
from claims.services.activation import activate_claimed_account
from claims.services.review import approve_account_claim
from claims.services.submission import submit_account_claim
from tests.factories import UserFactory
from tests.test_account_claiming import payload, verified_adult
from tests.test_postgresql_concurrency import run_concurrently

pytestmark = [pytest.mark.postgresql, pytest.mark.django_db(transaction=True)]
User = get_user_model()


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only account-claim concurrency test")


def create_claim():
    profile = verified_adult()
    serializer = AccountClaimSubmissionSerializer(data=payload())
    assert serializer.is_valid(), serializer.errors
    receipt = submit_account_claim(serializer.validated_data)
    return profile, PatientAccountClaim.objects.get(uuid=receipt.claim_id)


def reviewer():
    return UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")


def test_simultaneous_same_claim_approval_creates_one_account_and_activation():
    require_postgresql()
    profile, claim = create_claim()
    first_agent = reviewer()
    second_agent = reviewer()
    results, failures = run_concurrently(
        lambda: approve_account_claim(claim=claim, agent=first_agent).user_id,
        lambda: approve_account_claim(claim=claim, agent=second_agent).user_id,
    )
    assert len(results) == 1
    assert len(failures) == 1
    profile.refresh_from_db()
    assert profile.user_id == results[0]
    assert AccountActivation.objects.count() == 1


def test_simultaneous_duplicate_active_submissions_persist_one_claim():
    require_postgresql()
    profile = verified_adult()

    def operation():
        serializer = AccountClaimSubmissionSerializer(data=payload())
        assert serializer.is_valid(), serializer.errors
        return submit_account_claim(serializer.validated_data).claim_id

    results, failures = run_concurrently(operation, operation)
    assert len(results) == 2
    assert not failures
    assert PatientAccountClaim.objects.filter(patient=profile).count() == 1
    assert PatientAccountClaim.objects.filter(uuid__in=results).count() == 1


def test_ownership_race_has_one_authoritative_owner():
    require_postgresql()
    profile, claim = create_claim()
    claim_agent = reviewer()

    def competing_owner():
        with transaction.atomic():
            locked = type(profile).objects.select_for_update().get(pk=profile.pk)
            if locked.user_id:
                raise RuntimeError("already owned")
            user = User.objects.create_user(
                email="competing@example.com",
                password=None,
                status="PENDING_ACTIVATION",
            )
            locked.user = user
            locked.save(update_fields=("user", "updated_at"))
            return user.uuid

    results, failures = run_concurrently(
        lambda: approve_account_claim(claim=claim, agent=claim_agent).user_id,
        competing_owner,
    )
    assert len(results) == 1
    assert len(failures) == 1
    profile.refresh_from_db()
    assert profile.user_id == results[0]
    assert User.objects.filter(uuid=profile.user_id).count() == 1


def test_activation_double_consumption_succeeds_once():
    require_postgresql()
    profile, claim = create_claim()
    result = approve_account_claim(claim=claim, agent=reviewer())
    results, failures = run_concurrently(
        lambda: (
            activate_claimed_account(
                token=result.activation_token, new_password="StrongPass456!"
            ).uuid
        ),
        lambda: (
            activate_claimed_account(
                token=result.activation_token, new_password="StrongPass456!"
            ).uuid
        ),
    )
    assert len(results) == 1
    assert len(failures) == 1
    profile.refresh_from_db()
    assert profile.user.status == "ACTIVE"
    assert AccountActivation.objects.get().used_at is not None
