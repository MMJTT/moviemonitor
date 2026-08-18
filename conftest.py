import pytest
from django.test import Client


@pytest.fixture
def owner_user(db, django_user_model):
    user, _ = django_user_model.objects.get_or_create(
        username="850634546@qq.com",
        defaults={
            "email": "850634546@qq.com",
            "is_active": True,
            "is_staff": True,
            "is_superuser": True,
        },
    )
    changed = []
    if user.email != "850634546@qq.com":
        user.email = "850634546@qq.com"
        changed.append("email")
    if not user.is_active:
        user.is_active = True
        changed.append("is_active")
    if not user.is_staff:
        user.is_staff = True
        changed.append("is_staff")
    if changed:
        user.save(update_fields=changed)
    return user


@pytest.fixture
def client(owner_user):
    authenticated = Client()
    authenticated.force_login(owner_user)
    return authenticated


@pytest.fixture
def anonymous_client():
    return Client()
