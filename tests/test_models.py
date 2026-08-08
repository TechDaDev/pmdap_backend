from uuid import UUID

import pytest
from django.contrib.auth import get_user_model

from common.models import UUIDModel
from tests.factories import UserFactory


def test_uuid_model_is_abstract_and_timestamped():
    assert UUIDModel._meta.abstract is True
    assert UUIDModel._meta.pk.name == "uuid"
    assert UUIDModel._meta.get_field("created_at").auto_now_add is True
    assert UUIDModel._meta.get_field("updated_at").auto_now is True


@pytest.mark.django_db
def test_user_uses_generated_uuid_and_unique_email():
    user = UserFactory()
    user_model = get_user_model()

    assert isinstance(user.uuid, UUID)
    assert user_model._meta.pk.name == "uuid"
    assert user_model._meta.get_field("email").unique is True
    assert user_model.USERNAME_FIELD == "email"
    assert "username" not in [field.name for field in user_model._meta.fields]


@pytest.mark.django_db
def test_user_factory_hashes_password():
    user = UserFactory(password="correct horse battery staple")

    assert user.check_password("correct horse battery staple") is True
