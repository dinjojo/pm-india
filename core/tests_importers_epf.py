"""EPF has no file importer: it enters through the photo scan (core.ocr +
views.scan) or the manual add form. These tests pin the parts of the scan
path that EPF relies on: hint -> EPF type, amount-only draft rows with the
passbook's dates and closing balance, and the confirm step turning reviewed
rows into one EPF holding valued at the passbook balance."""
import unittest
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Asset, Holding
from core.ocr import guess_type, parse_slip

PASSBOOK = """EMPLOYEES PROVIDENT FUND ORGANISATION
Cont. For Due-Month 042026 15-05-2026 7,200 2,203 4,997
Cont. For Due-Month 052026 15-06-2026 7,200 2,203 4,997
Closing Balance 6,12,345
"""


class EpfScanDraftTests(TestCase):
    def test_epf_hints(self):
        for hint in ("EPF passbook", "EPFO member passbook", "PF statement"):
            self.assertEqual(guess_type(hint), Asset.AssetType.EPF, hint)

    def test_passbook_drafts_amount_only_rows_and_balance(self):
        draft = parse_slip(PASSBOOK, "EPF passbook")
        self.assertEqual(draft.asset_type, Asset.AssetType.EPF)
        self.assertEqual(draft.closing_balance, "612345")
        self.assertEqual([r.date for r in draft.rows], ["2026-05-15", "2026-06-15"])
        for row in draft.rows:
            self.assertEqual((row.asset_type, row.kind, row.quantity, row.price),
                             (Asset.AssetType.EPF, "buy", "", ""))


class EpfScanConfirmTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("epf", "epf@x.in", "p")
        self.client.force_login(self.user)

    def _post(self, rows, closing=""):
        data = {"step": "confirm", "row_count": str(len(rows)), "closing_value": closing}
        for i, row in enumerate(rows):
            for k, v in row.items():
                data[f"row_{i}_{k}"] = v
        return self.client.post(reverse("scan"), data)

    def test_reviewed_rows_become_one_epf_holding_at_passbook_balance(self):
        row = {"name": "EPF — UAN 1001", "asset_type": "epf", "kind": "buy", "amount": "9403"}
        resp = self._post([dict(row, date="2026-05-15"), dict(row, date="2026-06-15"),
                           dict(row, date="2026-07-01", kind="sell", amount="5000")],
                          closing="6,12,345")
        self.assertEqual(resp.status_code, 302)
        h = Holding.objects.get(account__user=self.user)
        self.assertEqual(h.asset.asset_type, Asset.AssetType.EPF)
        self.assertEqual(h.quantity, Decimal("13806"))       # deposits − withdrawal
        self.assertEqual(h.current_value, Decimal("612345"))

    def test_bad_row_is_reported_and_others_imported(self):
        good = {"name": "EPF", "asset_type": "epf", "kind": "buy", "date": "2026-05-15", "amount": "100"}
        self._post([good, dict(good, amount="12,O00")])
        self.assertEqual(Holding.objects.get(account__user=self.user).quantity, Decimal("100"))
