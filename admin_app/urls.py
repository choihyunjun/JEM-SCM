from django.urls import path
from . import views

app_name = 'admin_app'

urlpatterns = [
    path('', views.admin_dashboard, name='dashboard'),
    # 알림 관리
    path('notifications/', views.notification_manage, name='notification_manage'),
    path('api/rules/', views.api_rules, name='api_rules'),
    path('api/notification-logs/', views.api_notification_logs, name='api_notification_logs'),
]
