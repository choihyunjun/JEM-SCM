"""QMS 메뉴 점검(2026-10) 후속 수정 회귀 테스트.

Run with --settings=config.test_settings (ERP_ENABLED=false). 외부 HTTP·ERP 호출 없음.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

from material.models import MaterialStock, MaterialTransaction, Warehouse
from orders.models import Incoming, Order, Part, UserProfile, Vendor
from qms.access import QMS_MENU_PERMISSIONS, menu_permission
from qms.models import (
    CorrectiveAction, Formal4MRequest, ImportInspection, M4Request, M4Review,
    OutgoingInspection, QualityDocument, VendorClaim, VendorRating,
)

NO_HTTP = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP prohibited'))


class QmsTestBase(TestCase):
    def setUp(self):
        NO_HTTP.start()
        self.addCleanup(NO_HTTP.stop)

    def make_user(self, username, *perms, vendor=None):
        user = User.objects.create_user(username)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = 'VENDOR' if vendor else 'STAFF'
        profile.account_type = 'VENDOR' if vendor else 'INTERNAL'
        profile.org = vendor.organization if vendor else None
        for perm in perms:
            setattr(profile, perm, True)
        profile.save()
        return user

    def login(self, user):
        self.client.force_login(user)


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class MenuPermissionMappingTests(QmsTestBase):
    """메뉴 표시와 화면 권한이 같은 표(QMS_MENU_PERMISSIONS)를 쓴다."""

    MENU_URLS = {
        'm4': ('m4_list', '사전 4M'),
        'inspection': ('import_inspection_list', '수입검사 현황'),
        'nc': ('nc_list', '부적합품 관리'),
        'claim': ('claim_list', '클레임 관리'),
        'rating': ('rating_list', '협력사 평가'),
        'isir': ('isir_list', 'ISIR 현황'),
        'gauge': ('gauge_list', '계측기 관리'),
        'qdoc': ('qdoc_list', '품질문서 관리'),
    }

    def test_each_menu_visible_and_openable_with_only_its_own_permission(self):
        for menu, (url_name, label) in self.MENU_URLS.items():
            with self.subTest(menu=menu):
                user = self.make_user(f'only-{menu}', QMS_MENU_PERMISSIONS[menu])
                self.login(user)
                response = self.client.get(reverse(f'qms:{url_name}'))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, label)
                # 다른 메뉴 링크는 보이지 않는다
                for other, (other_url, _) in self.MENU_URLS.items():
                    if other != menu:
                        self.assertNotContains(response, f'href="{reverse("qms:" + other_url)}"')

    def test_inspection_view_alone_no_longer_shows_or_opens_other_menus(self):
        self.login(self.make_user('insp-only', 'can_qms_inspection_view'))
        page = self.client.get(reverse('qms:import_inspection_list'))
        for url_name in ('gauge_list', 'qdoc_list', 'nc_list', 'claim_list', 'rating_list', 'isir_list'):
            with self.subTest(url_name=url_name):
                self.assertNotContains(page, f'href="{reverse("qms:" + url_name)}"')
                self.assertEqual(self.client.get(reverse(f'qms:{url_name}')).status_code, 403)

    def test_dashboard_requires_some_qms_menu(self):
        self.login(self.make_user('no-qms'))
        self.assertEqual(self.client.get(reverse('qms:dashboard')).status_code, 403)
        self.login(self.make_user('gauge-only', 'can_qms_gauge_view'))
        self.assertEqual(self.client.get(reverse('qms:dashboard')).status_code, 200)

    def test_menu_permission_edit_name(self):
        self.assertEqual(menu_permission('qdoc', edit=True), 'can_qms_doc_edit')
        self.assertEqual(menu_permission('m4'), 'can_qms_4m_view')


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class ImportInspectionReviewTests(QmsTestBase):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(code='IIR', name='Inspection vendor')
        self.part = Part.objects.create(part_no='IIR-P', part_name='Part', vendor=self.vendor)
        self.wh = Warehouse.objects.create(code='8100', name='Inspection')
        Warehouse.objects.create(code='2000', name='Good')
        Warehouse.objects.create(code='8200', name='Bad')
        self.trx = MaterialTransaction.objects.create(
            transaction_no='IIR-IN', transaction_type='IN_MANUAL', part=self.part, vendor=self.vendor,
            quantity=Decimal('100.0000'), lot_no=date(2026, 1, 1), warehouse_to=self.wh)
        self.stock = MaterialStock.objects.create(warehouse=self.wh, part=self.part,
                                                  lot_no=date(2026, 1, 1), quantity=100)
        self.inspection = ImportInspection.objects.create(inbound_transaction=self.trx, lot_no=date(2026, 1, 1))
        self.url = reverse('qms:import_inspection_detail', args=[self.inspection.pk])

    def test_view_only_user_sees_read_only_detail_and_cannot_judge(self):
        self.login(self.make_user('viewer', 'can_qms_inspection_view'))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '조회 전용')
        self.assertNotContains(response, 'value="COMPLETE"')
        with patch('material.erp_api.register_erp_incoming') as erp:
            response = self.client.post(self.url, {'decision': 'COMPLETE', 'qty_good': '100', 'qty_bad': '0'})
            erp.assert_not_called()
        self.assertEqual(response.status_code, 403)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, 'PENDING')

    def test_prefilled_quantity_has_no_decimals(self):
        self.login(self.make_user('judge', 'can_qms_inspection_view', 'can_qms_inspection_edit'))
        response = self.client.get(self.url)
        self.assertContains(response, 'name="qty_good" id="qtyGood"')
        self.assertContains(response, 'value="100"')
        self.assertNotContains(response, '100.0000')
        self.assertContains(response, 'value="COMPLETE"')

    def test_decimal_and_blank_inputs_get_friendly_errors_and_change_nothing(self):
        self.login(self.make_user('judge2', 'can_qms_inspection_view', 'can_qms_inspection_edit'))
        for good, bad, expected in [('99.5', '0.5', '정수'), ('', '100', '입력해 주세요'), ('abc', '0', '숫자로')]:
            with self.subTest(good=good), patch('material.erp_api.register_erp_incoming') as erp:
                response = self.client.post(self.url, {'decision': 'COMPLETE', 'qty_good': good, 'qty_bad': bad},
                                            follow=True)
                erp.assert_not_called()
                self.assertContains(response, expected)
                self.inspection.refresh_from_db()
                self.assertEqual(self.inspection.status, 'PENDING')

    def test_integral_decimal_text_is_accepted(self):
        self.login(self.make_user('judge3', 'can_qms_inspection_view', 'can_qms_inspection_edit'))
        with patch('material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)):
            self.client.post(self.url, {'decision': 'COMPLETE', 'qty_good': '90.0000', 'qty_bad': '10'})
        self.inspection.refresh_from_db()
        self.assertEqual((self.inspection.status, self.inspection.qty_good, self.inspection.qty_bad),
                         ('APPROVED', 90, 10))

    def test_fractional_receipt_cannot_be_judged(self):
        MaterialTransaction.objects.filter(pk=self.trx.pk).update(quantity=Decimal('12.5'))
        self.login(self.make_user('judge4', 'can_qms_inspection_view', 'can_qms_inspection_edit'))
        self.assertContains(self.client.get(self.url), '정수가 아니어서')
        with patch('material.erp_api.register_erp_incoming') as erp:
            response = self.client.post(self.url, {'decision': 'COMPLETE', 'qty_good': '12', 'qty_bad': '0'},
                                        follow=True)
            erp.assert_not_called()
        self.assertContains(response, '정수가 아니어서')
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, 'PENDING')

    def test_partial_defect_keeps_status_but_shows_partial_label(self):
        ImportInspection.objects.filter(pk=self.inspection.pk).update(status='APPROVED', qty_good=90, qty_bad=10)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, 'APPROVED')
        self.assertTrue(self.inspection.is_partial)
        self.assertEqual(self.inspection.result_label, '부분 합격')
        self.login(self.make_user('viewer2', 'can_qms_inspection_view'))
        self.assertContains(self.client.get(reverse('qms:import_inspection_list')), '부분 합격')
        self.assertContains(self.client.get(self.url), '부분 합격')
        partial = self.client.get(reverse('qms:import_inspection_list'), {'status': 'PARTIAL'})
        self.assertEqual(len(partial.context['page_obj']), 1)
        passed = self.client.get(reverse('qms:import_inspection_list'), {'status': 'PASSED'})
        self.assertEqual(len(passed.context['page_obj']), 0)


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class VendorRatingCalculationTests(QmsTestBase):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(code='VR', name='Rating vendor')
        self.part = Part.objects.create(part_no='VR-P', part_name='Part', vendor=self.vendor)
        self.user = self.make_user('rater', 'can_qms_rating_view', 'can_qms_rating_edit')
        self.login(self.user)
        wh = Warehouse.objects.create(code='8100', name='Inspection')
        self.month_day = date(2026, 9, 15)
        for idx, (status, good, bad) in enumerate([('APPROVED', 100, 0), ('APPROVED', 90, 10),
                                                    ('REJECTED', 0, 100), ('PENDING', 0, 0)]):
            trx = MaterialTransaction.objects.create(
                transaction_no=f'VR-IN-{idx}', transaction_type='IN_MANUAL', part=self.part, vendor=self.vendor,
                quantity=100, warehouse_to=wh,
                date=timezone.make_aware(timezone.datetime(2026, 9, 15, 9, 0)))
            ImportInspection.objects.create(inbound_transaction=trx, status=status, qty_good=good, qty_bad=bad)

    def create_rating(self):
        self.client.post(reverse('qms:rating_create'), {
            'vendor': self.vendor.organization.pk, 'year': 2026, 'month': 9})
        return VendorRating.objects.get(vendor=self.vendor.organization, year=2026, month=9)

    def test_partial_defects_are_not_passes_and_pending_is_excluded(self):
        rating = self.create_rating()
        self.assertEqual(rating.incoming_total, 3)
        self.assertEqual(rating.incoming_pass, 1)
        self.assertEqual(rating.incoming_fail, 2)
        self.assertAlmostEqual(float(rating.incoming_rate), 33.33, places=2)
        self.assertEqual(rating.total_incoming_qty, 300)
        self.assertEqual(rating.defect_qty, 110)

    def test_delivery_early_and_ontime_count_as_kept_late_counts_as_late(self):
        Order.objects.create(vendor=self.vendor, part_no='VR-P', part_name='Part', quantity=10,
                             due_date=date(2026, 9, 20), erp_order_no='PO-1', erp_order_seq='1')
        Order.objects.create(vendor=self.vendor, part_no='VR-P', part_name='Part', quantity=10,
                             due_date=date(2026, 9, 5), erp_order_no='PO-2', erp_order_seq='1')
        # 납기보다 일찍(9/10, PO-1 납기 9/20) → 준수
        Incoming.objects.create(part=self.part, in_date=date(2026, 9, 10), quantity=10,
                                erp_order_no='PO-1', erp_order_seq='1')
        # 납기 당일 → 준수
        Incoming.objects.create(part=self.part, in_date=date(2026, 9, 20), quantity=10,
                                erp_order_no='PO-1', erp_order_seq='1')
        # 납기(9/5) 이후 9/8 입고 → 지연
        Incoming.objects.create(part=self.part, in_date=date(2026, 9, 8), quantity=10,
                                erp_order_no='PO-2', erp_order_seq='1')
        # 발주를 찾을 수 없는 입고 → 집계 제외(무조건 준수 아님)
        other = Part.objects.create(part_no='VR-X', part_name='No order', vendor=self.vendor)
        Incoming.objects.create(part=other, in_date=date(2026, 9, 9), quantity=10)
        rating = self.create_rating()
        self.assertEqual((rating.delivery_total, rating.delivery_ontime, rating.delivery_late), (3, 2, 1))

    def test_delivery_without_erp_number_uses_nearest_due_date(self):
        Order.objects.create(vendor=self.vendor, part_no='VR-P', part_name='Part', quantity=10,
                             due_date=date(2026, 9, 12))
        Order.objects.create(vendor=self.vendor, part_no='VR-P', part_name='Part', quantity=10,
                             due_date=date(2026, 10, 30))
        Incoming.objects.create(part=self.part, in_date=date(2026, 9, 10), quantity=10)  # 이른 납품
        Incoming.objects.create(part=self.part, in_date=date(2026, 9, 14), quantity=10)  # 이틀 지연
        rating = self.create_rating()
        self.assertEqual((rating.delivery_total, rating.delivery_ontime, rating.delivery_late), (2, 1, 1))

    def test_claims_and_ppm_reduce_quality_score(self):
        base = self.create_rating()
        base_quality = float(base.quality_score)
        expected = float(base.incoming_rate) - min(10, float(base.ppm) / 1000)
        self.assertAlmostEqual(base_quality, max(0, expected), places=1)
        VendorClaim.objects.create(claim_no='VR-CL', vendor=self.vendor.organization,
                                   issue_date=date(2026, 9, 3), part_no='VR-P', part_name='Part',
                                   claim_qty=1, issued_by=self.user)
        rating = self.create_rating()
        self.assertEqual(rating.claim_count, 1)
        self.assertAlmostEqual(float(rating.quality_score), max(0, expected - 5), places=1)

    def test_score_without_penalties_is_rate(self):
        rating = VendorRating(incoming_rate=100, delivery_rate=100, ppm=0, claim_count=0)
        rating.calculate_scores()
        self.assertEqual((float(rating.total_score), rating.grade), (100.0, 'A'))
        rating = VendorRating(incoming_rate=100, delivery_rate=100, ppm=50000, claim_count=10)
        rating.calculate_scores()
        self.assertEqual(float(rating.quality_score), 70.0)  # -10 (PPM 상한) -20 (클레임 상한)


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class PreM4VendorAccessTests(QmsTestBase):
    def setUp(self):
        super().setUp()
        self.author = self.make_user('author', 'can_qms_4m_view', 'can_qms_4m_edit')
        self.a = Vendor.objects.create(code='M4A', name='Vendor A')
        self.b = Vendor.objects.create(code='M4B', name='Vendor B')
        self.item_a = M4Request.objects.create(user=self.author, vendor_org=self.a.organization, part_no='P',
                                               part_name='Part', request_no='4M-A-1', m4_type='MAN')
        self.item_none = M4Request.objects.create(user=self.author, part_no='P', part_name='Part',
                                                  request_no='4M-N-1', m4_type='MAN')
        self.vendor_user = self.make_user('vendor-a', 'can_qms_4m_view', vendor=self.a)

    def test_registered_vendor_can_open_and_reply_with_view_permission_only(self):
        self.login(self.vendor_user)
        listing = self.client.get(reverse('qms:m4_list'))
        self.assertContains(listing, '4M-A-1')
        self.assertNotContains(listing, '4M-N-1')
        detail = self.client.get(reverse('qms:m4_detail', args=[self.item_a.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, '협력사 답변/증빙 제출')
        self.assertFalse(M4Review.objects.exists())  # 조회만으로 회신 행을 만들지 않음
        response = self.client.post(reverse('qms:m4_vendor_response', args=[self.item_a.pk]),
                                    {'content': '조치 완료'})
        self.assertEqual(response.status_code, 302)
        review = M4Review.objects.get(request=self.item_a)
        self.assertEqual((review.content, review.reviewer), ('조치 완료', self.vendor_user))

    def test_other_vendor_and_unassigned_documents_are_blocked(self):
        other = self.make_user('vendor-b', 'can_qms_4m_view', vendor=self.b)
        self.login(other)
        self.assertEqual(self.client.get(reverse('qms:m4_detail', args=[self.item_a.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('qms:m4_vendor_response', args=[self.item_a.pk]),
                                          {'content': 'x'}).status_code, 403)
        self.login(self.vendor_user)
        self.assertEqual(self.client.get(reverse('qms:m4_detail', args=[self.item_none.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('qms:m4_vendor_response', args=[self.item_none.pk]),
                                          {'content': 'x'}).status_code, 403)
        self.assertFalse(M4Review.objects.exists())

    def test_internal_user_sees_documents_without_vendor(self):
        self.login(self.make_user('internal-viewer', 'can_qms_4m_view'))
        self.assertContains(self.client.get(reverse('qms:m4_list')), '4M-N-1')
        self.assertEqual(self.client.get(reverse('qms:m4_detail', args=[self.item_none.pk])).status_code, 200)

    def test_vendor_view_permission_does_not_open_other_4m_edits(self):
        self.login(self.vendor_user)
        self.assertEqual(self.client.post(reverse('qms:m4_update', args=[self.item_a.pk]), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse('qms:add_m4_review', args=[self.item_a.pk]), {}).status_code, 403)

    def test_dashboard_counts_pre_and_formal_4m(self):
        M4Request.objects.filter(pk=self.item_a.pk).update(status='PENDING_REVIEW')
        approved = M4Request.objects.create(user=self.author, part_no='P', part_name='Part',
                                            request_no='4M-AP-1', m4_type='MAN', status='APPROVED')
        Formal4MRequest.objects.create(pre_request=approved, formal_no='FORMAL-1')
        self.login(self.make_user('internal-dash', 'can_qms_4m_view'))
        stats = self.client.get(reverse('qms:dashboard')).context['change_stats']
        self.assertEqual(stats, {'review': 1, 'formal': 1})


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class RetiredAndRepairedScreensTests(QmsTestBase):
    def test_v2_change_request_screens_are_retired(self):
        self.login(self.make_user('m4', 'can_qms_4m_view', 'can_qms_4m_edit'))
        for url_name in ('change_request_list', 'change_request_detail', 'vendor_response_create',
                         'document_upload', 'validity_evaluation', 'approval_step_process',
                         'formal4m_inspection_add', 'formal4m_schedule_add'):
            with self.subTest(url_name=url_name), self.assertRaises(NoReverseMatch):
                reverse(f'qms:{url_name}', args=[1])
        response = self.client.get('/qms/change/5/')
        self.assertContains(response, '더 이상 사용하지 않습니다')
        self.assertNotContains(self.client.get(reverse('qms:m4_list')), '4M 변경관리(v2)')

    def test_outgoing_blank_sample_quantity_defaults_to_zero(self):
        self.login(self.make_user('oi', 'can_qms_inspection_view', 'can_qms_inspection_edit'))
        response = self.client.post(reverse('qms:outgoing_create'), {
            'inspection_date': '2026-10-22', 'part_no': 'P', 'part_name': 'Part',
            'total_qty': '10', 'sample_qty': ''})
        oi = OutgoingInspection.objects.get()
        self.assertRedirects(response, reverse('qms:outgoing_detail', args=[oi.pk]), fetch_redirect_response=False)
        self.assertEqual(oi.sample_qty, 0)
        response = self.client.post(reverse('qms:outgoing_create'), {
            'inspection_date': '2026-10-22', 'part_no': 'P', 'part_name': 'Part',
            'total_qty': '10', 'sample_qty': '-3'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '0 이상')
        self.assertEqual(OutgoingInspection.objects.count(), 1)

    def make_doc(self, user):
        return QualityDocument.objects.create(doc_no='QD-1', doc_name='검사기준서', category='INSPECTION',
                                              effective_date=date(2026, 1, 1), created_by=user,
                                              file='qms/documents/a.pdf', file_name='a.pdf')

    def test_qdoc_detail_renders_and_actions_work(self):
        editor = self.make_user('qd', 'can_qms_doc_view', 'can_qms_doc_edit')
        doc = self.make_doc(editor)
        self.login(editor)
        url = reverse('qms:qdoc_detail', args=[doc.pk])
        page = self.client.get(url)
        self.assertContains(page, 'QD-1')
        self.assertContains(page, '검사기준서')
        self.client.post(url, {'action': 'update_status', 'status': 'APPROVED'})
        doc.refresh_from_db()
        self.assertEqual((doc.status, doc.approved_by), ('APPROVED', editor))
        self.client.post(url, {'action': 'add_revision', 'change_reason': '치수 기준 변경', 'new_version': '2.0'})
        doc.refresh_from_db()
        self.assertEqual((doc.revision, doc.version, doc.status), (1, '2.0', 'DRAFT'))
        self.assertEqual(doc.revisions.get().change_reason, '치수 기준 변경')
        self.client.post(url, {'action': 'update', 'doc_no': 'QD-1A', 'doc_name': '검사기준서 B',
                               'category': 'SOP', 'effective_date': '2026-02-01'})
        doc.refresh_from_db()
        self.assertEqual((doc.doc_no, doc.doc_name, doc.category), ('QD-1A', '검사기준서 B', 'SOP'))
        self.assertContains(self.client.get(url), '치수 기준 변경')

    def test_qdoc_actions_need_edit_permission(self):
        editor = self.make_user('qd-owner', 'can_qms_doc_view', 'can_qms_doc_edit')
        doc = self.make_doc(editor)
        self.login(self.make_user('qd-viewer', 'can_qms_doc_view'))
        url = reverse('qms:qdoc_detail', args=[doc.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {'action': 'update_status', 'status': 'OBSOLETE'}).status_code, 403)
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'DRAFT')


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class CapaPermissionSplitTests(QmsTestBase):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(code='CAPA', name='CAPA vendor')
        self.internal = self.make_user('qa', 'can_qms_nc_view', 'can_qms_nc_edit')
        self.vendor_user = self.make_user('capa-vendor', 'can_qms_nc_view', 'can_qms_nc_edit', vendor=self.vendor)
        self.capa = CorrectiveAction.objects.create(
            vendor=self.vendor.organization, part_no='P', part_name='Part', issue_title='치수 불량',
            issue_detail='상세', request_date=date.today(), due_date=date.today() + timedelta(days=7),
            requested_by=self.internal)
        self.url = reverse('qms:capa_detail', args=[self.capa.pk])

    def test_vendor_can_reply_but_not_verify_or_close(self):
        self.login(self.vendor_user)
        self.client.post(self.url, {'action': 'vendor_response', 'cause_analysis': '금형 마모'})
        self.capa.refresh_from_db()
        self.assertEqual((self.capa.cause_analysis, self.capa.status), ('금형 마모', 'ANALYZING'))
        for data in ({'action': 'update_status', 'status': 'CLOSED'},
                     {'action': 'verify', 'is_effective': 'yes'}):
            with self.subTest(action=data['action']):
                self.assertEqual(self.client.post(self.url, data).status_code, 403)
        self.assertEqual(self.client.post(reverse('qms:capa_delete', args=[self.capa.pk])).status_code, 403)
        self.capa.refresh_from_db()
        self.assertEqual(self.capa.status, 'ANALYZING')

    def test_internal_verify_not_effective_goes_back_to_action(self):
        self.login(self.internal)
        self.client.post(self.url, {'action': 'verify', 'is_effective': 'no', 'verification_result': '재발'})
        self.capa.refresh_from_db()
        self.assertEqual((self.capa.status, self.capa.is_effective), ('ACTION', False))
        self.client.post(self.url, {'action': 'update_status', 'status': 'CLOSED'})
        self.capa.refresh_from_db()
        self.assertEqual(self.capa.status, 'CLOSED')
