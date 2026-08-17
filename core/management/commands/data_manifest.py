import json
import os
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from core.services.data_manifest import build_data_manifest


class Command(BaseCommand):
    help = "Write a stable manifest for durable migration data."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        output_path = Path(options["output"])
        parent = output_path.parent
        if not parent.is_dir():
            raise CommandError("Output parent directory must already exist.")
        if output_path.is_dir():
            raise CommandError("Output path must be a file.")

        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            manifest_file = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with manifest_file:
                json.dump(
                    build_data_manifest(),
                    manifest_file,
                    cls=DjangoJSONEncoder,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                manifest_file.write("\n")
                manifest_file.flush()
                os.fsync(manifest_file.fileno())
            os.replace(temporary_path, output_path)
        except Exception as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise CommandError(f"Could not write manifest: {error}") from error

        self.stdout.write(str(output_path))
