# auth/views.py
from rest_framework import generics, permissions
from .serializers import AdminUserCreateSerializer, UserSerializer, MyTokenObtainPairSerializer
from .models import User
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView



class IsAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.is_admin



class LoginView(TokenObtainPairView):
    """
    POST -> { "email": "...", "password": "..." }
    Response -> { "access": "...", "refresh": "...", "user": { ... } }
    """
    permission_classes = [AllowAny]
    authentication_classes=[]
    serializer_class = MyTokenObtainPairSerializer


class TokenRefreshViewCustom(TokenRefreshView):
    """
    Optional: wrapper for token/refresh endpoint (same behavior as simplejwt).
    POST -> { "refresh": "<token>" } returns new access token.
    """
    permission_classes = [AllowAny]


class UserInfoView(APIView):
    """
    Returns authenticated user's basic profile.
    GET -> { id, email, first_name, last_name, role, profile_picture }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)



class AdminUserViewSet(viewsets.ModelViewSet):
    """
    Admin-only user management: list, update, disable, delete.
    Creating users should use AdminCreateUserView (or this view can be used too).
    """
    queryset = User.objects.all()
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get_serializer_class(self):
        # Use different serializers for list/detail vs create
        if self.action in ['create']:
            return AdminUserCreateSerializer
        return UserSerializer

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        # Prevent deletion of other admins via API (keep superuser creation via shell/management)
        if user.role == User.ROLE_ADMIN:
            return Response({'detail': 'Cannot delete an admin via API.'}, status=status.HTTP_403_FORBIDDEN)
        # Prefer disable over hard delete
        user.is_active = False
        user.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        user = self.get_object()
        if user.role == User.ROLE_ADMIN:
            return Response({'detail': 'Cannot disable an admin via API.'}, status=status.HTTP_403_FORBIDDEN)
        user.is_active = False
        user.save()
        return Response({'detail': 'User disabled'})
