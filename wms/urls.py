from django.urls import path
from . import views

app_name = "wms"

urlpatterns = [
    path("", views.stock_view, name="home"),
    path("stock/", views.stock_view, name="stock"),
    path("receipts/", views.receipt_list, name="receipt_list"),
    path("receipts/new/", views.receipt_create, name="receipt_create"),
    path("receipts/<int:receipt_id>/", views.receipt_detail, name="receipt_detail"),

    path("quality/", views.quality_queue, name="quality_queue"),
    path("quality/<int:receipt_id>/approve/", views.quality_approve, name="quality_approve"),
    path("quality/<int:receipt_id>/reject/", views.quality_reject, name="quality_reject"),

    path("settings/", views.settings_page, name="settings"),
    path("settings/sync-items/", views.sync_items, name="sync_items"),
    path("settings/upload-stock/", views.upload_stock_snapshot, name="upload_stock"),
    path("api/autocomplete-item/", views.autocomplete_item, name="autocomplete_item"),
]
