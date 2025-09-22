# core/management/commands/runapscheduler.py
import time
import logging
import os
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Start the APScheduler provisioner (long running)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-block",
            action="store_true",
            help="Start scheduler and exit (do not block).",
        )

    def handle(self, *args, **options):
        # Export env var for other processes started from same shell if you want them to detect
        # that scheduler is managed externally (optional)
        os.environ.setdefault("RUNNING_EXTERNAL_SCHEDULER", "1")

        try:
            # import your start_scheduler function
            from core.scheduler import start_scheduler
        except Exception as exc:
            logger.exception("Failed to import start_scheduler: %s", exc)
            raise

        logger.info("Starting scheduler via management command...")
        try:
            start_scheduler()
        except Exception:
            logger.exception("start_scheduler() raised an exception")
            raise

        self.stdout.write(self.style.SUCCESS("Scheduler started."))

        if not options.get("no_block"):
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                logger.info("runapscheduler interrupted by KeyboardInterrupt, exiting")
                self.stdout.write("Stopping scheduler (KeyboardInterrupt)")
