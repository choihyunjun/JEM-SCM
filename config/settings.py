from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------
DJANGO_ENV = os.getenv("DJANGO_ENV", "development")  # development | production
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1" if DJANGO_ENV == "production" else True

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    # 개발 편의용(운영에서는 반드시 환경변수로 넣어야 함)
    SECRET_KEY = "dev-only-secret-key-change-me"

ALLOWED_HOSTS = os.getenv(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1"
).split(",")

# 프록시(nginx 등) 뒤에 있다면 필요할 수 있음 (확실하지 않음: 현재 구성)
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# -----------------------------------------------------------------------------
# Apps
# -----------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "orders.apps.OrdersConfig",
    "import_export",
    "django.contrib.humanize",
    "qms",
    "material",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# -----------------------------------------------------------------------------
# Database (권장: 운영은 sqlite 지양)
# -----------------------------------------------------------------------------
DB_ENGINE = os.getenv("DJANGO_DB_ENGINE", "sqlite3")

if DB_ENGINE == "sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    # 예: mysql / postgres 등 (환경변수로 주입)
    DATABASES = {
        "default": {
            "ENGINE": os.getenv("DJANGO_DB_DJANGO_ENGINE"),  # 예: django.db.backends.mysql
            "NAME": os.getenv("DJANGO_DB_NAME"),
            "USER": os.getenv("DJANGO_DB_USER"),
            "PASSWORD": os.getenv("DJANGO_DB_PASSWORD"),
            "HOST": os.getenv("DJANGO_DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DJANGO_DB_PORT", ""),
        }
    }

# -----------------------------------------------------------------------------
# Auth / i18n
# -----------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

# -----------------------------------------------------------------------------
# Static (WhiteNoise)
# -----------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# 개발 중 즉시 반영이 필요하면 STATICFILES_DIRS 사용
STATICFILES_DIRS = [
    BASE_DIR / "orders" / "static",
    # 필요 시 프로젝트 공용 static 추가:
    # BASE_DIR / "static",
]

# 운영에서는 Manifest 기반 캐시 권장
if DJANGO_ENV == "production":
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
    WHITENOISE_MAX_AGE = 31536000  # 1년
    WHITENOISE_IMMUTABLE_FILE_TEST = lambda path, url: "manifest" in url or ".hash" in url

# -----------------------------------------------------------------------------
# Media
# -----------------------------------------------------------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# 운영 스토리지 분기(명시적 플래그 권장)
USE_GDRIVE = os.getenv("USE_GDRIVE_STORAGE", "0") == "1"

if USE_GDRIVE:
    # 확실하지 않음: gdstorage 패키지/설정이 어떤 방식인지
    DEFAULT_FILE_STORAGE = "gdstorage.storage.GoogleDriveStorage"
    # GOOGLE_DRIVE_STORAGE_JSON_KEY_FILE = os.getenv("GOOGLE_DRIVE_STORAGE_JSON_KEY_FILE")
else:
    DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"

# -----------------------------------------------------------------------------
# Security (운영 강화)
# -----------------------------------------------------------------------------
CSRF_TRUSTED_ORIGINS = os.getenv(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "https://jem-scm.com,https://www.jem-scm.com"
).split(",")

if DJANGO_ENV == "production":
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

# -----------------------------------------------------------------------------
# Project custom
# -----------------------------------------------------------------------------
LOGIN_REDIRECT_URL = "/login-success/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")

DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("DJANGO_DATA_UPLOAD_MAX", "52428800"))
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("DJANGO_FILE_UPLOAD_MAX", "52428800"))
DATA_UPLOAD_MAX_NUMBER_FIELDS = 50000  # BOM 대량 데이터 처리용

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"



