# views.py (updated)
import re
import logging

from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Count, Q, Prefetch
from rest_framework import status, viewsets
import rest_framework
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from django.db import transaction, IntegrityError
import traceback
import copy
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
    NotificationSerializer, ProjectTemplateDetailSerializer
)
from .scheduler import add_job
from .tasks import main_deployment_function
from django.utils import timezone

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
    
    lookup_field = "slug"
    lookup_value_regex = r"[-a-zA-Z0-9_]+"

    def get_serializer_class(self):
        # Return the detailed serializer for retrieve to include nested services
        if self.action == "retrieve":
            return ProjectTemplateDetailSerializer
        return ProjectTemplateSerializer
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    
    @action(detail=True, methods=["get"], url_path="fetch_tenant_details", permission_classes=[IsAuthenticated])
    def fetch_tenant_details(self, request, slug=None):
        """
        Return project detail + tenants (serialized) + tenant service instances.
        Quick-stats (total_instances / running / deploying / stopped) are computed
        from Tenant.status (NOT TenantService).
        """
        project = self.get_object()
        if not project:
            return Response({"detail": 'no project found for the given slug'}, status=status.HTTP_400_BAD_REQUEST)

        project_serializer = ProjectTemplateSerializer(project)
        # --- TENANTS (serialized) ---
        tenants_qs = Tenant.objects.filter(project=project).only(
            "id", "name", "subdomain", "status", "created_at", "updated_at"
        ).order_by("created_at")
        tenants_serialized = TenantDetailSerializer(tenants_qs, many=True)

        # --- QUICK STATS (based on tenant.status) ---
        total_tenants = tenants_qs.count()

        # Mapping: adjust these groups if you'd like different semantics
        running = tenants_qs.filter(status="running").count()
        # consider 'pending' and provisioning states as 'deploying' (i.e. not yet running)
        deploying = tenants_qs.filter(
            status__in=["pending", "provisioning", "waiting_for_internal_provision"]
        ).count()
        # treat failed/completed as stopped (adjust if you prefer completed != stopped)
        stopped = tenants_qs.filter(status__in=["failed", "completed"]).count()

        # --- INSTANCES (tenant services) ---
        tenant_services_qs = (
            TenantService.objects.filter(tenant__project=project)
            .select_related("tenant", "service_template")
        )

        instances = []
        for ts in tenant_services_qs:
            instance_id = (
                getattr(ts, "app_id", None)
                or getattr(ts, "instance_id", None)
                or getattr(ts, "name", None)
                or f"{ts.tenant.subdomain}-{getattr(ts, 'service_type', 'service')}"
            )

            environment = getattr(ts.tenant, "environment", None) or getattr(ts, "environment", None)
            if not environment:
                tname = (ts.tenant.name or "").lower()
                if "prod" in tname or "production" in tname:
                    environment = "Production"
                elif "stage" in tname:
                    environment = "Staging"
                elif "dev" in tname:
                    environment = "Development"
                else:
                    environment = (getattr(ts, "service_type", None) or "Unknown").capitalize()

            # per-instance status (use different name so we don't shadow imported 'status')
            if getattr(ts, "deployed", False) or getattr(ts, "last_deployed_at", None):
                inst_status = "Running"
            elif getattr(ts, "deploy_triggered", False) or (getattr(ts, "health_status", "") or "").lower().startswith("deploy"):
                inst_status = "Deploying"
            else:
                inst_status = "Stopped"

            version = getattr(ts, "version", None) or getattr(ts, "app_version", None)
            meta = getattr(ts, "meta", None) or getattr(ts, "detail", None) or {}
            if not version and isinstance(meta, dict):
                version = meta.get("version") or meta.get("app_version")

            url = getattr(ts, "domain", None) or getattr(ts, "domain_name", None) or None
            resources = getattr(ts, "resources", None)
            deployed_at = getattr(ts, "last_deployed_at", None)

            instances.append(
                {
                    "id": ts.id,
                    "instance_id": instance_id,
                    "environment": environment,
                    "status": inst_status,
                    "version": version,
                    "url": url,
                    "resources": resources,
                    "deployed_at": deployed_at,
                    "tenant": {
                        "id": ts.tenant.id,
                        "name": ts.tenant.name,
                        "subdomain": ts.tenant.subdomain,
                        "status": ts.tenant.status,
                    },
                }
            )

        instances = sorted(instances, key=lambda i: (i["environment"], str(i["instance_id"])))

        payload = {
            'project': project_serializer.data,
            "tenants": tenants_serialized.data,
            "last_deployment": max((i["deployed_at"] for i in instances if i["deployed_at"]), default=None),
            "quick_stats": {
                "total_instances": total_tenants,
                "running": running,
                "deploying": deploying,
                "stopped": stopped,
            },
            "instances": instances,
        }

        return Response(payload, status=status.HTTP_200_OK)


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
    
    @action(detail=False, methods=['post'], url_path='bulk_create')
    def bulk_create(self, request):
        """
        Bulk-create multiple ServiceTemplate objects in one request.
        Normalizes envs found under build_config.env -> env_vars for serializer.
        """
        data = request.data
        print(f'\n Data we got {data}\n')

        # Require an array
        if not isinstance(data, list):
            return Response({"detail": "Expected a list/array of service objects."},
                            status=status.HTTP_400_BAD_REQUEST)

        # If frontend provided a single project_id as query param, inject it into each item
        project_id = request.query_params.get('project_id')

        # Normalize and defensively prepare list
        normalized = []
        for raw_item in data:
            item = copy.deepcopy(raw_item)  # avoid mutating original
            if project_id and not item.get("project"):
                item["project"] = project_id

            # 1) If the frontend sends `build_config.env` (common), move it to top-level `env_vars`
            build_cfg = item.get("build_config") or {}
            env_from_build = None
            if isinstance(build_cfg, dict):
                env_from_build = build_cfg.pop("env", None)
                # if build_config had env, remove it to avoid double-handling
                if env_from_build is not None:
                    item["build_config"] = build_cfg  # updated (with env removed)
                    # normalize shape: list of {name, value}
                    if isinstance(env_from_build, list):
                        item["env_vars"] = env_from_build
                    else:
                        # if frontend gave a dict, convert to list of {name,value}
                        if isinstance(env_from_build, dict):
                            item["env_vars"] = [{"name": k, "value": v} for k, v in env_from_build.items()]
                        else:
                            # unknown shape: ignore or set empty
                            item["env_vars"] = []

            # 2) Accept old key `build_env` or `env_vars` if present: prefer explicit env_vars
            if "build_env" in item and "env_vars" not in item:
                # build_env maybe a list or dict — normalize to env_vars (list of {name, value})
                be = item.pop("build_env")
                if isinstance(be, list):
                    item["env_vars"] = be
                elif isinstance(be, dict):
                    item["env_vars"] = [{"name": k, "value": v} for k, v in be.items()]
                else:
                    item["env_vars"] = []

            normalized.append(item)

        serializer = ServiceTemplateSerializer(data=normalized, many=True, context={'request': request})
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as exc:
            # return the serializer errors for debugging
            return Response({"detail": "validation_error", "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                created = serializer.save()
                resp_serializer = ServiceTemplateSerializer(created, many=True, context={'request': request})
                return Response(resp_serializer.data, status=status.HTTP_201_CREATED)
        except IntegrityError as e:
            return Response({"detail": "Database integrity error", "error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            # log full traceback server-side for debugging
            tb = traceback.format_exc()
            logger.exception("Unexpected error in bulk_create: %s", tb)
            return Response({"detail": "Unexpected error creating service templates", "error": str(e), "trace": tb}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        

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

    def create(self, request, *args, **kwargs):
        # Validate the request data
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        
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

            # Clone service templates into TenantService
            for st in tenant.project.service_templates.all():
                TenantService.objects.create(
                    tenant=tenant,
                    service_template=st,
                    name=st.name,
                    service_type=st.service_type,
                    repo_url=st.repo_url,
                    repo_branch=st.repo_branch,
                )

            # Create initial deployment
            deployment = Deployment.objects.create(
                tenant=tenant,
                triggered_by=self.request.user,
                trigger_reason='initial',
                status='pending'
            )
            self._create_deployment_steps(deployment=deployment, tenant=tenant)
            
            # Schedule the deployment job
            try:
                job_id = f"deployment_{deployment.id}"
                add_job(
                    func=main_deployment_function,
                    trigger="date",
                    run_date=timezone.now(),  # schedule immediately
                    args=[deployment.id],
                    id=job_id,
                    replace_existing=True,
                    max_instances=1,
                )
                logger.info("Tenant %s created and deployment %s scheduled (job_id=%s).", tenant.id, deployment.id, job_id)
                # store job_id in meta for traceability
                deployment.meta = deployment.meta or {}
                deployment.meta['scheduler_job_id'] = job_id
                deployment.save(update_fields=['meta'])
            except Exception as e:
                # Scheduling failed; log and leave deployment in pending so it can be resumed later
                logger.exception("Failed to schedule deployment %s for tenant %s: %s", deployment.id, tenant.id, e)
        
        # Get the serialized data for the response
        headers = self.get_success_headers(serializer.data)
        
        # Add deployment_id to the response data
        response_data = serializer.data
        response_data['deployment_id'] = deployment.id
        
        return Response(response_data, status=status.HTTP_201_CREATED, headers=headers)
            
    def _create_deployment_steps(self, deployment: Deployment, tenant: Tenant):
        steps_data = []
        order = 1
        print("\n Creating Steps Project\n")
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

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        deployment = self.get_object()

        # if already succeeded, nothing to do
        if deployment.status == "succeeded":
            return Response({"detail": "Deployment already succeeded."}, status=status.HTTP_400_BAD_REQUEST)

        # schedule the job to run immediately (replace existing job if any)
        job_id = f"deployment_{deployment.id}"
        add_job(
            func=main_deployment_function,
            trigger="date",
            run_date=timezone.now(),
            args=[deployment.id],
            id=job_id,
            replace_existing=True,
            max_instances=1,
        )

        # update status to pending/running based on your convention
        deployment.status = "pending"
        deployment.save(update_fields=["status", "updated_at"])

        return Response({"detail": "Resume scheduled", "job_id": job_id}, status=status.HTTP_202_ACCEPTED)
    
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
    
    @action(detail=True, methods=['get'], url_path='logs')
    def logs(self, request, pk=None):
        """
        Return compact chronological logs for the given deployment.
        Useful for UI to show what happened and which steps completed.
        """
        deployment = self.get_object()
        meta = deployment.meta or {}
        logs = meta.get('logs', [])
        # Optionally you may want to limit how many logs are returned; for now return all
        return Response({"deployment_id": deployment.id, "logs": logs})


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
