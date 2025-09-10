# views.py (updated)
import re
import logging

from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from django.contrib.auth import get_user_model

from .models import (
    ProjectTemplate, IntegrationSecret, ServiceTemplate,
    Tenant, TenantService, Deployment, DeploymentStep,
    JobRecord, AuditEntry
)

from .serializers import (
    ProjectTemplateSerializer, IntegrationSecretSerializer,
    ServiceTemplateSerializer, ServiceTemplateDetailSerializer,
    TenantSerializer, TenantDetailSerializer, TenantCreateSerializer,
    TenantServiceSerializer, TenantServiceDetailSerializer, TenantServiceUpdateSerializer,
    DeploymentSerializer, DeploymentDetailSerializer, DeploymentCreateSerializer,
    DeploymentStepSerializer, DeploymentResumeSerializer, DeploymentStatusUpdateSerializer,
    StepStatusUpdateSerializer, JobRecordSerializer, JobRecordDetailSerializer,
    AuditEntrySerializer, AuditEntryDetailSerializer, HealthCheckSerializer,
    NotificationSerializer
)

User = get_user_model()
logger = logging.getLogger(__name__)


def sanitize_subdomain(value: str) -> str:
    """
    Lowercase, allow only a-z0-9 and hyphen. Replace invalid chars with hyphen.
    Collapse multiple hyphens, trim leading/trailing hyphens and cap to 63 chars.
    """
    if not value:
        return ""
    s = value.strip().lower()
    # replace invalid chars with hyphen
    s = re.sub(r"[^a-z0-9-]", "-", s)
    # collapse multiple hyphens
    s = re.sub(r"-{2,}", "-", s)
    # trim hyphens
    s = s.strip("-")
    return s[:63]


class ProjectTemplateViewSet(viewsets.ModelViewSet):
    queryset = ProjectTemplate.objects.filter(active=True)
    permission_classes = [IsAuthenticated]
    serializer_class = ProjectTemplateSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class IntegrationSecretViewSet(viewsets.ModelViewSet):
    queryset = IntegrationSecret.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = IntegrationSecretSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class ServiceTemplateViewSet(viewsets.ModelViewSet):
    queryset = ServiceTemplate.objects.filter(active=True)
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ServiceTemplateDetailSerializer
        return ServiceTemplateSerializer

    def get_queryset(self):
        # Filter by project if project_id is provided in query params
        project_id = self.request.query_params.get('project_id')
        if project_id:
            return ServiceTemplate.objects.filter(project_id=project_id, active=True)
        return ServiceTemplate.objects.filter(active=True)


