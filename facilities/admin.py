from django.contrib import admin

from facilities.models import (
    AdministrativeRegion,
    City,
    Country,
    HealthcareFacility,
    HealthcareFacilityAlias,
)

admin.site.register(Country)
admin.site.register(AdministrativeRegion)
admin.site.register(City)
admin.site.register(HealthcareFacility)
admin.site.register(HealthcareFacilityAlias)
