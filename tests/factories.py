import factory
from django.contrib.auth import get_user_model


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()

    email = factory.Faker("unique.email")
    password = factory.PostGenerationMethodCall("set_password", "ChangeMe123!")
