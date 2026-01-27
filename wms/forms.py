from django import forms

class _BootstrapMixin:
    def _apply(self):
        for name, field in self.fields.items():
            w = field.widget
            # Don't override checkbox etc.
            cls = w.attrs.get("class", "")
            if isinstance(w, (forms.TextInput, forms.NumberInput, forms.DateInput, forms.DateTimeInput, forms.FileInput)):
                w.attrs["class"] = (cls + " form-control").strip()
            elif isinstance(w, forms.Select):
                w.attrs["class"] = (cls + " form-select").strip()

class ReceiptCreateForm(_BootstrapMixin, forms.Form):
    warehouse_code = forms.CharField(label="입고창고", max_length=50)
    part_no = forms.CharField(label="품번", max_length=100)
    receipt_qty = forms.DecimalField(label="입고수량", max_digits=18, decimal_places=3)
    receipt_date = forms.DateField(label="입고일", widget=forms.DateInput(attrs={"type": "date"}))
    mfg_date = forms.DateField(label="제조일", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    lot_no = forms.CharField(label="LOT No", max_length=100, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply()

class StockUploadForm(_BootstrapMixin, forms.Form):
    file = forms.FileField(label="ERP 재고 파일(엑셀/CSV)")
    snapshot_at = forms.DateTimeField(
        label="기준일시(선택)",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="비우면 업로드 시각으로 저장됩니다."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply()
