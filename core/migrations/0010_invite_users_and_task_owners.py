import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

LEGACY_OWNER_EMAIL = "850634546@qq.com"


def assign_legacy_owner(apps, schema_editor):
    User = apps.get_model("auth", "User")
    MonitorTask = apps.get_model("core", "MonitorTask")
    owner, _ = User.objects.get_or_create(
        username=LEGACY_OWNER_EMAIL,
        defaults={
            "email": LEGACY_OWNER_EMAIL,
            "password": "!ticketwatch-invite-required",
            "is_active": True,
            "is_staff": True,
            "is_superuser": True,
        },
    )
    changed = []
    if owner.email.casefold() != LEGACY_OWNER_EMAIL:
        owner.email = LEGACY_OWNER_EMAIL
        changed.append("email")
    if not owner.is_staff:
        owner.is_staff = True
        changed.append("is_staff")
    if not owner.is_superuser:
        owner.is_superuser = True
        changed.append("is_superuser")
    if changed:
        owner.save(update_fields=changed)
    MonitorTask.objects.filter(owner__isnull=True).update(owner=owner)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0009_agent_mail_verification_retry"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitortask",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="monitor_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(assign_legacy_owner, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="monitortask",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="monitor_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="monitortask",
            name="one_unfinished_task_per_target",
        ),
        migrations.AddConstraint(
            model_name="monitortask",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("status__in", ["MONITORING", "PAUSED", "DETECTED", "ERROR"])
                ),
                fields=(
                    "owner",
                    "city_id",
                    "movie_id",
                    "show_date",
                    "normalized_cinema_name",
                ),
                name="one_unfinished_task_per_user_target",
            ),
        ),
        migrations.CreateModel(
            name="Invitation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "accepted_by",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="accepted_invitation",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_invitations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
    ]
