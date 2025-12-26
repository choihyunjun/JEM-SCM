from django.contrib import admin
from django.urls import path, include
from orders import views
from django.conf import settings  # [필수] settings 불러오기
from django.conf.urls.static import static  # [필수] static 함수 불러오기

# 관리자 페이지 제목 설정
admin.site.site_header = "JEM SCM 관리자 시스템"
admin.site.site_title = "JEM SCM"
admin.site.index_title = "진영전기 주식회사 발주 시스템"

urlpatterns = [
    # 1. 관리자 및 기본 로그인 관련
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('login-success/', views.login_success, name='login_success'),

    # [추가] SCM 스타일 통합 관리자 메인 페이지
    path('scm-admin/', views.scm_admin_main, name='scm_admin_main'),

    # 2. 발주 조회 및 기본 페이지
    path('', views.order_list, name='order_list'),
    path('list/', views.order_list, name='order_list_alias'),

    # 3. 발주 관리 기능 (등록/삭제/엑셀/마감)
    path('upload/', views.order_upload, name='order_upload'),
    path('upload-action/', views.order_upload_action, name='order_upload_action'),
    path('delete/', views.order_delete, name='order_delete'),
    path('close-action/', views.order_close_action, name='order_close_action'),
    path('export/', views.order_export, name='order_export'),

    # 4. 발주 승인 기능
    path('approve/<int:order_id>/', views.order_approve, name='order_approve'),
    path('approve-all/', views.order_approve_all, name='order_approve_all'),

    # 5. 과부족 조회 및 소요량/재고 관리
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/export/', views.inventory_export, name='inventory_export'),
    path('inventory/upload/', views.inventory_upload, name='inventory_upload'),
    path('inventory/upload/action/', views.inventory_upload_action, name='inventory_upload_action'),
    path('inventory/demand-manage/', views.demand_manage, name='demand_manage'),
    path('inventory/demand-delete-action/', views.demand_delete_action, name='demand_delete_action'),
    path('inventory/demand-upload/', views.demand_upload_action, name='demand_upload_action'),
    path('inventory/demand-delete/', views.delete_all_demands, name='delete_all_demands'),
    path('inventory/demand-update-ajax/', views.demand_update_ajax, name='demand_update_ajax'),
    path('inventory/quick-order/', views.quick_order_action, name='quick_order_action'),

    # 6. 라벨 발행 및 납품서 관리
    path('label/list/', views.label_list, name='label_list'),
    path('label/create_order/', views.create_delivery_order, name='create_delivery_order'),
    path('label/print/<int:order_id>/', views.label_print, name='label_print'),
    path('label/print_note/<int:order_id>/', views.delivery_note_print, name='delivery_note_print'),
    path('label/action/', views.label_print_action, name='label_print_action'),
    path('label/delete/<int:order_id>/', views.delete_delivery_order, name='delete_delivery_order'),

    # 7. 입고 관리 (스캔 및 이력 조회)
    path('label/receive_scan/', views.receive_delivery_order_scan, name='receive_delivery_order_scan'),
    path('incoming/list/', views.incoming_list, name='incoming_list'),
    path('incoming/export/', views.incoming_export, name='incoming_export'),
    path('incoming/cancel/', views.incoming_cancel, name='incoming_cancel'),

    # 8. 품질 관리 시스템 (QMS)
    path('qms/', include('qms.urls')),
]

# ★ 사진(미디어) 파일을 서빙하기 위한 설정 ★
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)