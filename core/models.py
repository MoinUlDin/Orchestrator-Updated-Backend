# core.models.py
from django.db import models
from django.core.validators import MinLengthValidator
from django.utils import timezone
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.conf import settings
from django.contrib.postgres.fields import JSONField  # optional fallback if using old Django/Postgres
from django.core.serializers.json import DjangoJSONEncoder

# Create your models here.
User = settings.AUTH_USER_MODEL

class ProjectTemplate(models.Model):
    DB_TYPE_CHOICES = [
        ('postgres', 'PostgreSQL'),
        ('mysql', 'MySQL'),
        ('mongodb', 'MongoDB'),
    ]
    
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    base_domain = models.CharField(max_length=255)
    db_required = models.BooleanField(default=False)
    db_type = models.CharField(max_length=20, choices=DB_TYPE_CHOICES, default='postgres')
    notify_emails = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class ServiceTemplate(models.Model):
    SERVICE_TYPE_CHOICES = [
        ('backend', 'Backend Service'),
        ('frontend', 'Frontend Service'),
        ('db', 'Database'),
        ('worker', 'Worker'),
        ('cron', 'Cron Job'),
    ]
    
    project = models.ForeignKey(ProjectTemplate, 
                                on_delete=models.CASCADE, 
                                related_name='service_templates')
    name = models.CharField(max_length=100)
    service_type = models.CharField(max_length=20, choices=SERVICE_TYPE_CHOICES)
    repo_url = models.URLField(blank=True, null=True)
    repo_branch = models.CharField(max_length=100, blank=True, null=True)
    build_config = models.JSONField(default=dict)
    env_vars = models.JSONField(default=list, blank=True, null=True) 
    expose_domain = models.BooleanField(default=False)
    internal_provision_endpoint = models.CharField(max_length=255, blank=True, null=True)
    internal_provision_token_secret = models.CharField(max_length=255, blank=True, null=True)
    order = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.project.name} - {self.name}"


class Tenant(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('provisioning', 'Provisioning'),
        ('waiting_for_internal_provision', 'Waiting for Internal Provision'),
        ('running', 'Running'),
        ('failed', 'Failed'),
        ('completed', 'Completed'),
    ]
    
    project = models.ForeignKey(ProjectTemplate, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    client_ref = models.CharField(max_length=100, blank=True, null=True)
    subdomain = models.CharField(
        max_length=63, 
        validators=[MinLengthValidator(2)]
    )
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project','subdomain'], name='unique_subdomain_per_project')
        ]

    def __str__(self):
        return f"{self.project.name} - {self.name}"


class TenantService(models.Model):
    HEALTH_STATUS_CHOICES = [
        ('ok', 'OK'),
        ('unhealthy', 'Unhealthy'),
        ('unknown', 'Unknown'),
    ]
    
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='services')
    service_template = models.ForeignKey(ServiceTemplate, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    service_type = models.CharField(max_length=20, choices=ServiceTemplate.SERVICE_TYPE_CHOICES)
    repo_url = models.URLField(blank=True, null=True)
    repo_branch = models.CharField(max_length=100, blank=True, null=True)
    app_id = models.CharField(max_length=255, blank=True, null=True)  # Dokploy application ID
    db_id = models.CharField(max_length=255, blank=True, null=True)   # Dokploy database ID
    domain = models.CharField(max_length=255, blank=True, null=True)
    domain_id = models.CharField(max_length=255, blank=True, null=True)  # Dokploy domain ID
    
    # Idempotency flags
    created = models.BooleanField(default=False)
    git_attached = models.BooleanField(default=False)
    build_configured = models.BooleanField(default=False)
    env_configured = models.BooleanField(default=False)
    deploy_triggered = models.BooleanField(default=False)
    deployed = models.BooleanField(default=False)
    
    # Health monitoring
    health_status = models.CharField(max_length=10, choices=HEALTH_STATUS_CHOICES, default='unknown')
    health_tries = models.IntegerField(default=0)
    next_wait_seconds = models.IntegerField(default=30)
    
    last_deployed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    detail = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.tenant.name} - {self.name}"


class Deployment(models.Model):
    TRIGGER_REASON_CHOICES = [
        ('initial', 'Initial Deployment'),
        ('redeploy', 'Redeployment'),
        ('resume', 'Resume Deployment'),
        ('manual_fix', 'Manual Fix'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('failed', 'Failed'),
        ('succeeded', 'Succeeded'),
    ]
    
    tenant = models.ForeignKey(Tenant, related_name='deployment', on_delete=models.CASCADE)
    triggered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    trigger_reason = models.CharField(max_length=20, choices=TRIGGER_REASON_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Calculate duration if both started_at and ended_at are set
        if self.started_at and self.ended_at:
            self.duration_seconds = int((self.ended_at - self.started_at).total_seconds())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tenant.name} - {self.get_trigger_reason_display()} - {self.created_at}"


class DeploymentStep(models.Model):
    STEP_KEY_CHOICES = [
        ('project-create', 'Project Creation'),
        ('backend-create', 'Backend Creation'),
        ('backend-git-attach', 'Backend Git Attachment'),
        ('backend-build-config', 'Backend Build Configuration'),
        ('frontend-create', 'Frontend Creation'),
        ('frontend-git-attach', 'Frontend Git Attachment'),
        ('frontend-build-config', 'Frontend Build Configuration'),
        ('backend-service-env-set', 'Backend Environment Setup'),
        ('db-create', 'Database Creation'),
        ('db-deploy', 'Database Deployment'),
        ('backend-deploy', 'Backend Deployment'),
        ('frontend-deploy', 'Frontend Deployment'),
        ('backend-wait-deploy', 'Wait for Backend Deployment Completion'),
        ('frontend-wait-deploy', 'Wait for Frontend Deployment Completion'),
        ('backend-domains-create', 'Backend Domain Creation'),
        ('frontend-domains-create', 'Frontend Domain Creation'),
        ('domains-wait-propagation', 'Wait for Domains Propagation'),
        ('health-check', 'Health Check'),
        ('health-retry', 'Health Retry'),
        ('create-superuser', 'Create Superuser'),
        ('email-notify-success', 'Send Email Notification'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]
    
    deployment = models.ForeignKey(Deployment, on_delete=models.CASCADE, related_name='steps')
    tenant_service = models.ForeignKey(TenantService, on_delete=models.CASCADE, null=True, blank=True)
    step_key = models.CharField(max_length=50, choices=STEP_KEY_CHOICES)
    order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    attempts = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.deployment} - {self.get_step_key_display()}"


class JobRecord(models.Model):
    JOB_TYPE_CHOICES = [
        ('health', 'Health Check'),
        ('email', 'Email Notification'),
        ('retry', 'Retry Job'),
    ]
    
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    job_id = models.CharField(max_length=100)  # APScheduler job ID
    step = models.ForeignKey(DeploymentStep, on_delete=models.CASCADE, null=True, blank=True, related_name='job_records')
    deployment = models.ForeignKey(Deployment, on_delete=models.CASCADE, related_name='job_records')
    job_type = models.CharField(max_length=20, choices=JOB_TYPE_CHOICES)
    attempt = models.IntegerField(default=1)
    next_run_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    result_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.job_id} - {self.job_type}"


