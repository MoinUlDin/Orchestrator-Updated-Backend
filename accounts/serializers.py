# auth/serializers.py
from rest_framework import serializers
from .models import User
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

class AdminUserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ('id', 'email', 'password', 'first_name', 'last_name', 'profile_picture')
        read_only_fields = ('id',)

    def create(self, validated_data):
        password = validated_data.pop('password')
        # ensure new users created via this endpoint are managers only
        validated_data['role'] = User.ROLE_MANAGER
        user = User.objects.create_user(**validated_data, password=password)
        return user

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'role', 'profile_picture')
        read_only_fields = ('email', 'role')


# Custom Token serializer that includes user info in the token response
class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # optional: add custom claims
        token['role'] = getattr(user, "role", None)
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        # attach user info to the response body
        user = self.user
        role = user.role.capitalize()
        user_data = {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": role,
            "profile_picture": None
        }
        # if profile picture exists, return its URL (ensure MEDIA served in production)
        try:
            if getattr(user, "profile_picture", None) and hasattr(user.profile_picture, "url"):
                user_data["profile_picture"] = user.profile_picture.url
        except Exception:
            user_data["profile_picture"] = None

        data["user"] = user_data
        return data