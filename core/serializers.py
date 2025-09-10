# core.serializers.py
from rest_framework import serializers
from .models import (
    ProjectTemplate, IntegrationSecret, ServiceTemplate, 
    Tenant, TenantService, Deployment, DeploymentStep, 
    JobRecord, AuditEntry
)

import re
from django.contrib.auth import get_user_model
User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'role', 'profile_picture']
        read_only_fields = ['id', 'email', 'role']


class ProjectTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectTemplate
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class IntegrationSecretSerializer(serializers.ModelSerializer):
    # Make encrypted_value write-only for security
    encrypted_value = serializers.CharField(write_only=True)
    
    class Meta:
        model = IntegrationSecret
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ServiceTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceTemplate
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ServiceTemplateDetailSerializer(ServiceTemplateSerializer):
    # Nested representation of related objects
    project = ProjectTemplateSerializer(read_only=True)
    internal_provision_token_secret = IntegrationSecretSerializer(read_only=True)

class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class TenantDetailSerializer(TenantSerializer):
    # Nested representation for detailed views
    project = ProjectTemplateSerializer(read_only=True)
    created_by = UserSerializer(read_only=True)


class TenantServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantService
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'last_deployed_at']


class TenantServiceDetailSerializer(TenantServiceSerializer):
    # Nested representation for detailed views
    tenant = TenantSerializer(read_only=True)
    service_template = ServiceTemplateSerializer(read_only=True)


class DeploymentStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeploymentStep
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class DeploymentSerializer(serializers.ModelSerializer):
    # Include steps as nested objects in deployment detail
    steps = DeploymentStepSerializer(many=True, read_only=True)
    
    class Meta:
        model = Deployment
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'duration_seconds']


class DeploymentDetailSerializer(DeploymentSerializer):
    # Nested representation for detailed views
    tenant = TenantSerializer(read_only=True)
    triggered_by = UserSerializer(read_only=True)
    steps = DeploymentStepSerializer(many=True, read_only=True)


class DeploymentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deployment
        fields = ['tenant', 'trigger_reason']
        # Other fields will be set automatically


class JobRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobRecord
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class JobRecordDetailSerializer(JobRecordSerializer):
    # Nested representation for detailed views
    deployment = DeploymentSerializer(read_only=True)
    step = DeploymentStepSerializer(read_only=True)


class AuditEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEntry
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class AuditEntryDetailSerializer(AuditEntrySerializer):
    # Nested representation for detailed views
    user = UserSerializer(read_only=True)


# Specialized serializers for deployment operations
class DeploymentResumeSerializer(serializers.Serializer):
    deployment_id = serializers.IntegerField()
    resume_from_step = serializers.CharField(required=False)


class DeploymentStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Deployment.STATUS_CHOICES)
    message = serializers.CharField(required=False, allow_blank=True)


class StepStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=DeploymentStep.STATUS_CHOICES)
    message = serializers.CharField(required=False, allow_blank=True)
    meta = serializers.JSONField(required=False)


# Serializer for tenant creation with services
class TenantCreateSerializer(serializers.ModelSerializer):
    services = serializers.JSONField(write_only=True, required=False)

    class Meta:
        model = Tenant
        fields = ['project','name','client_ref','subdomain','services']

    def validate_subdomain(self, value):
        s = value.strip().lower()
        if not re.match(r'^[a-z0-9-]{2,63}$', s):
            raise serializers.ValidationError("Subdomain must be 2-63 chars, letters, numbers or hyphen.")
        return s
    
    def create(self, validated_data):
        services = validated_data.pop('services', [])
        tenant = super().create(validated_data)
        # services expected as list of dicts like [{'service_template': id, 'repo_url': '...'}]
        for s in services:
            st_id = s.get('service_template')
            st = ServiceTemplate.objects.get(id=st_id)
            TenantService.objects.create(
                tenant=tenant,
                service_template=st,
                name=s.get('name', st.name),
                repo_url=s.get('repo_url') or st.repo_url,
                repo_branch=s.get('repo_branch') or st.repo_branch
            )
        return tenant

# Serializer for service creation/update
class TenantServiceUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantService
        fields = ['repo_url', 'repo_branch']


# Health check serializers
class HealthCheckSerializer(serializers.Serializer):
    tenant_service_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=TenantService.HEALTH_STATUS_CHOICES)
    detail = serializers.CharField(required=False, allow_blank=True)


# Notification serializers
class NotificationSerializer(serializers.Serializer):
    deployment_id = serializers.IntegerField()
    success = serializers.BooleanField()
    message = serializers.CharField(required=False, allow_blank=True)