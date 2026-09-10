from django.core.management.base import BaseCommand
from material.models import ERPIncomingOperation
from material.erp_outbox import dispatch, reconcile


class Command(BaseCommand):
    help = 'Send committed pending ERP receipt requests; reconcile uncertain results without resending.'

    def add_arguments(self, parser):
        parser.add_argument('--reconcile', action='store_true')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        statuses = ['REVIEW', 'RUNNING'] if options['reconcile'] else ['PENDING']
        ids = list(ERPIncomingOperation.objects.filter(status__in=statuses).order_by('created_at')
                   .values_list('pk', flat=True)[:max(0, options['limit'])])
        for pk in ids:
            result = reconcile(pk) if options['reconcile'] else dispatch(pk)
            self.stdout.write(f'{pk}: {result or "processed"}')
