from django.conf import settings
from django.db import models
from django.db.models import Q

from common.models import UUIDModel


class OtpPurpose(models.TextChoices):
    EMAIL_VERIFICATION = "EMAIL_VERIFICATION", "Email verification"
    PASSWORD_RESET = "PASSWORD_RESET", "Password reset"
    PASSWORD_CHANGE = "PASSWORD_CHANGE", "Password change"
    EMAIL_CHANGE = "EMAIL_CHANGE", "Email change"
    PHONE_VERIFICATION = "PHONE_VERIFICATION", "Phone verification"


class OtpTargetState(UUIDModel):
    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"

    purpose = models.CharField(max_length=32, choices=OtpPurpose)
    channel = models.CharField(max_length=16, choices=Channel)
    target_hash = models.CharField(max_length=64)
    last_issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("purpose", "channel", "target_hash"),
                name="otp_unique_target_purpose_channel",
            )
        ]


class OtpChallenge(UUIDModel):
    Channel = OtpTargetState.Channel

    state = models.ForeignKey(
        OtpTargetState, on_delete=models.PROTECT, related_name="challenges"
    )
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="otp_challenges",
    )
    code_hash = models.CharField(max_length=256)
    expires_at = models.DateTimeField()
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("state",),
                condition=Q(
                    consumed_at__isnull=True,
                    invalidated_at__isnull=True,
                    locked_at__isnull=True,
                ),
                name="otp_one_unfinished_challenge_per_target",
            )
        ]
        indexes = [models.Index(fields=("state", "-created_at"))]

    @property
    def purpose(self):
        return self.state.purpose

    @property
    def channel(self):
        return self.state.channel


class OtpAuthorization(UUIDModel):
    challenge = models.OneToOneField(
        OtpChallenge,
        on_delete=models.PROTECT,
        related_name="authorization",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)


class OtpRateLimitBucket(UUIDModel):
    class Kind(models.TextChoices):
        TARGET = "TARGET", "Target"
        ACCOUNT = "ACCOUNT", "Account"
        SOURCE = "SOURCE", "Request source"

    kind = models.CharField(max_length=16, choices=Kind)
    key_hash = models.CharField(max_length=64)
    window_started_at = models.DateTimeField()
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "key_hash"), name="otp_unique_rate_limit_bucket"
            )
        ]
