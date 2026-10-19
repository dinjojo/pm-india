"""CAS PDF importer against the casparser 1.x pydantic API.

Uses fake pydantic-model-shaped objects (the real models from casparser.types
built in memory) so both issuer shapes are covered without a real PDF:
- CAMS/KFin CASData: folios -> schemes -> typed transactions
- NSDL/CDSL NSDLCASData: demat accounts with position snapshots
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from core.importers import cas as cas_mod
from core.models import Account, Asset, Transaction
from casparser.types import CASFileType, FileType
from casparser.types import (
    CASData, DematAccount, Equity, Folio, InvestorInfo, MutualFund,
    NSDLCASData, Scheme, SchemeValuation, StatementPeriod, TransactionData,
    TransactionType,
)


def _tx(ttype, d, units, nav, amount):
    return TransactionData(date=d, description=ttype, amount=amount, units=units,
                           nav=nav, balance=Decimal("0"), type=ttype)


class CamKfinImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@x.in", "p")
        self.cas = CASData(
            statement_period=StatementPeriod(from_="2026-04-01", to="2026-10-18"),
            investor_info=InvestorInfo(name="Dinesh", email="d@x.in", address="a", mobile=""),
            cas_type=CASFileType.DETAILED, file_type=FileType.CAMS,
            folios=[Folio(folio="1234567/88", amc="HDFC AMC", schemes=[
                Scheme(scheme="HDFC Large Cap Fund - Direct Plan - Growth",
                       advisor="", rta_code="L", rta="CAMS", isin="INF179K01XQ0",
                       open=Decimal("0"), close=Decimal("100"), close_calculated=Decimal("100"),
                       valuation=SchemeValuation(date="2026-10-18", nav=Decimal("50"),
                                                 cost=Decimal("5000"), value=Decimal("5000")),
                       transactions=[
                           _tx(TransactionType.PURCHASE, "2026-05-02", Decimal("100"), Decimal("50"), Decimal("5000")),
                           _tx(TransactionType.DIVIDEND_PAYOUT, "2026-06-02", None, None, Decimal("250")),
                           _tx(TransactionType.STT_TAX, "2026-05-02", None, None, Decimal("5")),
                       ]),
            ])],
        )

    def test_transactions_imported_with_kinds(self):
        result = cas_mod.import_cas_pdf.__wrapped__ if hasattr(cas_mod.import_cas_pdf, "__wrapped__") else None
        # bypass the PDF: call the internal flow the same way the PDF path would
        r = self._import(self.cas)
        self.assertEqual(r.transactions_created, 2)
        txs = Transaction.objects.all().order_by("date")
        self.assertEqual(txs[0].kind, Transaction.Kind.BUY)
        self.assertEqual(txs[0].quantity, Decimal("100"))
        self.assertEqual(txs[0].price, Decimal("50"))
        self.assertEqual(txs[1].kind, Transaction.Kind.DIVIDEND)
        self.assertEqual(txs[1].amount, Decimal("250"))
        self.assertEqual(r.folios, 1)

    def test_reimport_is_idempotent(self):
        self._import(self.cas)
        again = self._import(self.cas)
        self.assertEqual(again.transactions_created, 0)
        self.assertEqual(Transaction.objects.count(), 2)

    def test_asset_direct_flag(self):
        self._import(self.cas)
        asset = Asset.objects.get(asset_type=Asset.AssetType.MUTUAL_FUND)
        self.assertTrue(asset.is_direct)
        self.assertEqual(asset.isin, "INF179K01XQ0")

    def _import(self, cas):
        result = cas_mod.CasImportResult()
        account, _ = Account.objects.get_or_create(user=self.user, name="CAS — T")
        cas_mod._import_cams_kfin(cas, result, account)
        result.folios = len(cas.folios)
        return result


class NsdlImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u2", "u2@x.in", "p")
        self.cas = NSDLCASData(
            statement_period=StatementPeriod(from_="2026-04-01", to="2026-10-18"),
            investor_info=InvestorInfo(name="Dinesh", email="d@x.in", address="a", mobile=""),
            cas_type=CASFileType.DETAILED, file_type=FileType.CAMS,
            accounts=[DematAccount(
                name="NSDEMAT", type="NSDL", dp_id="IN30", client_id="12345678",
                folios=2, balance=Decimal("1"), owners=[],
                equities=[Equity(name="Reliance Industries", isin="INE002A01018",
                                 num_shares=Decimal("10"), price=Decimal("2500"), value=Decimal("25000"))],
                mutual_funds=[MutualFund(name="Parag Parikh Flexi Cap - Direct", isin="INF879O01019",
                                         balance=Decimal("500"), nav=Decimal("80"), value=Decimal("40000"),
                                         avg_cost=Decimal("60"), total_cost=Decimal("30000"))],
            )],
        )

    def test_demat_snapshot_imported_as_opening_positions(self):
        result = cas_mod.CasImportResult()
        account, _ = Account.objects.get_or_create(user=self.user, name="CAS — T")
        cas_mod._import_nsdlcdsl(self.cas, result, account)
        self.assertEqual(result.transactions_created, 2)
        self.assertEqual(result.demat_positions, 2)
        stock = Asset.objects.get(asset_type=Asset.AssetType.STOCK)
        self.assertEqual(stock.identifier, "INE002A01018")
        stx = stock.transactions.get()
        self.assertEqual(stx.quantity, Decimal("10"))
        self.assertEqual(stx.amount, Decimal("25000"))
        mf = Asset.objects.get(asset_type=Asset.AssetType.MUTUAL_FUND)
        mtx = mf.transactions.get()
        self.assertEqual(mtx.price, Decimal("60"))          # avg_cost preferred
        self.assertEqual(mtx.amount, Decimal("30000"))      # total_cost preferred
