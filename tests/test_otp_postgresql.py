import threading

import pytest
from django.db import close_old_connections, connection

from otp.exceptions import OtpCooldown
from otp.models import OtpChallenge, OtpPurpose
from otp.services import issue_otp

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


class ThreadSafeDelivery:
    def send_email_otp(self, **kwargs):
        return None


def test_concurrent_issuance_leaves_one_usable_challenge():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL concurrency test")

    barrier = threading.Barrier(2)
    outcomes = []
    outcome_lock = threading.Lock()

    def worker():
        close_old_connections()
        barrier.wait()
        try:
            issue_otp(
                purpose=OtpPurpose.EMAIL_VERIFICATION,
                channel="EMAIL",
                target="race@example.com",
                delivery_service=ThreadSafeDelivery(),
            )
        except OtpCooldown:
            outcome = "cooldown"
        else:
            outcome = "issued"
        finally:
            close_old_connections()
        with outcome_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["cooldown", "issued"]
    assert (
        OtpChallenge.objects.filter(
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
            locked_at__isnull=True,
        ).count()
        == 1
    )
