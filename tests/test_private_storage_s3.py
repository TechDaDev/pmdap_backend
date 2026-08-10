"""Private storage backend tests: env-driven S3 selection + S3 semantics.

The S3 backend is exercised with an in-memory fake boto3 *resource* so the
normal unit suite never needs a live bucket. Local (filesystem) backend
semantics are covered by the rest of the suite.
"""

import hashlib
import importlib
import io
import types

import pytest
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.files.storage import FileSystemStorage

import documents.storage
import identities.storage


def _s3_error(code, message, operation):
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        operation,
    )


class FakeObject:
    def __init__(self, key, store):
        self.key = key
        self.store = store

    def upload_fileobj(self, fileobj, ExtraArgs=None, Callback=None, Config=None):
        self.store[self.key] = fileobj.read()

    def download_fileobj(self, fileobj, ExtraArgs=None, Callback=None, Config=None):
        if self.key not in self.store:
            raise _s3_error("404", "Not Found", "GetObject")
        fileobj.write(self.store[self.key])
        fileobj.seek(0)

    def get(self, **kwargs):
        if self.key not in self.store:
            raise _s3_error("404", "Not Found", "GetObject")
        return {"Body": io.BytesIO(self.store[self.key])}

    def load(self, **params):
        if self.key not in self.store:
            raise _s3_error("404", "Not Found", "HeadObject")

    def delete(self):
        self.store.pop(self.key, None)


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def Object(self, key):
        return FakeObject(key, self.store)


class FakeMetaClient:
    def __init__(self, store):
        self.store = store

    def head_object(self, Bucket=None, Key=None, **params):
        if Key not in self.store:
            raise _s3_error("404", "Not Found", "HeadObject")
        return {
            "ContentLength": len(self.store[Key]),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


class FakeS3Resource:
    """In-memory stand-in for the boto3 S3 resource used by django-storages."""

    def __init__(self):
        self.store = {}
        self.meta = types.SimpleNamespace(client=FakeMetaClient(self.store))

    def Bucket(self, name):
        return FakeBucket(self.store)


@pytest.fixture
def s3_backend(monkeypatch):
    """Reload storage modules in S3 mode; restore local mode on teardown."""
    # django-storages 1.14 captures settings at instance construction, so the
    # AWS settings must be in place before the modules are reloaded.
    monkeypatch.setattr(settings, "AWS_STORAGE_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(settings, "AWS_S3_ENDPOINT_URL", "https://t3.storageapi.dev")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(settings, "AWS_S3_REGION_NAME", "iad")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    importlib.reload(documents.storage)
    importlib.reload(identities.storage)
    medical = documents.storage.private_medical_storage
    identity = identities.storage.private_identity_storage
    yield medical, identity
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    importlib.reload(documents.storage)
    importlib.reload(identities.storage)


def test_s3_backend_selected_and_blocks_public_urls(s3_backend):
    from storages.backends.s3boto3 import S3Boto3Storage

    medical, identity = s3_backend
    assert isinstance(medical, S3Boto3Storage)
    assert isinstance(identity, S3Boto3Storage)
    assert medical.location == "medical"
    assert identity.location == "identity"
    assert medical.bucket_name == "test-bucket"
    assert medical.base_url is None
    with pytest.raises(ValueError, match="do not have public URLs"):
        medical.url("medical/report.pdf")
    with pytest.raises(ValueError, match="do not have public URLs"):
        identity.url("identity/front.png")


def test_s3_private_medical_byte_for_byte_roundtrip_and_hash(s3_backend, monkeypatch):
    medical, _ = s3_backend
    payload = b"%PDF-1.4 synthetic report bytes \x00\x01\x02"
    fake = FakeS3Resource()
    medical._connections.connection = fake

    name = medical.save("report.pdf", io.BytesIO(payload))

    assert name == "report.pdf"
    assert "medical/report.pdf" in fake.store
    with medical.open(name, "rb") as handle:
        readback = handle.read()
    assert readback == payload
    assert hashlib.sha256(readback).digest() == hashlib.sha256(payload).digest()
    # storage key is never exposed as a public URL
    with pytest.raises(ValueError, match="do not have public URLs"):
        medical.url(name)


def test_s3_private_identity_roundtrip_delete(s3_backend, monkeypatch):
    _, identity = s3_backend
    payload = b"\x89PNG\r\n\x1a\n fake identity image"
    fake = FakeS3Resource()
    identity._connections.connection = fake

    name = identity.save("front.png", io.BytesIO(payload))

    assert name == "front.png"
    assert "identity/front.png" in fake.store
    with identity.open(name, "rb") as handle:
        assert handle.read() == payload
    assert identity.exists(name) is True
    identity.delete(name)
    assert identity.exists(name) is False
    assert "identity/front.png" not in fake.store


def test_s3_missing_object_raises_controlled_error(s3_backend, monkeypatch):
    medical, _ = s3_backend
    fake = FakeS3Resource()
    medical._connections.connection = fake
    with pytest.raises(FileNotFoundError):
        medical.open("medical/does-not-exist.pdf", "rb")


def test_local_backend_remains_default_and_private():
    importlib.reload(documents.storage)
    importlib.reload(identities.storage)
    assert isinstance(documents.storage.private_medical_storage, FileSystemStorage)
    assert isinstance(identities.storage.private_identity_storage, FileSystemStorage)
    assert documents.storage.private_medical_storage.location == str(
        settings.MEDICAL_FILE_ROOT
    )
    assert identities.storage.private_identity_storage.location == str(
        settings.IDENTITY_FILE_ROOT
    )
    with pytest.raises(ValueError, match="do not have public URLs"):
        documents.storage.private_medical_storage.url("medical/report.pdf")
