# core/scheduler.py
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    executors={"default": ThreadPoolExecutor(max_workers=getattr(settings, "SCHEDULER_MAX_WORKERS", 5))},
    job_defaults={"coalesce": False, "max_instances": 1},
    timezone=timezone.get_default_timezone(),
)

_started = False

def start_scheduler():
    global _started
    if _started:
        logger.debug("Scheduler already started")
        return
    scheduler.start(paused=False)
    _started = True
    logger.info("Provisioner scheduler started")

def add_job(func, trigger="date", run_date=None, args=None, kwargs=None, id=None, replace_existing=True, **aps_kwargs):
    job = scheduler.add_job(
        func=func,
        trigger=trigger,
        run_date=run_date,
        args=args or [],
        kwargs=kwargs or {},
        id=id,
        replace_existing=replace_existing,
        **aps_kwargs,
    )
    logger.info("Scheduled job id=%s trigger=%s run_date=%s", id, trigger, run_date)
    return job

def remove_job(job_id: str):
    scheduler.remove_job(job_id)
    logger.info("Removed job %s", job_id)

def get_job(job_id: str):
    return scheduler.get_job(job_id)
