from rest_framework import serializers

from facilities.models import (
    AdministrativeRegion,
    City,
    Country,
    HealthcareFacility,
    HealthcareFacilityAlias,
)


class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ("code", "name")
        read_only_fields = fields


class RegionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdministrativeRegion
        fields = ("uuid", "name", "code")
        read_only_fields = fields


class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ("uuid", "name")
        read_only_fields = fields


class FacilityAliasSerializer(serializers.ModelSerializer):
    class Meta:
        model = HealthcareFacilityAlias
        fields = ("uuid", "name", "language")
        read_only_fields = fields


class HealthcareFacilitySerializer(serializers.ModelSerializer):
    country = CountrySerializer(read_only=True)
    region = RegionSerializer(read_only=True)
    city = CitySerializer(read_only=True)
    aliases = serializers.SerializerMethodField()

    class Meta:
        model = HealthcareFacility
        fields = (
            "uuid",
            "name",
            "country",
            "region",
            "city",
            "address",
            "facility_type",
            "active",
            "aliases",
        )
        read_only_fields = fields

    def get_aliases(self, facility) -> list[dict]:
        return FacilityAliasSerializer(
            facility.aliases.filter(active=True), many=True
        ).data


class FacilityFilterSerializer(serializers.Serializer):
    country = serializers.RegexField(r"^[A-Z]{2}$", required=False)
    region = serializers.CharField(max_length=120, required=False)
    city = serializers.CharField(max_length=120, required=False)
    type = serializers.ChoiceField(
        choices=HealthcareFacility.FacilityType.choices, required=False
    )
    active = serializers.BooleanField(required=False, default=True)

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This filter is not allowed."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)
