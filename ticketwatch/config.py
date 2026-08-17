from django.core.exceptions import ImproperlyConfigured


def env_bool(env, name, default):
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean")


def validate_production_env(env):
    if env.get("TICKETWATCH_ENV") != "production":
        return
    if not env.get("DJANGO_SECRET_KEY"):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required in production")


def build_database_config(env, base_dir):
    production = env.get("TICKETWATCH_ENV") == "production"
    host = env.get("POSTGRES_HOST")
    if not host:
        if production:
            raise ImproperlyConfigured("POSTGRES_HOST is required in production")
        return {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": base_dir / "db.sqlite3",
            }
        }
    required = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ImproperlyConfigured(f"missing PostgreSQL settings: {', '.join(missing)}")
    return {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env["POSTGRES_DB"],
            "USER": env["POSTGRES_USER"],
            "PASSWORD": env["POSTGRES_PASSWORD"],
            "HOST": host,
            "PORT": env.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
