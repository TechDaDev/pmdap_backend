import hashlib

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from audit.models import AuditLog
from audit.services import record_audit
from claims.exceptions import InvalidActivationToken
from claims.models import AccountActivation, PatientAccountClaimEvent


def activate_claimed_account(*, token, new_password):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with transaction.atomic():
        try:
            activation = (
                AccountActivation.objects.select_for_update()
                .select_related("user", "claim")
                .get(token_hash=token_hash)
            )
        except AccountActivation.DoesNotExist as exc:
            raise InvalidActivationToken() from exc
        if activation.used_at is not None or activation.expires_at <= timezone.now():
            raise InvalidActivationToken()
        user = activation.user
        if (
            user.status != User.Status.PENDING_ACTIVATION
            or activation.claim.status != activation.claim.Status.APPROVED
            or activation.claim.approved_user_id != user.pk
        ):
            raise InvalidActivationToken()
        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": exc.messages}) from exc
        user.set_password(new_password)
        user.status = User.Status.ACTIVE
        user.save(update_fields=("password", "status", "updated_at"))
        activation.used_at = timezone.now()
        activation.save(update_fields=("used_at", "updated_at"))
        PatientAccountClaimEvent.objects.create(
            claim=activation.claim,
            event_type=PatientAccountClaimEvent.EventType.ACTIVATED,
            actor=user,
            metadata={},
        )
        record_audit(
            action=AuditLog.Action.ACCOUNT_ACTIVATED,
            actor=user,
            patient=activation.claim.patient,
            resource_type="USER",
            resource_uuid=user.uuid,
            previous_values={"status": User.Status.PENDING_ACTIVATION},
            new_values={"status": User.Status.ACTIVE},
        )
        return user
