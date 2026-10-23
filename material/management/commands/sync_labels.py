"""라벨을 창고별 실재고에 맞춘다 (재고 맞추기 때 자동 실행되며, 수동으로도 돌릴 수 있다)."""
from django.core.management.base import BaseCommand

from material.labels import sync_all


class Command(BaseCommand):
    help = '원재료 라벨 위치·소진 상태를 실재고에 맞춥니다'

    def handle(self, *args, **options):
        result = sync_all()
        self.stdout.write(self.style.SUCCESS(f"라벨 이동 {result['moved']}장, 소진 처리 {result['consumed']}장"))
