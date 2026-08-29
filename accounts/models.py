import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower

from accounts.managers import UserManager


class User(AbstractUser):
    class Role(models.TextChoices):
        PATIENT = "PATIENT", "Patient"
        IDENTITY_VERIFICATION_AGENT = (
            "IDENTITY_VERIFICATION_AGENT",
            "Identity verification agent",
        )
        ADMIN = "ADMIN", "Admin"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PENDING_ACTIVATION = "PENDING_ACTIVATION", "Pending activation"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        DISABLED = "DISABLED", "Disabled"

    username = None
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=32, blank=True)
    role = models.CharField(max_length=32, choices=Role, default=Role.PATIENT)
    status = models.CharField(max_length=24, choices=Status, default=Status.PENDING)
    email_verified = models.BooleanField(default=False)
    # Authoritative timestamp set only server-side when a registration
    # completes with a verified email (M31B). Null for grandfathered accounts.
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        constraints = [
            models.UniqueConstraint(
                Lower("email"), name="accounts_user_email_ci_unique"
            )
        ]

    def __str__(self):
        return self.email