class TenantViewSet(viewsets.ModelViewSet):
    """
    Handles Tenant creation. Important notes:
      - TenantCreateSerializer.create() is responsible for creating TenantService rows
        when `services` payload is provided. We avoid duplicating that logic here.
      - After creating Tenant + TenantServices we create a Deployment and DeploymentSteps
        inside a DB transaction to keep state consistent.
      - Long-running deployment work should be scheduled to a background worker (Celery / APScheduler).
    """
    queryset = Tenant.objects.filter(active=True)
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TenantDetailSerializer
        elif self.action == 'create':
            return TenantCreateSerializer
        return TenantSerializer

    def get_queryset(self):
        project_id = self.request.query_params.get('project_id')
        if project_id:
            return Tenant.objects.filter(project_id=project_id, active=True)
        return Tenant.objects.filter(active=True)

    def perform_create(self, serializer):
        # serializer is already validated by DRF at this point
        validated = getattr(serializer, "validated_data", None)
        if not validated:
            # Shouldn't happen normally; safeguard
            raise ValidationError({"detail": "Invalid tenant payload."})

        project = validated.get("project")
        subdomain_raw = validated.get("subdomain", "")
        subdomain = sanitize_subdomain(subdomain_raw)
        if not subdomain:
            raise ValidationError({"subdomain": "Invalid subdomain (letters, numbers and hyphen allowed; 2-63 chars)."})

        # Ensure uniqueness scoped to project
        if Tenant.objects.filter(project=project, subdomain=subdomain).exists():
            raise ValidationError({"subdomain": "Subdomain already exists for this project."})

        # Create tenant, services, deployment and steps atomically
        with transaction.atomic():
            tenant = serializer.save(created_by=self.request.user, subdomain=subdomain)

            # At this point TenantCreateSerializer.create() should have created TenantService objects
            # If you prefer the view to create them instead, move that logic here and remove it from serializer.

            # Create initial deployment
            deployment = Deployment.objects.create(
                tenant=tenant,
                triggered_by=self.request.user,
                trigger_reason='initial',
                status='pending'
            )

            # Create deployment steps
            self._create_deployment_steps(deployment, tenant)

            # Enqueue background worker to process deployment (do not run in the request thread)
            # Example placeholder: enqueue_deployment(deployment.id)
            # Implement actual enqueueing using Celery/APS cheduler.
            logger.info("Tenant %s created and deployment %s enqueued (implement enqueue).", tenant.id, deployment.id)

    def _create_deployment_steps(self, deployment: Deployment, tenant: Tenant):
        steps_data = []
        order = 1

        # Project creation
        steps_data.append({
            'deployment': deployment,
            'step_key': 'project.create',
            'order': order,
            'status': 'pending'
        })
        order += 1

        # Service creation/configuration steps (generic step keys)
        for service in tenant.services.all():
            steps_data.append({
                'deployment': deployment,
                'tenant_service': service,
                'step_key': 'service.create',
                'order': order,
                'status': 'pending'
            })
            order += 1

            steps_data.append({
                'deployment': deployment,
                'tenant_service': service,
                'step_key': 'service.git_attach',
                'order': order,
                'status': 'pending'
            })
            order += 1

            steps_data.append({
                'deployment': deployment,
                'tenant_service': service,
                'step_key': 'service.build_config',
                'order': order,
                'status': 'pending'
            })
            order += 1

            if service.service_type == 'backend':
                steps_data.append({
                    'deployment': deployment,
                    'tenant_service': service,
                    'step_key': 'service.env_set',
                    'order': order,
                    'status': 'pending'
                })
                order += 1

        # DB steps (if project requires DB)
        if tenant.project.db_required:
            steps_data.append({
                'deployment': deployment,
                'step_key': 'db.create',
                'order': order,
                'status': 'pending'
            })
            order += 1

            steps_data.append({
                'deployment': deployment,
                'step_key': 'db.deploy',
                'order': order,
                'status': 'pending'
            })
            order += 1

        # Service deploy steps
        for service in tenant.services.all():
            steps_data.append({
                'deployment': deployment,
                'tenant_service': service,
                'step_key': 'service.deploy',
                'order': order,
                'status': 'pending'
            })
            order += 1

            steps_data.append({
                'deployment': deployment,
                'tenant_service': service,
                'step_key': 'service.wait_deploy',
                'order': order,
                'status': 'pending'
            })
            order += 1

        # Domain creation and propagation
        steps_data.append({
            'deployment': deployment,
            'step_key': 'domains.create',
            'order': order,
            'status': 'pending'
        })
        order += 1

        steps_data.append({
            'deployment': deployment,
            'step_key': 'domains.wait_propagation',
            'order': order,
            'status': 'pending'
        })
        order += 1

        # Health checks
        for service in tenant.services.all():
            steps_data.append({
                'deployment': deployment,
                'tenant_service': service,
                'step_key': 'health.check',
                'order': order,
                'status': 'pending'
            })
            order += 1

        # Internal provision
        steps_data.append({
            'deployment': deployment,
            'step_key': 'internal.provision',
            'order': order,
            'status': 'pending'
        })
        order += 1

        # Notification (last)
        steps_data.append({
            'deployment': deployment,
            'step_key': 'email.notify_success',
            'order': order,
            'status': 'pending'
        })

        # Persist steps
        for step_data in steps_data:
            DeploymentStep.objects.create(**step_data)

    @action(detail=True, methods=['post'])
    def redeploy(self, request, pk=None):
        tenant = self.get_object()

        with transaction.atomic():
            deployment = Deployment.objects.create(
                tenant=tenant,
                triggered_by=request.user,
                trigger_reason='redeploy',
                status='pending'
            )
            self._create_deployment_steps(deployment, tenant)

            # enqueue background processing for this deployment
            logger.info("Redeploy requested for tenant=%s deployment=%s", tenant.id, deployment.id)

        return Response({'message': 'Redeploy scheduled', 'deployment_id': deployment.id}, status=status.HTTP_202_ACCEPTED)


class TenantServiceViewSet(viewsets.ModelViewSet):
    queryset = TenantService.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TenantServiceDetailSerializer
        elif self.action in ['update', 'partial_update']:
            return TenantServiceUpdateSerializer
        return TenantServiceSerializer

    def get_queryset(self):
        tenant_id = self.request.query_params.get('tenant_id')
        if tenant_id:
            return TenantService.objects.filter(tenant_id=tenant_id)
        return TenantService.objects.all()


