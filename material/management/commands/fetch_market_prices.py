from django.core.management.base import BaseCommand

from material.market_price import MAX_COPPER_PAGES, collect


class Command(BaseCommand):
    help = '시세·환율 수집: 한국은행 환율 4종 + 한국비철금속협회 동 시세. 금·은은 화면에서 직접 입력한다.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=10, help='환율을 며칠 전부터 다시 받을지 (처음 채울 때는 400 등)')
        parser.add_argument('--pages', type=int, default=1,
                            help=f'동 시세 표를 몇 쪽 읽을지 (한 쪽 20일, 최대 {MAX_COPPER_PAGES})')

    def handle(self, *args, **options):
        for source, ok, message in collect(days=max(1, options['days']), pages=max(1, options['pages'])):
            self.stdout.write(f"{source}: {'OK' if ok else 'FAIL'} {message}")
