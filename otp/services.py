import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from otp.delivery import ResendOtpDeliveryService
from otp.exceptions import (
    InvalidOtp,
    OtpCooldown,
    OtpDeliveryFailed,
    OtpRateLimited,
    UnsupportedOtpChannel,
)
from otp.models import (
    OtpAuthorization,
    OtpChallenge,
    OtpPurpose,
    OtpRateLimitBucket,
    OtpTargetState,
)


@dataclass(frozen=True)
class OtpIssueResult:
    challenge_uuid: object
    expires_at: object
    resend_at: object


@dataclass(frozen=True)
class OtpAuthorizationResult:
    token: str
    expires_at: object


def _secret_digest(value):
    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def _normalize_target(*, channel, target):
    value = target.strip()
    if channel == OtpTargetState.Channel.EMAIL:
        value = value.casefold()
        try:
            validate_email(value)
        except ValidationError as exc:
            raise ValueError("Invalid OTP target.") from exc
        return value
    if channel == OtpTargetState.Channel.SMS:
        return value
    raise UnsupportedOtpChannel()


def _target_hash(*, channel, target):
    return _secret_digest(f"otp-target:v1:{channel}:{target}")


def _code_material(*, challenge_uuid, code):
    return _secret_digest(f"otp-code:v1:{challenge_uuid}:{code}")


def _lock_bucket(*, kind, raw_key, now, limit):
    key_hash = _secret_digest(f"otp-rate:v1:{kind}:{raw_key}")
    bucket, _ = OtpRateLimitBucket.objects.get_or_create(
        kind=kind,
        key_hash=key_hash,
        defaults={"window_started_at": now},
    )
    bucket = OtpRateLimitBucket.objects.select_for_update().get(pk=bucket.pk)
    window_seconds = settings.OTP_ISSUE_RATE_WINDOW_SECONDS
    if bucket.window_started_at <= now - timedelta(seconds=window_seconds):
        bucket.window_started_at = now
        bucket.request_count = 0
    if bucket.request_count >= limit:
        raise OtpRateLimited("OTP request rate limit exceeded.")
    bucket.request_count += 1
    bucket.save(update_fields=("window_started_at", "request_count", "updated_at"))


def _apply_issue_limits(*, target_hash, account, source, now):
    keys = [
        (
            OtpRateLimitBucket.Kind.TARGET,
            target_hash,
            settings.OTP_ISSUE_LIMIT_TARGET,
        )
    ]
    if account is not None:
        keys.append(
            (
                OtpRateLimitBucket.Kind.ACCOUNT,
                str(account.pk),
                settings.OTP_ISSUE_LIMIT_ACCOUNT,
            )
        )
    if source:
        keys.append(
            (
                OtpRateLimitBucket.Kind.SOURCE,
                str(source),
                settings.OTP_ISSUE_LIMIT_SOURCE,
            )
        )
    for kind, raw_key, limit in keys:
        _lock_bucket(kind=kind, raw_key=raw_key, now=now, limit=limit)


