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
    # 조회 및 메인
    path('', views.order_list, name='order_list'),
    path('list/', views.order_list, name='order_list_alias'),

    # [관리 기능]
    path('upload/', views.order_upload, name='order_upload'),
    path('upload/preview/', views.order_upload_preview, name='order_upload_preview'),
    path('upload/confirm/', views.order_create_confirm, name='order_create_confirm'),

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

    