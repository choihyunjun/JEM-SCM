from django.contrib import admin
from django.urls import path, include
from orders import views

# 관리자 페이지 제목 설정
admin.site.site_header = "JEM SCM 관리자 시스템"
admin.site.site_title = "JEM SCM"
admin.site.index_title = "진영전기 주식회사 발주 시스템"

urlpatterns = [
    # 1. 관리자 및 기본 로그인 관련
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('login-success/', views.login_success, name='login_success'),

    # 2. 발주 조회 및 기본 페이지
    path('', views.order_list, name='order_list'),
    path('list/', views.order_list, name='order_list_alias'), # 중복 방지용 별칭

    # 3. 발주 관리 기능 (등록/삭제/엑셀)
    path('upload/', views.order_upload, name='order_upload'),
    path('upload-action/', views.order_upload_action, name='order_upload_action'),
    path('delete/', views.order_delete, name='order_delete'),
    path('export/', views.order_export, name='order_export'),

    # 4. 발주 승인 기능
    path('approve/<int:order_id>/', views.order_approve, name='order_approve'),
    path('approve-all/', views.order_approve_all, name='order_approve_all'),

    # 5. 과부족 조회 및 엑셀 출력 기능
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/export/', views.inventory_export, name='inventory_export'), # <-- [신규 추가]
]