# core/management/commands/run_scheduler.py
import time
import logging
import signal
import importlib
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.models import QueuedJob

logger = logging.getLogger(__name__)


# how many jobs to claim each loop
BATCH_SIZE = 10
# maximum worker threads
MAX_WORKERS = 6
# how long to sleep when no jobs
IDLE_SLEEP_SECONDS = 2.0
# how long to sleep after discovering some jobs (lets loop quickly)
BUSY_SLEEP_SECONDS = 0.1


class GracefulExit(SystemExit):
    pass


def _resolve_task(fn_path: str):
    """
    Resolve a dotted path like 'myapp.tasks.do_work' -> callable
    """
    module_name, _, func_name = fn_path.rpartition(".")
    if not module_name:
        raise ImportError(f"Invalid task path: {fn_path}")
    module = importlib.import_module(module_name)
    func = getattr(module, func_name)
    return func


def _run_job(job_id: int):
    """
    Run single job (used within a worker thread). This does DB updates itself.
    """
    try:
        job = QueuedJob.objects.get(pk=job_id)
    except QueuedJob.DoesNotExist:
        logger.warning("QueuedJob %s disappeared before execution", job_id)
        return

    # mark running (increments attempts)
    job.mark_running()
    logger.info("Running job %s -> %s", job.pk, job.task_name)

    try:
        func = _resolve_task(job.task_name)
    except Exception as exc:
        job.mark_failed(f"Task import error: {exc}", allow_retry=False)
        logger.exception("Failed to import task for job %s", job.pk)
        return

    try:
        # Execute callable - expect it may return JSON-serializable result or None
        result = func(*job.args, **job.kwargs)
        # Save success
        job.mark_succeeded(result=result)
        logger.info("Job %s succeeded", job.pk)
    except Exception as exc:
        # Mark failed (may reschedule based on attempts)
        logger.exception("Job %s raised exception", job.pk)
        job.mark_failed(str(exc), allow_retry=True)


class Command(BaseCommand):
    help = "Run the DB-backed scheduler worker (poll QueuedJob and execute)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Claim and run one batch then exit")

    def handle(self, *args, **options):
        # Allow graceful shutdown via SIGTERM/SIGINT
        stop_requested = {"flag": False}

        def _signal_handler(signum, frame):
            logger.info("Scheduler received signal %s, will stop after current jobs", signum)
            stop_requested["flag"] = True

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        self.stdout.write("Starting scheduler worker...")
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

        try:
            while True:
                if stop_requested["flag"]:
                    logger.info("Stop requested, exiting main loop")
                    break

                now = timezone.now()

                # Claim a batch of pending jobs ready to run using select_for_update(skip_locked=True)
                claimed_job_ids = []
                with transaction.atomic():
                    qs = (
                        QueuedJob.objects.select_for_update(skip_locked=True)
                        .filter(status=QueuedJob.STATUS_PENDING, next_run_at__lte=now)
                        .order_by("priority", "next_run_at")[:BATCH_SIZE]
                    )
                    # copy ids to avoid evaluation while locked (we hold lock until transaction exits)
                    for j in qs:
                        claimed_job_ids.append(j.pk)
                        # mark as scheduled immediately so other schedulers see it's not pending
                        j.mark_scheduled()

                if not claimed_job_ids:
                    # nothing to do
                    if options["once"]:
                        break
                    time.sleep(IDLE_SLEEP_SECONDS)
                    continue

                # Submit each as a worker future
                futures = []
                for jid in claimed_job_ids:
                    futures.append(executor.submit(_run_job, jid))

                # Wait for completed futures but don't block new loop entirely; we simply ensure exceptions are logged
                for f in as_completed(futures):
                    try:
                        _ = f.result()
                    except Exception:
                        logger.exception("Worker raised exception")

                # If --once requested exit after one batch
                if options["once"]:
                    break

                # small busy sleep then loop again
                time.sleep(BUSY_SLEEP_SECONDS)

        except GracefulExit:
            logger.info("Graceful exit requested")
        finally:
            logger.info("Shutting down executor")
            executor.shutdown(wait=True)
            self.stdout.write(self.style.SUCCESS("Scheduler worker stopped"))
