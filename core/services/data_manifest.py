"""Stable, database-independent checksums for durable business data."""

import json
from hashlib import sha256

from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder

from core.models import (
    AgentMailConfig,
    AppSetting,
    CheckRun,
    Invitation,
    MonitorTask,
    Notification,
)

MANIFEST_MODELS = (
    AppSetting,
    AgentMailConfig,
    get_user_model(),
    Invitation,
    MonitorTask,
    CheckRun,
    Notification,
)
MANIFEST_EXCLUDED_FIELDS = {
    get_user_model(): frozenset({"last_login"}),
}


def _model_digest(model):
    excluded = MANIFEST_EXCLUDED_FIELDS.get(model, frozenset())
    fields = tuple(
        field
        for field in model._meta.concrete_fields
        if not field.auto_created and field.name not in excluded
    )
    rows = [
        {field.name: field.value_from_object(instance) for field in fields}
        for instance in model._default_manager.order_by(model._meta.pk.name).iterator()
    ]
    payload = json.dumps(
        rows,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {"count": len(rows), "sha256": sha256(payload.encode("utf-8")).hexdigest()}


def build_data_manifest():
    """Return checksums for the durable models that must survive database migration."""
    return {
        "schema_version": 2,
        "models": {model._meta.label_lower: _model_digest(model) for model in MANIFEST_MODELS},
    }
