from django.db import models
from django.core.validators import MinLengthValidator
from django.utils import timezone
from encrypted_model_fields.fields import EncryptedTextField
from django.conf import settings

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


class IntegrationSecret(models.Model):
    SECRET_TYPE_CHOICES = [
        ('internal_provision_token', 'Internal Provision Token'),
        ('dokploy_api', 'Dokploy API Key'),
        ('other', 'Other'),
    ]
    
    name = models.CharField(max_length=100)
    secret_type = models.CharField(max_length=30, choices=SECRET_TYPE_CHOICES)
    encrypted_value = EncryptedTextField()
    project = models.ForeignKey(ProjectTemplate, related_name='secrets', on_delete=models.CASCADE, null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    last_rotated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.secret_type})"


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
    internal_provision_token_secret = models.ForeignKey(
        IntegrationSecret, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
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
    
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
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
        ('project.create', 'Project Creation'),
        ('service.create', 'Service Creation'),
        ('service.git_attach', 'Git Attachment'),
        ('service.build_config', 'Build Configuration'),
        ('service.env_set', 'Environment Setup'),
        ('db.create', 'Database Creation'),
        ('db.deploy', 'Database Deployment'),
        ('service.deploy', 'Service Deployment'),
        ('service.wait_deploy', 'Wait for Deployment'),
        ('domains.create', 'Domain Creation'),
        ('domains.wait_propagation', 'Domain Propagation'),
        ('health.check', 'Health Check'),
        ('health.retry', 'Health Retry'),
        ('internal.provision', 'Internal Provisioning'),
        ('email.notify_success', 'Success Notification'),
        ('email.notify_failure', 'Failure Notification'),
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