# Optional AuditEntry model (mentioned but not required for core)
class AuditEntry(models.Model):
    ACTION_CHOICES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('deploy', 'Deploy'),
        ('other', 'Other'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.user} - {self.action} - {self.model_name} - {self.created_at}"



class QueuedJob(models.Model):
    """
    DB-backed queued job (handoff from web/processes -> single scheduler process).
    The scheduler process will poll for jobs with status='pending' and next_run_at <= now()
    and then schedule them into APScheduler (or run them directly).
    """

    STATUS_PENDING = "pending"    # created, not yet scheduled/executed
    STATUS_SCHEDULED = "scheduled"  # persisted to apscheduler (job_id set)
    STATUS_RUNNING = "running"    # currently executing
    STATUS_SUCCEEDED = "succeeded"  # finished ok
    STATUS_FAILED = "failed"      # finished with error
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    # Basic metadata
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="queued_jobs")
    created_at = models.DateTimeField(auto_now_add=True)

    # task information
    task_name = models.CharField(max_length=255, db_index=True)
    # Args and kwargs stored as JSON. Use Django's JSONField (Postgres / modern Django)
    args = models.JSONField(default=list, encoder=DjangoJSONEncoder)   # list
    kwargs = models.JSONField(default=dict, encoder=DjangoJSONEncoder)  # dict

    # scheduling / execution
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)  # when to run
    priority = models.IntegerField(default=100, help_text="Lower numbers run earlier", db_index=True)

    # lifecycle
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=3)

    # if we create a job in APScheduler we can store that id here
    aps_job_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    # result / error
    result = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    last_error = models.TextField(null=True, blank=True)

    # runtime timestamps
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_run_at", "priority"]),
        ]
        ordering = ["priority", "next_run_at", "created_at"]

    def __str__(self):
        return f"QueuedJob(id={self.pk}, task={self.task_name}, status={self.status})"

    # --------------------------
    # Helper / convenience APIs
    # --------------------------
    @classmethod
    def enqueue(
        cls,
        task_name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        created_by: User = None,
        company=None,
        next_run_at: timezone.datetime | None = None,
        priority: int = 100,
        max_attempts: int = 3,
    ) -> "QueuedJob":
        """
        Create and return a QueuedJob in status 'pending'.
        Use this in your views to hand off work to the scheduler process.
        """
        job = cls.objects.create(
            task_name=task_name,
            args=args or [],
            kwargs=kwargs or {},
            created_by=created_by,
            next_run_at=next_run_at or timezone.now(),
            priority=priority,
            max_attempts=max_attempts,
            status=cls.STATUS_PENDING,
        )
        return job

    def mark_scheduled(self, aps_job_id: str | None = None):
        self.status = self.STATUS_SCHEDULED
        if aps_job_id:
            self.aps_job_id = aps_job_id
        self.save(update_fields=["status", "aps_job_id"])

    def mark_running(self):
        self.status = self.STATUS_RUNNING
        self.started_at = timezone.now()
        self.attempts = (self.attempts or 0) + 1
        self.save(update_fields=["status", "started_at", "attempts"])

    def mark_succeeded(self, result=None):
        self.status = self.STATUS_SUCCEEDED
        self.result = result
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "result", "finished_at"])

    def mark_failed(self, error_text: str = "", allow_retry: bool = True):
        self.last_error = (self.last_error or "") + f"\n[{timezone.now().isoformat()}] {error_text}"
        self.finished_at = timezone.now()
        if allow_retry and (self.attempts or 0) < (self.max_attempts or 0):
            # reschedule based on an exponential or fixed backoff; here simple 30s * attempts
            backoff_seconds = 30 * max(1, (self.attempts or 1))
            self.next_run_at = timezone.now() + timezone.timedelta(seconds=backoff_seconds)
            self.status = self.STATUS_PENDING
            self.save(update_fields=["last_error", "finished_at", "next_run_at", "status"])
        else:
            self.status = self.STATUS_FAILED
            self.save(update_fields=["last_error", "finished_at", "status"])

    def cancel(self):
        self.status = self.STATUS_CANCELLED
        self.save(update_fields=["status"])