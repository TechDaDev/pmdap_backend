import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateIdentityStorage(FileSystemStorage):
    @property
    def base_location(self):
        return str(settings.IDENTITY_FILE_ROOT)

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None

    def url(self, name):
        raise ValueError("Private identity files do not have public URLs.")


private_identity_storage = PrivateIdentityStorage()
