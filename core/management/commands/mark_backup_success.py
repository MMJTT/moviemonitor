from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import RuntimeState


class Command(BaseCommand):
    help = "Record a completed PostgreSQL backup in runtime status."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        name = options["name"]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise CommandError("Backup name must be a file name, not a path.")
        RuntimeState.objects.update_or_create(
            pk=1,
            defaults={"last_backup_at": timezone.now(), "last_backup_name": name},
        )
        self.stdout.write("Backup status recorded.")
