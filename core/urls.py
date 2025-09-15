from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views


router = DefaultRouter()
router.register(r'project-templates', views.ProjectTemplateViewSet)
router.register(r'service-templates', views.ServiceTemplateViewSet)
router.register(r'tenants', views.TenantViewSet)
router.register(r'tenant-services', views.TenantServiceViewSet)
router.register(r'deployments', views.DeploymentViewSet)
router.register(r'deployment-steps', views.DeploymentStepViewSet)
router.register(r'job-records', views.JobRecordViewSet)
router.register(r'audit-entries', views.AuditEntryViewSet)

# Additional custom views
router.register(r'notifications', views.NotificationViewSet, basename='notification')

urlpatterns = [
    path('', include(router.urls)),
    path("dashboard/overview/", views.DashboardOverviewAPIView.as_view(), name="dashboard-overview"),
]