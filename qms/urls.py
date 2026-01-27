from django.urls import path
from . import views

app_name = 'qms'

urlpatterns = [
    path('m4/', views.m4_list, name='m4_list'),
    path('m4/create/', views.m4_create, name='m4_create'),
    path('m4/<int:pk>/', views.m4_detail, name='m4_detail'),
    
    # [추가] 기안자 수동 결재(상신) 경로
    path('m4/<int:pk>/submit/', views.m4_submit, name='m4_submit'),
    
    path('m4/<int:pk>/approve/', views.m4_approve, name='m4_approve'),
    path('m4/<int:pk>/reject/', views.m4_reject, name='m4_reject'),
    path('m4/<int:pk>/resubmit/', views.m4_resubmit, name='m4_resubmit'),
    path('m4/<int:pk>/cancel/', views.m4_cancel_approval, name='m4_cancel_approval'),
    
    path('m4/<int:pk>/review/', views.add_m4_review, name='add_m4_review'),
    path('m4/<int:pk>/vendor-response/', views.m4_vendor_response, name='m4_vendor_response'),
    path('m4/<int:pk>/edit/', views.m4_update, name='m4_update'),
    
    path('review/<int:review_id>/delete/', views.delete_m4_review, name='delete_m4_review'),
    path('review/<int:review_id>/edit/', views.edit_m4_review, name='edit_m4_review'),

    path('m4/<int:pk>/delete/', views.m4_delete, name='m4_delete'),

    # 정식 4M (사전 4M 승인 후 생성)
    path('formal/', views.formal4m_list, name='formal4m_list'),
    path('formal/<int:formal_id>/', views.formal4m_detail_by_id, name='formal4m_detail_by_id'),
    path('formal/<int:formal_id>/validity-start/', views.formal4m_set_validity_start, name='formal4m_set_validity_start'),
    path('formal/<int:formal_id>/upgrade/', views.formal4m_upgrade_to_full, name='formal4m_upgrade_to_full'),
    path('m4/<int:pk>/formal/', views.formal4m_detail, name='formal4m_detail'),
    path('formal/<int:formal_id>/item/<int:item_id>/upload/', views.formal4m_upload, name='formal4m_upload'),
    path('formal/<int:formal_id>/item/<int:item_id>/review/', views.formal4m_review_update, name='formal4m_review_update'),

    # 정식 4M 결재(워크플로우)
    path('formal/<int:formal_id>/workflow/set/', views.formal4m_workflow_set, name='formal4m_workflow_set'),
    path('formal/<int:formal_id>/workflow/submit/', views.formal4m_workflow_submit, name='formal4m_workflow_submit'),
    path('formal/<int:formal_id>/workflow/approve/', views.formal4m_workflow_approve, name='formal4m_workflow_approve'),
    path('formal/<int:formal_id>/workflow/reject/', views.formal4m_workflow_reject, name='formal4m_workflow_reject'),
    path('formal/<int:formal_id>/workflow/resubmit/', views.formal4m_workflow_resubmit, name='formal4m_workflow_resubmit'),
    path('formal/<int:formal_id>/workflow/cancel/', views.formal4m_workflow_cancel, name='formal4m_workflow_cancel'),

    path('formal/<int:formal_id>/inspection/<int:row_id>/update/', views.formal4m_inspection_update, name='formal4m_inspection_update'),
    path('formal/<int:formal_id>/schedule/<int:row_id>/update/', views.formal4m_schedule_update, name='formal4m_schedule_update'),
    path('formal/<int:formal_id>/stage/<int:row_id>/update/', views.formal4m_stage_update, name='formal4m_stage_update'),
    path('formal/<int:formal_id>/approval/update/', views.formal4m_approval_update, name='formal4m_approval_update'),
    path('formal/<int:formal_id>/inspection/add/', views.formal4m_inspection_add, name='formal4m_inspection_add'),
    path('formal/<int:formal_id>/schedule/add/', views.formal4m_schedule_add, name='formal4m_schedule_add'),

    path('inspection/list/', views.import_inspection_list, name='import_inspection_list'),
    path('inspection/<int:pk>/', views.import_inspection_detail, name='import_inspection_detail'),

    # ============================================
    # 새로운 4M 변경점 관리 (v2)
    # ============================================
    path('change/', views.change_request_list, name='change_request_list'),
    path('change/create/', views.change_request_create, name='change_request_create'),
    path('change/<int:pk>/', views.change_request_detail, name='change_request_detail'),
    path('change/<int:pk>/edit/', views.change_request_edit, name='change_request_edit'),
    path('change/<int:pk>/submit/', views.change_request_submit, name='change_request_submit'),
    path('change/<int:pk>/phase/', views.change_request_phase_change, name='change_request_phase_change'),

    # 결재
    path('approval/<int:pk>/process/', views.approval_step_process, name='approval_step_process'),

    # 협력사 회신
    path('change/<int:pk>/vendor-request/', views.vendor_response_create, name='vendor_response_create'),
    path('vendor-response/<int:pk>/submit/', views.vendor_response_submit, name='vendor_response_submit'),

    # 제출 서류
    path('document/<int:pk>/upload/', views.document_upload, name='document_upload'),
    path('document/<int:pk>/review/', views.document_review, name='document_review'),

    # 유효성 평가
    path('change/<int:pk>/validity/', views.validity_evaluation, name='validity_evaluation'),

    # ============================================
    # QMS 대시보드
    # ============================================
    path('', views.qms_dashboard, name='dashboard'),

    # ============================================
    # 출하검사
    # ============================================
    path('outgoing/', views.outgoing_inspection_list, name='outgoing_list'),
    path('outgoing/create/', views.outgoing_inspection_create, name='outgoing_create'),
    path('outgoing/<int:pk>/', views.outgoing_inspection_detail, name='outgoing_detail'),

    # ============================================
    # 부적합품 관리
    # ============================================
    path('nc/', views.nc_list, name='nc_list'),
    path('nc/create/', views.nc_create, name='nc_create'),
    path('nc/<int:pk>/', views.nc_detail, name='nc_detail'),

    # ============================================
    # 시정조치 (CAPA)
    # ============================================
    path('capa/', views.capa_list, name='capa_list'),
    path('capa/create/', views.capa_create, name='capa_create'),
    path('capa/<int:pk>/', views.capa_detail, name='capa_detail'),

    # ============================================
    # 협력사 클레임
    # ============================================
    path('claim/', views.claim_list, name='claim_list'),
    path('claim/create/', views.claim_create, name='claim_create'),
    path('claim/<int:pk>/', views.claim_detail, name='claim_detail'),

    # ============================================
    # 협력사 평가
    # ============================================
    path('rating/', views.vendor_rating_list, name='rating_list'),
    path('rating/create/', views.vendor_rating_create, name='rating_create'),
    path('rating/<int:pk>/', views.vendor_rating_detail, name='rating_detail'),

    # ============================================
    # ISIR (초도품검사)
    # ============================================
    path('isir/', views.isir_list, name='isir_list'),
    path('isir/create/', views.isir_create, name='isir_create'),
    path('isir/<int:pk>/', views.isir_detail, name='isir_detail'),
    path('isir/<int:pk>/pdf/', views.isir_pdf, name='isir_pdf'),

    # ============================================
    # VOC 관리 (고객의 소리)
    # ============================================
    path('voc/', views.voc_list, name='voc_list'),
    path('voc/create/', views.voc_create, name='voc_create'),
    path('voc/<int:pk>/', views.voc_detail, name='voc_detail'),

    # ============================================
    # 계측기 관리
    # ============================================
    path('gauge/', views.gauge_list, name='gauge_list'),
    path('gauge/create/', views.gauge_create, name='gauge_create'),
    path('gauge/<int:pk>/', views.gauge_detail, name='gauge_detail'),

    # ============================================
    # 품질문서 관리
    # ============================================
    path('qdoc/', views.qdoc_list, name='qdoc_list'),
    path('qdoc/create/', views.qdoc_create, name='qdoc_create'),
    path('qdoc/<int:pk>/', views.qdoc_detail, name='qdoc_detail'),
]
