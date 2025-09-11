# auth/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import AdminUserViewSet, LoginView, TokenRefreshViewCustom, UserInfoView

router = DefaultRouter()
router.register(r'admin/users', AdminUserViewSet, basename='admin-users')

urlpatterns = [
    # viewset CRUD endpoints (list/create/retrieve/update/delete)
    path('', include(router.urls)),

    # auth endpoints
    path('login/', LoginView.as_view(), name='token_obtain_pair'), # POST email+password -> tokens + user
    path('token/refresh/', TokenRefreshViewCustom.as_view(), name='token_refresh'),  
    path('me/', UserInfoView.as_view(), name='user-info'),    
]
