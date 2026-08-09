from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny

from common.api import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/account-claims/", include("claims.urls")),
    path("api/v1/archive/", include("archive.urls")),
    path("api/v1/documents/", include("documents.urls")),
    path("api/v1/facilities/", include("facilities.urls")),
    path("api/v1/patients/", include("patients.urls")),
    path("api/v1/identity-documents/", include("identities.urls")),
    path("api/v1/minors/", include("guardians.urls")),
    path(
        "api/v1/verification/identity-documents/",
        include("identities.verification_urls"),
    ),
    path(
        "api/v1/verification/account-claims/",
        include("claims.verification_urls"),
    ),
    path(
        "api/v1/verification/guardian-relationships/",
        include("guardians.verification_urls"),
    ),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path(
        "api/v1/schema/",
        SpectacularAPIView.as_view(
            authentication_classes=[], permission_classes=[AllowAny]
        ),
        name="schema",
    ),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(
            url_name="schema",
            authentication_classes=[],
            permission_classes=[AllowAny],
        ),
        name="swagger-ui",
    ),
]
