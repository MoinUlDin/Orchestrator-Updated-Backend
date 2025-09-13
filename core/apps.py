from django.apps import AppConfig
import os
import logging
logger = logging.getLogger(__name__)

class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    def ready(self):
        # # only start scheduler when RUN_SCHEDULER_IN_APP is True (set in settings for dev convenience).
        # from django.conf import settings
        # if not getattr(settings, "RUN_SCHEDULER_IN_APP", False):
        #     return

        # Avoid starting twice when using Django autoreloader (runserver)
        # For runserver the child process sets the env var "RUN_MAIN" or "WERKZEUG_RUN_MAIN"
        # Only start scheduler when RUN_MAIN is 'true' or not present (for gunicorn you'll likely disable RUN_SCHEDULER_IN_APP)
        if os.environ.get("RUN_MAIN") not in (None, "true", "1"):
            # this is likely the reloader parent process; skip
            logger.debug("Skipping scheduler start due to RUN_MAIN=%s", os.environ.get("RUN_MAIN"))
            print('Skipping now')
            return

        try:
            from .scheduler import start_scheduler
            start_scheduler()
            logger.info("Provisioner scheduler started from AppConfig.ready()")
            print("\n === Provisioner scheduler started from AppConfig.ready() ===\n")
        except Exception:
            logger.exception("Failed to start scheduler in AppConfig.ready()")
