from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from common.models import UUIDModel
from facilities.normalization import normalize_reference_name


class Country(models.Model):
    code = models.CharField(
        primary_key=True,
        max_length=2,
        validators=(RegexValidator(r"^[A-Z]{2}$", "Use ISO alpha-2 code."),),
    )
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name", "code")

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = " ".join(self.name.split())
        self.full_clean()
        return super().save(*args, **kwargs)


class AdministrativeRegion(UUIDModel):
    country = models.ForeignKey(
        Country, on_delete=models.PROTECT, related_name="regions"
    )
    name = models.CharField(max_length=120)
    normalized_name = models.CharField(max_length=120, editable=False)
    code = models.CharField(max_length=32, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("normalized_name", "uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("country", "normalized_name"),
                name="facility_region_country_name_unique",
            )
        ]

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.normalized_name = normalize_reference_name(self.name)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class City(UUIDModel):
    region = models.ForeignKey(
        AdministrativeRegion, on_delete=models.PROTECT, related_name="cities"
    )
    name = models.CharField(max_length=120)
    normalized_name = models.CharField(max_length=120, editable=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("normalized_name", "uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("region", "normalized_name"),
                name="facility_city_region_name_unique",
            )
        ]

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.normalized_name = normalize_reference_name(self.name)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class HealthcareFacility(UUIDModel):
    class FacilityType(models.TextChoices):
        HOSPITAL = "HOSPITAL", "Hospital"
        CLINIC = "CLINIC", "Clinic"
        LABORATORY = "LABORATORY", "Laboratory"
        RADIOLOGY_CENTER = "RADIOLOGY_CENTER", "Radiology center"
        PHARMACY = "PHARMACY", "Pharmacy"
        PRIMARY_CARE_CENTER = "PRIMARY_CARE_CENTER", "Primary care center"
        SPECIALIZED_CENTER = "SPECIALIZED_CENTER", "Specialized center"
        UNIVERSITY_HOSPITAL = "UNIVERSITY_HOSPITAL", "University hospital"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, editable=False)
    country = models.ForeignKey(
        Country, on_delete=models.PROTECT, related_name="facilities"
    )
    region = models.ForeignKey(
        AdministrativeRegion,
        on_delete=models.PROTECT,
        related_name="facilities",
        null=True,
        blank=True,
    )
    city = models.ForeignKey(
        City,
        on_delete=models.PROTECT,
        related_name="facilities",
        null=True,
        blank=True,
    )
    address = models.CharField(max_length=500, blank=True)
    facility_type = models.CharField(max_length=24, choices=FacilityType)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("normalized_name", "uuid")
        constraints = [
            models.CheckConstraint(
                condition=Q(city__isnull=True) | Q(region__isnull=False),
                name="facility_city_requires_region",
            ),
            models.UniqueConstraint(
                fields=("country", "city", "normalized_name"),
                name="facility_country_city_name_unique",
                nulls_distinct=False,
            ),
        ]
        indexes = [
            models.Index(
                fields=("active", "normalized_name"),
                name="facility_active_name_idx",
            )
        ]

    def clean(self):
        errors = {}
        if self.region_id and self.region.country_id != self.country_id:
            errors["region"] = "Region does not belong to facility country."
        if self.city_id and self.city.region_id != self.region_id:
            errors["city"] = "City does not belong to facility region."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.address = " ".join(self.address.split())
        self.normalized_name = normalize_reference_name(self.name)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class HealthcareFacilityAlias(UUIDModel):
    facility = models.ForeignKey(
        HealthcareFacility, on_delete=models.PROTECT, related_name="aliases"
    )
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, editable=False)
    language = models.CharField(max_length=10, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("normalized_name", "uuid")
        constraints = [
            models.UniqueConstraint(
                fields=("facility", "normalized_name"),
                name="facility_alias_name_unique",
            )
        ]

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.language = self.language.strip().lower()
        self.normalized_name = normalize_reference_name(self.name)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name