def issue_otp(
    *,
    purpose,
    channel,
    target,
    account=None,
    source=None,
    delivery_service=None,
    locale="en",
):
    if purpose not in OtpPurpose.values:
        raise ValueError("Invalid OTP purpose.")
    if channel != OtpTargetState.Channel.EMAIL:
        raise UnsupportedOtpChannel("OTP delivery channel is not available.")
    normalized_target = _normalize_target(channel=channel, target=target)
    target_digest = _target_hash(channel=channel, target=normalized_target)
    now = timezone.now()
    ttl_minutes = settings.OTP_TTL_MINUTES
    cooldown_seconds = settings.OTP_RESEND_COOLDOWN_SECONDS

    with transaction.atomic():
        state, _ = OtpTargetState.objects.get_or_create(
            purpose=purpose,
            channel=channel,
            target_hash=target_digest,
        )
        state = OtpTargetState.objects.select_for_update().get(pk=state.pk)
        if state.last_issued_at is not None:
            resend_at = state.last_issued_at + timedelta(seconds=cooldown_seconds)
            if resend_at > now:
                retry_after = max(1, math.ceil((resend_at - now).total_seconds()))
                raise OtpCooldown(retry_after)

        _apply_issue_limits(
            target_hash=target_digest, account=account, source=source, now=now
        )
        OtpChallenge.objects.select_for_update().filter(
            state=state,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
            locked_at__isnull=True,
        ).update(invalidated_at=now, updated_at=now)

        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = OtpChallenge(
            state=state,
            account=account,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        challenge.code_hash = make_password(
            _code_material(challenge_uuid=challenge.uuid, code=code)
        )
        challenge.save()
        state.last_issued_at = now
        state.save(update_fields=("last_issued_at", "updated_at"))
        record_audit(
            action=AuditLog.Action.OTP_REQUESTED,
            actor=account,
            resource_type="OTP_CHALLENGE",
            resource_uuid=challenge.uuid,
            metadata={"purpose": purpose, "channel": channel},
        )

    sender = delivery_service or ResendOtpDeliveryService()
    try:
        sender.send_email_otp(
            target=normalized_target,
            code=code,
            expires_minutes=ttl_minutes,
            locale=locale,
        )
    except Exception as exc:
        OtpChallenge.objects.filter(
            pk=challenge.pk,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        ).update(invalidated_at=timezone.now(), updated_at=timezone.now())
        raise OtpDeliveryFailed("OTP delivery failed.") from exc

    return OtpIssueResult(
        challenge_uuid=challenge.uuid,
        expires_at=challenge.expires_at,
        resend_at=now + timedelta(seconds=cooldown_seconds),
    )


def verify_otp(*, purpose, channel, target, code, account=None):
    normalized_target = _normalize_target(channel=channel, target=target)
    target_digest = _target_hash(channel=channel, target=normalized_target)
    now = timezone.now()
    failed = False
    result = None

    with transaction.atomic():
        try:
            state = OtpTargetState.objects.select_for_update().get(
                purpose=purpose,
                channel=channel,
                target_hash=target_digest,
            )
            challenge = (
                OtpChallenge.objects.select_for_update()
                .select_related("state")
                .get(
                    state=state,
                    consumed_at__isnull=True,
                    invalidated_at__isnull=True,
                    locked_at__isnull=True,
                )
            )
        except (OtpTargetState.DoesNotExist, OtpChallenge.DoesNotExist) as exc:
            raise InvalidOtp("Invalid or unavailable OTP.") from exc

        if challenge.expires_at <= now or challenge.account_id != getattr(
            account, "pk", None
        ):
            raise InvalidOtp("Invalid or unavailable OTP.")

        material = _code_material(challenge_uuid=challenge.uuid, code=str(code))
        if not check_password(material, challenge.code_hash):
            challenge.failed_attempts += 1
            action = AuditLog.Action.OTP_FAILED
            if challenge.failed_attempts >= settings.OTP_MAX_ATTEMPTS:
                challenge.locked_at = now
                action = AuditLog.Action.OTP_LOCKED
            challenge.save(
                update_fields=(
                    "failed_attempts",
                    "locked_at",
                    "updated_at",
                )
            )
            record_audit(
                action=action,
                actor=account,
                resource_type="OTP_CHALLENGE",
                resource_uuid=challenge.uuid,
                metadata={"purpose": purpose, "channel": channel},
            )
            failed = True
        else:
            challenge.consumed_at = now
            challenge.save(update_fields=("consumed_at", "updated_at"))
            raw_token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(minutes=settings.OTP_AUTHORIZATION_TTL_MINUTES)
            OtpAuthorization.objects.create(
                challenge=challenge,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                expires_at=expires_at,
            )
            record_audit(
                action=AuditLog.Action.OTP_VERIFIED,
                actor=account,
                resource_type="OTP_CHALLENGE",
                resource_uuid=challenge.uuid,
                metadata={"purpose": purpose, "channel": channel},
            )
            result = OtpAuthorizationResult(token=raw_token, expires_at=expires_at)

    if failed:
        raise InvalidOtp("Invalid or unavailable OTP.")
    return result


def consume_otp_authorization(*, token, purpose, channel, target, account=None):
    normalized_target = _normalize_target(channel=channel, target=target)
    target_digest = _target_hash(channel=channel, target=normalized_target)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        try:
            authorization = (
                OtpAuthorization.objects.select_for_update()
                .select_related("challenge__state")
                .get(token_hash=token_hash)
            )
        except OtpAuthorization.DoesNotExist as exc:
            raise InvalidOtp("Invalid or unavailable OTP authorization.") from exc
        challenge = authorization.challenge
        superseded = OtpChallenge.objects.filter(
            state=challenge.state,
            created_at__gt=challenge.created_at,
            invalidated_at__isnull=True,
        ).exists()
        if (
            authorization.consumed_at is not None
            or authorization.expires_at <= now
            or superseded
            or challenge.state.purpose != purpose
            or challenge.state.channel != channel
            or challenge.state.target_hash != target_digest
            or challenge.account_id != getattr(account, "pk", None)
        ):
            raise InvalidOtp("Invalid or unavailable OTP authorization.")
        authorization.consumed_at = now
        authorization.save(update_fields=("consumed_at", "updated_at"))
        record_audit(
            action=AuditLog.Action.OTP_CONSUMED,
            actor=account,
            resource_type="OTP_CHALLENGE",
            resource_uuid=challenge.uuid,
            metadata={"purpose": purpose, "channel": channel},
        )
        return challenge
