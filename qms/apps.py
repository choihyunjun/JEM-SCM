from django.apps import AppConfig

class QmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'qms'  # 이 이름이 base.html의 app_name 판별 기준이 됩니다.