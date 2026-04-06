from django.apps import AppConfig


class AdminAppConfig(AppConfig):
    name = 'admin_app'
    default_auto_field = 'django.db.models.BigAutoField'
    verbose_name = '관리자'
