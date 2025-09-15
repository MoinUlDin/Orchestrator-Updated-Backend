from django.contrib import admin
from .models import Tenant, Deployment, DeploymentStep
# Register your models here.

admin.site.register(Tenant)
admin.site.register(Deployment)
admin.site.register(DeploymentStep)