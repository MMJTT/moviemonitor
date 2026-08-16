from django.contrib.staticfiles.management.commands.runserver import Command as RunserverCommand
from django.core.management.base import CommandError

from core.scheduler import LocalScheduler, set_process_scheduler


class Command(RunserverCommand):
    help = "Run the loopback web UI and one local scheduler"

    def handle(self, *args, **options):
        requested = options.get("addrport")
        if requested and requested not in {"127.0.0.1:8000", "localhost:8000"}:
            raise CommandError("runlocal only listens on 127.0.0.1:8000")
        options["addrport"] = "127.0.0.1:8000"
        options["use_ipv6"] = False
        options["use_reloader"] = False
        scheduler = LocalScheduler()
        set_process_scheduler(scheduler)
        try:
            scheduler.start()
            try:
                return super().handle(*args, **options)
            finally:
                scheduler.stop()
                scheduler.join(timeout=5)
        finally:
            set_process_scheduler(None)
