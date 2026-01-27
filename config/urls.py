from django.contrib import admin
from django.urls import path, include
from orders import views
from django.conf import settings
from django.conf.urls.static import static

# [수정] material_views는 이제 include를 쓰므로 필요 없지만, 에러 방지를 위해 지우지 않아도 됨
from material import views as material_views 

# 관리자 페이지 제목 설정
admin.site.site_header = "JEM SCM 관리자 시스템"
admin.site.site_title = "JEM SCM"
admin.site.index_title = "진영전기 주식회사 발주 시스템"

urlpatterns = [
    # ------------------------------------------------------------------
    # 1. 관리자 및 기본 로그인
    # ------------------------------------------------------------------
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('login-success/', views.login_success, name='login_success'),

    # SCM 스타일 통합 관리자 메인 페이지
    path('scm-admin/', views.scm_admin_main, name='scm_admin_main'),

    # ------------------------------------------------------------------
    # 2. 발주(SCM) 관련 기능
    # ------------------------------------------------------------------
    # 조회 및 메인 (기본 페이지: 알림 대시보드)
    path('', views.scm_alert_dashboard, name='home'),
    path('list/', views.order_list, name='order_list'),

    # [관리 기능]
    path('upload/', views.order_upload, name='order_upload'),
    path('upload/preview/', views.order_upload_preview, name='order_upload_preview'),
    path('upload/confirm/', views.order_create_confirm, name='order_create_confirm'),
    path('upload/template/', views.order_upload_template, name='order_upload_template'),

    path('delete/', views.order_delete, name='order_delete'),
    path('close-action/', views.order_close_action, name='order_close_action'),
    path('export/', views.order_export, name='order_export'),

    # 승인 기능
    path('approve/<int:order_id>/', views.order_approve, name='order_approve'),
    path('approve-all/', views.order_approve_all, name='order_approve_all'),

    # ------------------------------------------------------------------
    # 3. 재고 및 소요량 관리 (SCM)
    # ------------------------------------------------------------------
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/export/', views.inventory_export, name='inventory_export'),
    path('inventory/lot-details/<str:part_no>/', views.get_lot_details, name='get_lot_details'),

    path('inventory/demand-manage/', views.demand_manage, name='demand_manage'),
    path('inventory/demand-delete-action/', views.demand_delete_action, name='demand_delete_action'),
    path('inventory/demand-upload/', views.demand_upload_action, name='demand_upload_action'),
    path('inventory/demand-delete/', views.delete_all_demands, name='delete_all_demands'),
    path('inventory/demand-update-ajax/', views.demand_update_ajax, name='demand_update_ajax'),
    path('inventory/quick-order/', views.quick_order_action, name='quick_order_action'),
    path('inventory/bulk-shortage-order/', views.bulk_shortage_order, name='bulk_shortage_order'),

    # ------------------------------------------------------------------
    # 4. 납품서(라벨) 및 입고 관리
    # ------------------------------------------------------------------
    path('label/list/', views.label_list, name='label_list'),
    path('label/create_order/', views.create_delivery_order, name='create_delivery_order'),
    path('label/print/<int:order_id>/', views.label_print, name='label_print'),
    path('label/print_note/<int:order_id>/', views.delivery_note_print, name='delivery_note_print'),
    path('label/action/', views.label_print_action, name='label_print_action'),
    path('label/delete/<int:order_id>/', views.delete_delivery_order, name='delete_delivery_order'),

    # [QR 스캔 프로세스]
    path('label/receive_scan/', views.receive_delivery_order_scan, name='receive_delivery_order_scan'),
    path('incoming/confirm/', views.receive_delivery_order_confirm, name='receive_delivery_order_confirm'),

    path('incoming/list/', views.incoming_list, name='incoming_list'),
    path('incoming/export/', views.incoming_export, name='incoming_export'),
    path('incoming/cancel/', views.incoming_cancel, name='incoming_cancel'),

    # [품목 마스터 관리]
    path('part/list/', views.part_list, name='part_list'),
    path('part/vendor-template/', views.part_vendor_template, name='part_vendor_template'),
    path('part/upload/', views.part_upload, name='part_upload'),
    path('part/upload/preview/', views.part_upload_preview, name='part_upload_preview'),
    path('part/upload/confirm/', views.part_upload_confirm, name='part_upload_confirm'),
    path('part/upload/template/', views.part_upload_template, name='part_upload_template'),

    # [협력사 관리]
    path('vendor/', views.vendor_manage, name='vendor_manage'),
    path('vendor/detail/<int:vendor_id>/', views.vendor_detail, name='vendor_detail'),
    path('vendor/create/', views.vendor_create, name='vendor_create'),
    path('vendor/update/', views.vendor_update, name='vendor_update'),
    path('vendor/delete/', views.vendor_delete, name='vendor_delete'),
    path('vendor/link-user/', views.vendor_link_user, name='vendor_link_user'),
    path('vendor/unlink-user/', views.vendor_unlink_user, name='vendor_unlink_user'),
    path('vendor/search-users/', views.vendor_search_users, name='vendor_search_users'),
    path('vendor/upload/', views.vendor_upload, name='vendor_upload'),
    path('vendor/upload/preview/', views.vendor_upload_preview, name='vendor_upload_preview'),
    path('vendor/upload/confirm/', views.vendor_upload_confirm, name='vendor_upload_confirm'),
    path('vendor/export/', views.vendor_export, name='vendor_export'),

    # [사용자 권한 관리]
    path('system/user-permission/', views.user_permission_manage, name='user_permission_manage'),

    # [사용자 관리]
    path('system/user/', views.user_manage, name='user_manage'),
    path('system/user/create/', views.user_create, name='user_create'),
    path('system/user/update/', views.user_update, name='user_update'),
    path('system/user/delete/', views.user_delete, name='user_delete'),

    # [리포트]
    path('report/vendor-delivery/', views.vendor_delivery_report, name='vendor_delivery_report'),
    path('report/vendor-delivery/close/', views.vendor_delivery_close_month, name='vendor_delivery_close_month'),
    path('report/alert-dashboard/', views.scm_alert_dashboard, name='scm_alert_dashboard'),

    # [공지사항 / QnA]
    path('notice/create/', views.notice_create, name='notice_create'),
    path('qna/create/', views.qna_create, name='qna_create'),
    path('qna/answer/<int:qna_id>/', views.qna_answer, name='qna_answer'),

    # ------------------------------------------------------------------
    # API 엔드포인트 (품번/협력사/직원 검색)
    # ------------------------------------------------------------------
    path('api/parts/search/', views.api_part_search, name='api_part_search'),
    path('api/vendors/search/', views.api_vendor_search, name='api_vendor_search'),
    path('api/organizations/search/', views.api_organization_search, name='api_organization_search'),
    path('api/employees/search/', views.api_employee_search, name='api_employee_search'),

    # ------------------------------------------------------------------
    # 5. 자재관리 (WMS) - [⭐⭐ 핵심 수정 사항 ⭐⭐]
    # ------------------------------------------------------------------
    # material/urls.py를 바라보도록 설정 (그래야 'material:dashboard' 같은 주소가 작동함)
    path('wms/', include('material.urls')),

    # ------------------------------------------------------------------
    # 6. 품질관리 (QMS)
    # ------------------------------------------------------------------
    path('qms/', include('qms.urls')),
    
    path('return/confirm/<int:pk>/', views.confirm_return, name='confirm_return'), # ✅ [신규] 반출 확인
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    