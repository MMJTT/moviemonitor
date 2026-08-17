import signal

from django.core.management.base import BaseCommand

from core.worker import WorkerLoop


class Command(BaseCommand):
    help = "Run the standalone ticket monitoring worker"

    def handle(self, *args, **options):
        loop = WorkerLoop()

        def stop_loop(signum, frame):
            loop.stop()

        signal.signal(signal.SIGTERM, stop_loop)
        signal.signal(signal.SIGINT, stop_loop)
        loop.run_forever()