class DeploymentViewSet(viewsets.ModelViewSet):
    queryset = Deployment.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return DeploymentDetailSerializer
        elif self.action == 'create':
            return DeploymentCreateSerializer
        return DeploymentSerializer

    def get_queryset(self):
        tenant_id = self.request.query_params.get('tenant_id')
        if tenant_id:
            return Deployment.objects.filter(tenant_id=tenant_id)
        return Deployment.objects.all()

    def perform_create(self, serializer):
        serializer.save(triggered_by=self.request.user)

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        deployment = self.get_object()
        serializer = DeploymentResumeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # mark deployment as running and set start time if not set
        deployment.status = 'running'
        if not deployment.started_at:
            deployment.started_at = timezone.now()
        deployment.save()

        resume_from_step = serializer.validated_data.get('resume_from_step')
        if resume_from_step:
            steps = deployment.steps.order_by('order')
            found = False
            for step in steps:
                if step.step_key == resume_from_step:
                    found = True
                if found and step.status in ['failed', 'pending']:
                    step.status = 'pending'
                    step.save()
        else:
            # mark all failed steps as pending
            deployment.steps.filter(status='failed').update(status='pending')

        # enqueue background worker to resume processing
        logger.info("Deployment %s resumed by %s", deployment.id, request.user)

        return Response({'message': 'Deployment resume requested', 'deployment_id': deployment.id}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        deployment = self.get_object()
        serializer = DeploymentStatusUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        deployment.status = serializer.validated_data['status']
        if deployment.status in ['failed', 'succeeded']:
            deployment.ended_at = timezone.now()
            if not deployment.started_at:
                deployment.started_at = deployment.created_at
            deployment.duration_seconds = int((deployment.ended_at - deployment.started_at).total_seconds())
        deployment.save()

        return Response(DeploymentSerializer(deployment).data)


class DeploymentStepViewSet(viewsets.ModelViewSet):
    queryset = DeploymentStep.objects.all()
    permission_classes = [IsAuthenticated]
    serializer_class = DeploymentStepSerializer

    def get_queryset(self):
        deployment_id = self.request.query_params.get('deployment_id')
        if deployment_id:
            return DeploymentStep.objects.filter(deployment_id=deployment_id).order_by('order')
        return DeploymentStep.objects.all()

    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        step = self.get_object()
        serializer = StepStatusUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        step.status = data['status']
        if data.get('message') is not None:
            step.message = data['message']
        if data.get('meta') is not None:
            step.meta = data.get('meta')

        # update timestamps and attempts
        if step.status == 'running' and not step.started_at:
            step.started_at = timezone.now()
        if step.status in ['success', 'failed', 'skipped']:
            step.ended_at = timezone.now()
            step.attempts = step.attempts + 1

        step.save()
        return Response(DeploymentStepSerializer(step).data)


class JobRecordViewSet(viewsets.ModelViewSet):
    queryset = JobRecord.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return JobRecordDetailSerializer
        return JobRecordSerializer

    def get_queryset(self):
        deployment_id = self.request.query_params.get('deployment_id')
        if deployment_id:
            return JobRecord.objects.filter(deployment_id=deployment_id)
        return JobRecord.objects.all()


class AuditEntryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditEntry.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AuditEntryDetailSerializer
        return AuditEntrySerializer

    def get_queryset(self):
        user_id = self.request.query_params.get('user_id')
        if user_id:
            return AuditEntry.objects.filter(user_id=user_id)
        return AuditEntry.objects.all()


class HealthCheckViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def create(self, request):
        serializer = HealthCheckSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        tenant_service = get_object_or_404(TenantService, id=data['tenant_service_id'])

        tenant_service.health_status = data['status']
        if data.get('detail'):
            tenant_service.detail = data['detail']
        tenant_service.save()

        return Response({'status': 'Health status updated'})


class NotificationViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def create(self, request):
        serializer = NotificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        deployment = get_object_or_404(Deployment, id=data['deployment_id'])

        if data.get('message'):
            deployment.summary = data['message']
            deployment.save()

        step_key = 'email.notify_success' if data['success'] else 'email.notify_failure'

        DeploymentStep.objects.create(
            deployment=deployment,
            step_key=step_key,
            order=9999,
            status='success' if data['success'] else 'failed',
            message=data.get('message', ''),
            started_at=timezone.now(),
            ended_at=timezone.now()
        )

        return Response({'status': 'Notification processed'})
