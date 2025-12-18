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
    
    # [신규 추가] 발주 마감 처리 기능
    path('close-action/', views.order_close_action, name='order_close_action'),

    path('export/', views.order_export, name='order_export'),

    # 4. 발주 승인 기능
    path('approve/<int:order_id>/', views.order_approve, name='order_approve'),
    path('approve-all/', views.order_approve_all, name='order_approve_all'),

    # 5. 과부족 조회 및 엑셀 출력 기능
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/export/', views.inventory_export, name='inventory_export'),

    # ============================================================
    # 6. 라벨 발행 및 납품서 관리
    # ============================================================
    
    # 6-1. 라벨/납품서 메인 목록 화면
    path('label/list/', views.label_list, name='label_list'),

    # 6-2. 납품서 생성 (DB 저장 처리)
    path('label/create_order/', views.create_delivery_order, name='create_delivery_order'),

    # 6-3. 라벨 인쇄 화면
    path('label/print/<int:order_id>/', views.label_print, name='label_print'),

    # 6-4. 납품서(거래명세서) 인쇄 화면
    path('label/print_note/<int:order_id>/', views.delivery_note_print, name='delivery_note_print'),

    # 6-5. (에러 방지용) 기존 단일 인쇄 액션
    path('label/action/', views.label_print_action, name='label_print_action'),

    # ============================================================
    # 7. [신규] 입고 관리 (스캔 및 이력 조회)
    # ============================================================
    
    # 7-1. 입고 처리 (QR 스캔 시 여기로 데이터 전송)
    path('label/receive_scan/', views.receive_delivery_order_scan, name='receive_delivery_order_scan'),

    # 7-2. [✅ 신규 추가] 입고 현황 리스트 (새 메뉴 화면)
    path('incoming/list/', views.incoming_list, name='incoming_list'),

    # 7-2. 입고 현황 리스트
    path('incoming/list/', views.incoming_list, name='incoming_list'),

    # 7-3. [✅ 신규 추가] 입고 내역 엑셀 다운로드
    path('incoming/export/', views.incoming_export, name='incoming_export'),
]