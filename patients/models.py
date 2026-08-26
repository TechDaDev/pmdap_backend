from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from common.models import UUIDModel
from patients.storage import private_avatar_storage


def validate_not_future(value):
    if value > timezone.localdate():
        raise ValidationError("Date of birth cannot be in the future.")


class PatientProfile(UUIDModel):
    class Sex(models.TextChoices):
        FEMALE = "FEMALE", "Female"
        MALE = "MALE", "Male"
        UNSPECIFIED = "UNSPECIFIED", "Unspecified"

    class BloodGroup(models.TextChoices):
        A_POSITIVE = "A+", "A+"
        A_NEGATIVE = "A-", "A-"
        B_POSITIVE = "B+", "B+"
        B_NEGATIVE = "B-", "B-"
        AB_POSITIVE = "AB+", "AB+"
        AB_NEGATIVE = "AB-", "AB-"
        O_POSITIVE = "O+", "O+"
        O_NEGATIVE = "O-", "O-"
        UNKNOWN = "UNKNOWN", "Unknown"

    class IdentityStatus(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", "Unverified"
        PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending verification"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    class Governorate(models.TextChoices):
        """Stable Iraqi governorate codes (registration residence).

        Aligned with the seeded facilities AdministrativeRegion names; kept as
        explicit stable codes here so the public registration API never
        depends on a mutable location table.
        """

        AL_ANBAR = "AL_ANBAR", "Al Anbar"
        AL_QADISIYYAH = "AL_QADISIYYAH", "Al-Qadisiyyah"
        BABIL = "BABIL", "Babil"
        BAGHDAD = "BAGHDAD", "Baghdad"
        BASRA = "BASRA", "Basra"
        DHI_QAR = "DHI_QAR", "Dhi Qar"
        DIYALA = "DIYALA", "Diyala"
        DUHOK = "DUHOK", "Duhok"
        ERBIL = "ERBIL", "Erbil"
        HALABJA = "HALABJA", "Halabja"
        KARBALA = "KARBALA", "Karbala"
        KIRKUK = "KIRKUK", "Kirkuk"
        MAYSAN = "MAYSAN", "Maysan"
        MUTHANNA = "MUTHANNA", "Muthanna"
        NAJAF = "NAJAF", "Najaf"
        NINEVEH = "NINEVEH", "Nineveh"
        SALADIN = "SALADIN", "Saladin"
        SULAYMANIYAH = "SULAYMANIYAH", "Sulaymaniyah"
        WASIT = "WASIT", "Wasit"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="patient_profile",
        null=True,
        blank=True,
    )
    digital_id = models.CharField(max_length=17, unique=True, editable=False)
    full_name = models.CharField(max_length=255)
    # Structured Iraqi patronymic components. Canonical; `full_name` is only
    # the deterministic display join "given father grandfather".
    given_name = models.CharField(max_length=255, blank=True, default="")
    father_name = models.CharField(max_length=255, blank=True, default="")
    grandfather_name = models.CharField(max_length=255, blank=True, default="")
    # Current residence / registration governorate (Iraqi card flow).
    governorate = models.CharField(
        max_length=32, choices=Governorate.choices, blank=True, default=""
    )
    date_of_birth = models.DateField(validators=[validate_not_future])
    sex = models.CharField(max_length=16, choices=Sex)
    nationality = models.CharField(
        max_length=2,
        validators=[
            RegexValidator(
                regex=r"^[A-Z]{2}$",
                message="Use an uppercase ISO alpha-2 country code.",
            )
        ],
    )
    blood_group = models.CharField(
        max_length=7, choices=BloodGroup, default=BloodGroup.UNKNOWN
    )
    identity_status = models.CharField(
        max_length=24,
        choices=IdentityStatus,
        default=IdentityStatus.UNVERIFIED,
    )
    avatar = models.ImageField(
        storage=private_avatar_storage,
        upload_to="",
        null=True,
        blank=True,
    )

    def age_on(self, today):
        return (
            today.year
            - self.date_of_birth.year
            - (
                (today.month, today.day)
                < (self.date_of_birth.month, self.date_of_birth.day)
            )
        )

    @property
    def age(self):
        return self.age_on(timezone.localdate())

    @property
    def is_minor(self):
        return self.age < 18

    def clean(self):
        super().clean()
        errors = {}
        if self.user_id:
            if self.user.role != self.user.Role.PATIENT:
                errors["user"] = "Direct owner must have PATIENT role."
            if self.date_of_birth and self.age_on(timezone.localdate()) < 18:
                errors["date_of_birth"] = "Direct owner must be an adult patient."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._original_uuid = instance.uuid
        return instance

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original_uuid = getattr(self, "_original_uuid", self.uuid)
            original = type(self).objects.only("digital_id").get(pk=original_uuid)
            if self.uuid != original_uuid or self.digital_id != original.digital_id:
                raise ValidationError("Patient UUID and Digital ID are immutable.")
        result = super().save(*args, **kwargs)
        self._original_uuid = self.uuid
        return result

    def __str__(self):
        return self.digital_id
