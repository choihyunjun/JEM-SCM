from datetime import date
from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.http import Http404, HttpResponse

from orders.access import has_permission
from orders.models import Vendor, Order, UserProfile
from orders.views import order_export, role_has_menu_perm
from qms import views as qms
from qms.models import NonConformance, VendorClaim, OutgoingInspection


class AccessSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='viewer')
        self.profile, _ = UserProfile.objects.get_or_create(user=self.user)
        self.user.profile = self.profile
        self.profile.role = 'STAFF'
        self.profile.account_type = 'INTERNAL'
        self.profile.can_scm_order_view = True
        self.profile.can_qms_inspection_view = True
        self.profile.can_qms_nc_view = True
        self.profile.can_qms_claim_view = True
        self.profile.save()
        self.a = Vendor.objects.create(code='A', name='Vendor A')
        self.b = Vendor.objects.create(code='B', name='Vendor B')
        for vendor in (self.a, self.b):
            Order.objects.create(vendor=vendor, part_no=vendor.code, part_name='Part', quantity=10, due_date=date.today())
        self.nc = NonConformance.objects.create(nc_no='NC-A', vendor=self.a.organization,
            source='INCOMING', occurred_date=date.today(), part_no='P', part_name='Part', defect_qty=1)
        self.claim = VendorClaim.objects.create(claim_no='CL-A', vendor=self.a.organization,
            issue_date=date.today(), part_no='P', part_name='Part', claim_qty=1, issued_by=self.user)
        self.outgoing = OutgoingInspection.objects.create(inspection_no='OI-A',
            inspection_date=date.today(), part_no='P', part_name='Part', total_qty=10)

    def request(self, data=None):
        request = RequestFactory().post('/', data) if data is not None else RequestFactory().get('/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def vendor_account(self, vendor=None):
        self.profile.role = 'VENDOR'
        self.profile.account_type = 'VENDOR'
        self.profile.org = vendor.organization if vendor else None
        self.profile.save()

    def test_revoked_permission_does_not_fall_back_to_role_or_legacy(self):
        self.profile.can_scm_order_view = False
        self.profile.can_view_orders = True
        self.profile.save()
        self.assertFalse(role_has_menu_perm(self.user, 'can_view_orders'))
        self.assertFalse(has_permission(self.user, 'can_scm_order_view'))

    def test_qms_legacy_flag_cannot_restore_edit_permission(self):
        self.profile.can_qms_inspection = True
        self.profile.save()
        response = qms.outgoing_inspection_detail(self.request({'action': 'update_result', 'status': 'PASS'}), self.outgoing.pk)
        self.assertEqual(response.status_code, 403)
        self.outgoing.refresh_from_db()
        self.assertEqual(self.outgoing.status, 'PENDING')

    def test_readonly_account_can_view_but_cannot_save_detail_forms(self):
        for view, obj, data in (
            (qms.outgoing_inspection_detail, self.outgoing, {'action': 'update_result', 'status': 'PASS'}),
            (qms.nc_detail, self.nc, {'action': 'update', 'status': 'CLOSED'}),
            (qms.claim_detail, self.claim, {'action': 'issue'}),
        ):
            with self.subTest(view=view.__name__):
                self.assertEqual(view(self.request(data), obj.pk).status_code, 403)
                with patch('qms.views.render', return_value=HttpResponse()) as render:
                    self.assertEqual(view(self.request(), obj.pk).status_code, 200)
                    render.assert_called_once()

    def test_editor_can_still_update_inspection(self):
        self.profile.can_qms_inspection_edit = True
        self.profile.save()
        with patch('qms.views.render', return_value=HttpResponse()):
            qms.outgoing_inspection_detail(self.request({'action': 'update_result', 'status': 'PASS', 'pass_qty': 10}), self.outgoing.pk)
        self.outgoing.refresh_from_db()
        self.assertEqual(self.outgoing.status, 'PASS')

    def test_vendor_excel_is_scoped_and_unlinked_vendor_is_empty(self):
        for vendor, expected in ((self.a, 2), (None, 1)):
            self.vendor_account(vendor)
            response = order_export(self.request())
            if vendor is None:
                self.assertEqual(response.status_code, 403)
                continue
            rows = list(openpyxl.load_workbook(BytesIO(response.content)).active.values)
            self.assertEqual(len(rows), expected)
            if vendor:
                self.assertEqual(rows[1][3], vendor.name)

    def test_internal_excel_still_includes_all_vendors(self):
        response = order_export(self.request())
        self.assertEqual(openpyxl.load_workbook(BytesIO(response.content)).active.max_row, 3)

    def test_other_vendor_detail_and_write_are_not_found(self):
        self.vendor_account(self.b)
        self.profile.can_qms_nc_edit = self.profile.can_qms_claim_edit = True
        self.profile.save()
        for view, obj in ((qms.nc_detail, self.nc), (qms.claim_detail, self.claim)):
            for data in (None, {'action': 'update', 'status': 'CLOSED'}):
                with self.subTest(view=view.__name__, data=data), self.assertRaises(Http404):
                    view(self.request(data), obj.pk)

    def test_own_vendor_list_and_detail_work(self):
        self.vendor_account(self.a)
        with patch('qms.views.render', return_value=HttpResponse()) as render:
            qms.nc_detail(self.request(), self.nc.pk)
            self.assertEqual(render.call_args.args[2]['nc'].pk, self.nc.pk)
        with patch('qms.views.render', return_value=HttpResponse()) as render:
            qms.nc_list(self.request())
            context = render.call_args.args[2]
            # All model querysets used to build the list are scoped, including counts.
            from qms.access import scoped
            self.assertEqual(list(scoped(self.request(), NonConformance)), [self.nc])

    def test_missing_vendor_link_blocks_qms(self):
        self.vendor_account()
        self.assertEqual(qms.nc_detail(self.request(), self.nc.pk).status_code, 403)

    def test_vendor_cannot_assign_record_to_another_vendor(self):
        self.vendor_account(self.a)
        self.profile.can_qms_nc_edit = True
        self.profile.save()
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            qms.nc_edit(self.request({'vendor': str(self.b.organization.pk)}), self.nc.pk)

    def test_readonly_template_has_disabled_forms_and_no_edit_link(self):
        request = self.request()
        response = qms.outgoing_inspection_detail(request, self.outgoing.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'disabled class="qms-readonly"')
        from django.urls import reverse
        self.assertNotContains(response, 'href="' + reverse('qms:outgoing_edit', args=[self.outgoing.pk]) + '"')

    def test_erp_recovery_requires_incoming_edit_permission(self):
        from material.erp_recovery import incoming_operations
        self.assertEqual(incoming_operations(self.request()).status_code, 302)
        self.profile.can_wms_incoming_process = True
        self.profile.save()
        self.assertEqual(incoming_operations(self.request()).status_code, 200)
