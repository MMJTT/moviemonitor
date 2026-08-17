from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from ticketwatch.config import build_database_config, env_bool, validate_production_env


def test_production_requires_secret_and_postgres():
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        validate_production_env({"TICKETWATCH_ENV": "production"})


def test_production_database_never_falls_back_to_sqlite():
    env = {
        "TICKETWATCH_ENV": "production",
        "DJANGO_SECRET_KEY": "server-secret",
    }
    with pytest.raises(ImproperlyConfigured, match="POSTGRES_HOST"):
        build_database_config(env, Path("/app"))


def test_postgres_config_uses_explicit_values():
    env = {
        "TICKETWATCH_ENV": "production",
        "DJANGO_SECRET_KEY": "server-secret",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "ticketwatch",
        "POSTGRES_USER": "ticketwatch",
        "POSTGRES_PASSWORD": "db-secret",
    }
    config = build_database_config(env, Path("/app"))["default"]
    assert config["ENGINE"] == "django.db.backends.postgresql"
    assert config["HOST"] == "postgres"
    assert config["CONN_MAX_AGE"] == 60


def test_env_bool_rejects_ambiguous_value():
    with pytest.raises(ImproperlyConfigured, match="DJANGO_DEBUG"):
        env_bool({"DJANGO_DEBUG": "sometimes"}, "DJANGO_DEBUG", False)
