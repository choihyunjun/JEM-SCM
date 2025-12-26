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
    path('m4/<int:pk>/edit/', views.m4_update, name='m4_update'),
    
    path('review/<int:review_id>/delete/', views.delete_m4_review, name='delete_m4_review'),
    path('review/<int:review_id>/edit/', views.edit_m4_review, name='edit_m4_review'),

    path('m4/<int:pk>/delete/', views.m4_delete, name='m4_delete'),
]