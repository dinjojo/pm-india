"""Bank statement CSV import (core.importers.banks) per BANK_PROVIDERS entry.

Inline fixtures mirror the real export headers of each bank; amounts use
Indian digit grouping. Covers normal statements, edge cases (newest-first
order, overdrawn 'Dr' balance, signed debits, two accounts) and malformed
rows that must become per-row errors, not crashes.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from core.importers.banks import BANK_PROVIDERS, _money, import_bank_statement
from core.models import Asset, Holding, Transaction

HDFC = b"""Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
01/04/26,SALARY APR,0000123,01/04/26,,"1,20,000.00","1,25,000.00"
03/04/26,UPI-RENT,0000124,03/04/26,"25,000.00",,"1,00,000.00"
05/04/26,SIP HDFC MF,0000125,05/04/26,"10,000.00",,"90,000.00"
"""

# ICICI net banking: newest first, "(INR )" headers.
ICICI = b"""S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )
1,10/04/2026,10/04/2026,-,ATM WDL,"2,000.00",0.00,"48,000.00"
2,05/04/2026,05/04/2026,-,NEFT IN,0.00,"30,000.00","50,000.00"
3,01/04/2026,01/04/2026,-,OPENING CHQ,"5,000.00",0.00,"20,000.00"
"""

# Yes Bank TSV; the account goes overdrawn ("Dr" balance) and the debit
# column carries a minus sign.
YES = (
    "Txn Date\tParticulars\tDebit\tCredit\tBalance\n"
    "02-Apr-2026\tCARD SPEND\t-1,500.00\t\t500.00 Cr\n"
    "04-Apr-2026\tEMI\t2,000.00\t\t1,500.00 Dr\n"
).encode()


class MoneyCellTests(TestCase):
    def test_indian_formats(self):
        self.assertEqual(_money("1,23,456.50"), Decimal("123456.50"))
        self.assertEqual(_money("₹50"), Decimal("50"))
        self.assertEqual(_money("(500.00)"), Decimal("-500.00"))
        self.assertEqual(_money("1,000.00 Cr"), Decimal("1000.00"))
        self.assertEqual(_money("1,000.00 Dr"), Decimal("-1000.00"))
        self.assertEqual(_money("1000.00Dr"), Decimal("-1000.00"))
        self.assertIsNone(_money("abc"))
        self.assertIsNone(_money(""))


class BankImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("b", "b@x.in", "p")

    def _holding(self, name):
        return Holding.objects.get(account__user=self.user, account__name=name)

    def test_every_provider_has_a_test_here(self):
        self.assertEqual({p.key for p in BANK_PROVIDERS}, {"hdfc", "icici", "yesbank", "generic"})

    def test_hdfc_credits_and_debits(self):
        r = import_bank_statement(self.user, HDFC, "hdfc", "HDFC Savings")
        self.assertEqual(r.errors, [])
        self.assertEqual(r.transactions_created, 3)
        txs = list(Transaction.objects.filter(source="bank").order_by("date"))
        self.assertEqual([t.kind for t in txs], ["buy", "sell", "sell"])
        self.assertEqual([t.quantity for t in txs],
                         [Decimal("120000"), Decimal("25000"), Decimal("10000")])
        self.assertEqual(txs[0].date, date(2026, 4, 1))
        self.assertEqual(r.closing_balance, Decimal("90000.00"))
        h = self._holding("HDFC Savings")
        self.assertEqual(h.asset.asset_type, Asset.AssetType.SAVINGS)
        self.assertEqual(h.quantity, Decimal("85000"))  # 1,20,000 in − 35,000 out
        self.assertEqual(h.current_value, Decimal("90000.00"))

    def test_icici_newest_first_uses_latest_balance(self):
        r = import_bank_statement(self.user, ICICI, "icici", "ICICI")
        self.assertEqual(r.errors, [])
        self.assertEqual(r.transactions_created, 3)
        self.assertEqual(r.closing_balance, Decimal("48000.00"))
        self.assertEqual(self._holding("ICICI").current_value, Decimal("48000.00"))

    def test_yesbank_tsv_signed_debit_and_overdrawn_balance(self):
        r = import_bank_statement(self.user, YES, "yesbank", "Yes")
        self.assertEqual(r.errors, [])
        txs = list(Transaction.objects.filter(source="bank").order_by("date"))
        self.assertEqual([(t.kind, t.quantity) for t in txs],
                         [("sell", Decimal("1500")), ("sell", Decimal("2000"))])
        self.assertEqual(r.closing_balance, Decimal("-1500.00"))
        self.assertEqual(self._holding("Yes").current_value, Decimal("-1500.00"))

    def test_generic_auto_detects_provider(self):
        r = import_bank_statement(self.user, ICICI, "generic", "Auto")
        self.assertEqual(r.provider, "icici")
        self.assertEqual(r.transactions_created, 3)
        plain = b"Date,Description,Debit,Credit,Balance\n2026-04-01,Interest,,120.50,10120.50\n"
        r2 = import_bank_statement(self.user, plain, "generic", "Plain")
        self.assertEqual(r2.errors, [])
        self.assertEqual(r2.transactions_created, 1)
        self.assertEqual(r2.closing_balance, Decimal("10120.50"))

    def test_reimport_is_idempotent(self):
        import_bank_statement(self.user, HDFC, "hdfc", "HDFC Savings")
        again = import_bank_statement(self.user, HDFC, "hdfc", "HDFC Savings")
        self.assertEqual(again.transactions_created, 0)
        self.assertIn("already imported", again.errors[0])
        self.assertEqual(Transaction.objects.filter(source="bank").count(), 3)

    def test_closing_balance_only_touches_this_users_account(self):
        other = User.objects.create_user("o", "o@x.in", "p")
        import_bank_statement(other, HDFC, "hdfc", "HDFC Savings")
        later = HDFC.replace(b"05/04/26,SIP", b"06/04/26,SIP").replace(b'"90,000.00"', b'"91,000.00"')
        import_bank_statement(self.user, later, "hdfc", "HDFC Savings")
        self.assertEqual(Holding.objects.get(account__user=other).current_value, Decimal("90000.00"))
        self.assertEqual(self._holding("HDFC Savings").current_value, Decimal("91000.00"))

    def test_malformed_amount_is_a_row_error(self):
        bad = HDFC + b'07/04/26,TYPO,0000126,07/04/26,"12,0O0.00",,"78,000.00"\n'
        r = import_bank_statement(self.user, bad, "hdfc", "HDFC Savings")
        self.assertEqual(r.transactions_created, 3)
        self.assertEqual(len(r.errors), 1)
        self.assertIn("Row 5", r.errors[0])
        self.assertIn("12,0O0.00", r.errors[0])

    def test_placeholder_cells_and_junk_rows_are_skipped_quietly(self):
        csv = (b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
               b"********,,,,\n"
               b"01/04/26,FD INTEREST,-,500.00,500.00\n"
               b"STATEMENT SUMMARY,,,,\n")
        r = import_bank_statement(self.user, csv, "hdfc", "HDFC")
        self.assertEqual(r.errors, [])
        self.assertEqual(r.transactions_created, 1)
        self.assertEqual(r.skipped, 2)

    def test_unusable_files_give_clean_errors(self):
        self.assertIn("Unknown bank", import_bank_statement(self.user, HDFC, "sbi", "x").errors[0])
        self.assertIn("no header", import_bank_statement(self.user, b"", "hdfc", "x").errors[0])
        r = import_bank_statement(self.user, b"Date,Amount\n01/04/26,5\n", "hdfc", "x")
        self.assertIn("withdrawal column", r.errors[0])
        r = import_bank_statement(self.user, HDFC.splitlines()[0] + b"\n", "hdfc", "x")
        self.assertEqual(r.errors, ["No transactions found in this file."])
        self.assertFalse(Transaction.objects.exists())
