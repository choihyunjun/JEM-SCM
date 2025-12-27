"""
Django settings for config project.
최종 수정일: 2025-12-19
반영 내용: 보안 강화, WhiteNoise(이미지 서빙), 권한 분리 기초 설정
"""

from pathlib import Path
import os

# 경로 설정
BASE_DIR = Path(__file__).resolve().parent.parent

# 보안 주의: 실제 배포시에는 환경변수 사용 권장
SECRET_KEY = 'django-insecure-itpjrwqo%(y%2wzj301tgp7@bzl836nt8nr#9#xq+knq@59sx6'

# [수정] VS Code 로컬 테스트를 위해 True로 변경 (CSS 로드 문제 해결 핵심)
DEBUG = True
# [보안 개선] 허용할 도메인 및 IP 리스트
ALLOWED_HOSTS = ['1.234.80.211', 'hyunjun0701.cafe24.com','jem-scm.com' ,'localhost', '127.0.0.1']


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # 우리가 만든 앱 & 라이브러리
    'orders',
    'import_export',
    'django.contrib.humanize',
    'qms',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # [이미지 해결] 미들웨어 추가
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization (한글 및 한국 시간 설정)
LANGUAGE_CODE = 'ko-kr'
TIME_ZONE = 'Asia/Seoul'
USE_I18N = True
USE_TZ = True


# [이미지/CSS 설정]
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'  # 서버에서 파일을 모을 경로

# [수정] DEBUG=True일 때는 WhiteNoise 저장소를 기본으로 사용하지 않도록 보완
# 로컬 개발 환경에서 CSS 수정을 즉시 반영하기 위함입니다.
if not DEBUG:
    STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# [추가] 앱 내부의 static 폴더를 명확히 인식하도록 경로 설정
STATICFILES_DIRS = [
    BASE_DIR / "orders" / "static",
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ==========================================
# ▼▼▼ [프로젝트 커스텀 설정]
# ==========================================

LOGIN_REDIRECT_URL = '/login-success/'
LOGOUT_REDIRECT_URL = '/accounts/login/'
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DATA_UPLOAD_MAX_MEMORY_SIZE = 52428800
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800


# ==========================================
# ▼▼▼ [구글 드라이브 & 미디어 스토리지 설정]
# ==========================================

# 1. 미디어 파일 기본 경로 설정 (FileField 등이 사용하는 경로)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# 2. Cafe24 운영 서버와 로컬 VS Code 환경 분기 처리
# Cafe24 서버에는 '/home/hyunjun0701' 경로가 존재합니다.
if os.path.exists('/home/hyunjun0701'):
    # [운영 서버 전용] 구글 드라이브 연동 로직
    # 기존에 Cafe24에서 연동하셨던 설정값이 있다면 여기에 추가될 것입니다.
    try:
        DEFAULT_FILE_STORAGE = 'gdstorage.storage.GoogleDriveStorage'
        # 예: GOOGLE_DRIVE_STORAGE_JSON_KEY_FILE = os.path.join(BASE_DIR, '인증파일.json')
    except Exception:
        # 혹시 라이브러리가 설치 안 되어 있을 경우를 대비한 안전장치
        DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
else:
    # [로컬 VS Code 전용] 
    # 내 컴퓨터에서는 일반 폴더(media/)에 저장하여 에러를 방지합니다.
    DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'

    # settings.py 수정
# 403 에러 메시지에 나온 도메인을 정확히 입력해야 합니다.
CSRF_TRUSTED_ORIGINS = [
    'https://jem-scm.com',
    'https://www.jem-scm.com', # www 포함 버전도 함께 넣는 것이 안전합니다.
]