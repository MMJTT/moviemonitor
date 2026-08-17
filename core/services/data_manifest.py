"""Stable, database-independent checksums for durable business data."""

import json
from hashlib import sha256

from django.core.serializers.json import DjangoJSONEncoder

from core.models import AgentMailConfig, AppSetting, CheckRun, MonitorTask, Notification

MANIFEST_MODELS = (
    AppSetting,
    AgentMailConfig,
    MonitorTask,
    CheckRun,
    Notification,
)


def _model_digest(model):
    fields = tuple(
        field for field in model._meta.concrete_fields if not field.auto_created
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
        "schema_version": 1,
        "models": {model._meta.label_lower: _model_digest(model) for model in MANIFEST_MODELS},
    }
