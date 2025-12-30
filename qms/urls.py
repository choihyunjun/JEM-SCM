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
    ]
