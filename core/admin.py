from django.contrib import admin
from .models import Tenant, Deployment
# Register your models here.

admin.site.register(Tenant)
admin.site.register(Deployment)