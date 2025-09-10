# auth/urls.py
from django.urls import path
from .views import AdminCreateUserView

urlpatterns = [
    path('admin/users/', AdminCreateUserView.as_view(), name='admin-create-user'),
]
