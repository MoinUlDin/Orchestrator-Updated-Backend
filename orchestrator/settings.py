
from datetime import timedelta
from pathlib import Path
from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
# optionally load .env in development
load_dotenv(BASE_DIR / ".env")

DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("1", "true", "yes")
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "NOT_secure_use_only_Local_TEsting")
if not SECRET_KEY and not DEBUG or SECRET_KEY=='NOT_secure_use_only_Local_TEsting':
    raise Exception("Missing DJANGO_SECRET_KEY for production")

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CORS_ALLOWED_ORIGINS = [
    "https://abc.schoolcare.pk",
]


FIELD_ENCRYPTION_KEY = 'tg93UNJdqF9ZOmNmWlHXBQavakvYX2gqEVEnDXFIEvs='

DEBUG = True

ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_apscheduler',
    'drf_spectacular',

    # your apps
    'accounts', # user managment
    'core'
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'orchestrator.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173"
]
# or for dev quickly:
# CORS_ALLOW_ALL_ORIGINS = True

# Media (profile images)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

WSGI_APPLICATION = 'orchestrator.wsgi.application'

AUTH_USER_MODEL = 'accounts.User'   # app name 'auth', model 'User'

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Orchestrator Advance',
    'DESCRIPTION': 'Your project description',
    'VERSION': '1.1.0',
    'SERVE_INCLUDE_SCHEMA': False,

}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=7),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
}

# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Email: simple console backend by default (dev)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@example.com")
# retry and delays
DOKPLOY_MAX_RETRIES = os.getenv("DOKPLOY_MAX_RETRIES", 5)
DOKPLOY_MAX_RETRY_DELAY_CAP = os.getenv("DOKPLOY_MAX_RETRY_DELAY_CAP", 120)
# Dokploy / provisioning env (these will be read by the provisioner code)
DOKPLOY_API = os.getenv("DOKPLOY_API", "")
DOKPLOY_TOKEN = os.getenv("DOKPLOY_TOKEN", "")
PROVISION_CALLBACK_TOKEN = os.getenv("PROVISION_CALLBACK_TOKEN", "internal-provision-token")

EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST='smtp.gmail.com'
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=os.getenv("EMAIL_HOST_USER", None)
EMAIL_HOST_PASSWORD=os.getenv("EMAIL_HOST_PASSWORD", None)
DEFAULT_FROM_EMAIL=os.getenv("DEFAULT_FROM_EMAIL", None)