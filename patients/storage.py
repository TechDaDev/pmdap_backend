import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


def _storage_backend() -> str:
    return os.getenv("STORAGE_BACKEND", "local").strip().lower()


if _storage_backend() == "s3":
    from storages.backends.s3boto3 import S3Boto3Storage as _StorageBase

    _S3 = True
else:
    _StorageBase = FileSystemStorage
    _S3 = False


@deconstructible
class PrivateAvatarStorage(_StorageBase):
    """Private patient profile avatar storage.

    Local backend stores under ``AVATAR_FILE_ROOT``. S3 backend stores under
    the ``avatar`` key prefix. Both keep files private: no public base URL and
    ``url()`` always raises so avatars are only served via authenticated
    streaming.
    """

    if _S3:
        location = "avatar"
        default_acl = "private"
        file_overwrite = False
        querystring_auth = False
        custom_domain = None
    else:

        @property
        def base_location(self):
            return str(settings.AVATAR_FILE_ROOT)

        @property
        def location(self):
            return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None

    def url(self, name):
        raise ValueError("Private avatar files do not have public URLs.")


private_avatar_storage = PrivateAvatarStorage()
