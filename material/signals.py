from django.db.models.signals import pre_delete
from django.dispatch import receiver
from .models import MaterialTransaction


@receiver(pre_delete, sender=MaterialTransaction)
def protect_pending_erp_receipt(sender, instance, **kwargs):
    from .erp_outbox import guard_receipt_change
    guard_receipt_change(instance)
