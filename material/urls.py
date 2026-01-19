from django.urls import path
from . import views

app_name = 'material'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    
    # 1. 재고/수불 관리
    path('stock/', views.stock_list, name='stock_list'),
    path('stock/transfer/', views.stock_transfer, name='stock_transfer'),
    path('stock/transfer/history/', views.transfer_history, name='transfer_history'),
    path('transaction/history/', views.transaction_history, name='transaction_history'),
    path('stock/adjustment/', views.stock_adjustment, name='stock_adjustment'),
    
    # 2. 입고 관리
    path('inbound/manual/', views.manual_incoming, name='manual_incoming'),
    
    # [핵심] views.incoming_history 함수와 연결하고, name도 'incoming_history'로 설정
    path('inbound/history/', views.incoming_history, name='incoming_history'),

    # 3. 출고 관리
    path('outbound/create/', views.outbound_create, name='outbound_create'),

    # 4. 현장 지원
    path('tag/create/', views.process_tag_form, name='process_tag_form'),
    path('tag/print/', views.process_tag_print, name='process_tag_print'),
    
    # [신규] 재고 조사
    path('stock/check/', views.stock_check, name='stock_check'),
    path('stock/check/result/', views.stock_check_result, name='stock_check_result'),
    path('outbound/return/', views.stock_return, name='stock_return'),
    
    path('outbound/return/print/<int:trx_id>/', views.print_return_note, name='print_return_note'),
    path('stock/move/', views.stock_move, name='stock_move'),
    path('api/part-exists/', views.api_part_exists, name='api_part_exists'),

    # LOT 상세 조회 API
    path('stock/lot-details/<str:part_no>/', views.get_lot_details, name='get_lot_details'),
]