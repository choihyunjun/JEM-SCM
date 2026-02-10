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
    path('transaction/history/excel/', views.transaction_history_excel, name='transaction_history_excel'),
    path('stock/adjustment/', views.stock_adjustment, name='stock_adjustment'),
    path('stock/adjustment/template/', views.stock_adjustment_template, name='stock_adjustment_template'),
    path('closing/report/', views.closing_report, name='closing_report'),
    path('closing/report/excel/', views.closing_report_excel, name='closing_report_excel'),
    
    # 2. 입고 관리
    path('inbound/manual/', views.manual_incoming, name='manual_incoming'),
    
    # [핵심] views.incoming_history 함수와 연결하고, name도 'incoming_history'로 설정
    path('inbound/history/', views.incoming_history, name='incoming_history'),

    # 3. 출고 관리
    path('outbound/create/', views.outbound_create, name='outbound_create'),

    # 4. 현장 지원
    path('tag/create/', views.process_tag_form, name='process_tag_form'),
    path('tag/print/', views.process_tag_print, name='process_tag_print'),

    # 현품표 스캔 API (중복 스캔 확인)
    path('api/tag/scan/', views.api_process_tag_scan, name='api_process_tag_scan'),
    path('api/tag/<str:tag_id>/', views.api_process_tag_info, name='api_process_tag_info'),
    
    # [신규] 재고 조사 (QR 스캔)
    path('inventory-check/', views.inventory_check_list, name='inventory_check_list'),
    path('inventory-check/create/', views.inventory_check_create, name='inventory_check_create'),
    path('inventory-check/<int:pk>/scan/', views.inventory_check_scan, name='inventory_check_scan'),
    path('inventory-check/<int:pk>/scan/api/', views.inventory_check_scan_api, name='inventory_check_scan_api'),
    path('inventory-check/<int:pk>/complete/', views.inventory_check_complete, name='inventory_check_complete'),
    path('inventory-check/<int:pk>/result/', views.inventory_check_result, name='inventory_check_result'),

    # [신규] 재고 종합 조회 (피벗 테이블)
    path('inventory-summary/', views.inventory_summary, name='inventory_summary'),
    path('inventory-summary/excel/', views.inventory_summary_excel, name='inventory_summary_excel'),

    # [기존] 재고 조사 (수동)
    path('stock/check/', views.stock_check, name='stock_check'),
    path('stock/check/result/', views.stock_check_result, name='stock_check_result'),
    path('outbound/return/', views.stock_return, name='stock_return'),
    
    path('outbound/return/print/<int:trx_id>/', views.print_return_note, name='print_return_note'),
    path('stock/move/', views.stock_move, name='stock_move'),
    path('api/part-exists/', views.api_part_exists, name='api_part_exists'),

    # LOT 관리 API
    path('stock/lot-details/<str:part_no>/', views.get_lot_details, name='wms_lot_details'),
    path('api/available-lots/', views.api_get_available_lots, name='api_get_available_lots'),

    # ERP 동기화
    path('erp-sync/', views.erp_sync, name='erp_sync'),
    path('erp-sync/export/', views.erp_sync_export, name='erp_sync_export'),

    # BOM 관리
    path('bom/', views.bom_list, name='bom_list'),
    path('bom/upload/', views.bom_upload, name='bom_upload'),
    path('bom/delete-all/', views.bom_delete_all, name='bom_delete_all'),
    path('bom/calculate/', views.bom_calculate, name='bom_calculate'),
    path('bom/calculate/template/', views.bom_calc_template, name='bom_calc_template'),
    path('bom/calculate/export/', views.bom_calc_export, name='bom_calc_export'),
    path('bom/calculate/batch-export/', views.bom_calc_batch_export, name='bom_calc_batch_export'),
    path('bom/detail/<str:part_no>/', views.bom_detail, name='bom_detail'),
    path('api/bom/calculate/', views.api_bom_calculate, name='api_bom_calculate'),
    path('bom/register-demand/', views.bom_register_demand, name='bom_register_demand'),

    # 원재료 관리
    path('raw-material/', views.raw_material_layout, name='raw_material_layout'),
    path('raw-material/incoming/', views.raw_material_incoming, name='raw_material_incoming'),
    path('raw-material/rack-manage/', views.raw_material_rack_manage, name='raw_material_rack_manage'),
    path('raw-material/setting/', views.raw_material_setting, name='raw_material_setting'),
    path('raw-material/expiry/', views.raw_material_expiry, name='raw_material_expiry'),
    path('raw-material/label-print/', views.raw_material_label_print, name='raw_material_label_print'),
    path('api/raw-material-labels/', views.api_raw_material_labels, name='api_raw_material_labels'),
    path('api/part-search/', views.api_part_search, name='api_part_search'),
]