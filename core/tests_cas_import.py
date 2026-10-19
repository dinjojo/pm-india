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
    Bond, CASData, DematAccount, Equity, Folio, InvestorInfo, MutualFund,
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


def _scheme(name, isin, txs, advisor=""):
    return Scheme(scheme=name, advisor=advisor, rta_code="X", rta="CAMS", isin=isin,
                  open=Decimal("0"), close=Decimal("0"), close_calculated=Decimal("0"),
                  valuation=SchemeValuation(date="2026-10-18", nav=Decimal("10"),
                                            cost=Decimal("0"), value=Decimal("0")),
                  transactions=txs)


def _cams(folios):
    return CASData(
        statement_period=StatementPeriod(from_="01-Apr-2026", to="18-Oct-2026"),
        investor_info=InvestorInfo(name="Dinesh", email="d@x.in", address="a", mobile=""),
        cas_type=CASFileType.DETAILED, file_type=FileType.CAMS, folios=folios)


def _nsdl(accounts, to="18-Oct-2026"):
    return NSDLCASData(
        statement_period=StatementPeriod(from_="01-Apr-2026", to=to),
        investor_info=InvestorInfo(name="Dinesh", email="d@x.in", address="a", mobile=""),
        cas_type=CASFileType.DETAILED, file_type=FileType.NSDL, accounts=accounts)


def _demat(dp_id, client_id, equities=(), mfs=(), bonds=()):
    return DematAccount(name=f"{dp_id} demat", type="NSDL", dp_id=dp_id, client_id=client_id,
                        folios=1, balance=Decimal("0"), owners=[], equities=list(equities),
                        mutual_funds=list(mfs), bonds=list(bonds))


class CamsKfinEdgeCaseTests(TestCase):
    """Folio-transaction branch: signs, multiple folios, scheme-name fallback."""

    def setUp(self):
        self.user = User.objects.create_user("e", "e@x.in", "p")
        self.account = Account.objects.create(user=self.user, name="CAS — E")

    def _run(self, cas):
        result = cas_mod.CasImportResult()
        cas_mod._import_cams_kfin(cas, result, self.account)
        return result

    def test_redemption_and_switch_out_reduce_the_holding(self):
        from core.portfolio import refresh_holdings
        cas = _cams([Folio(folio="111", amc="KFin AMC", schemes=[_scheme(
            "Axis Bluechip - Regular Growth", "INF846K01164", advisor="ARN-1234", txs=[
                _tx(TransactionType.PURCHASE_SIP, "2026-04-05", Decimal("100"), Decimal("50"), Decimal("5000")),
                # casparser signs outflows negative
                _tx(TransactionType.REDEMPTION, "2026-08-01", Decimal("-30"), Decimal("60"), Decimal("-1800")),
                _tx(TransactionType.SWITCH_OUT, "2026-09-01", Decimal("-20"), Decimal("55"), Decimal("-1100")),
            ])])])
        self.assertEqual(self._run(cas).transactions_created, 3)
        red = Transaction.objects.get(kind=Transaction.Kind.SELL)
        self.assertEqual((red.quantity, red.price, red.amount), (Decimal("30"), Decimal("60"), Decimal("1800")))
        sw = Transaction.objects.get(kind=Transaction.Kind.SWITCH_OUT)
        self.assertEqual(sw.quantity, Decimal("20"))
        refresh_holdings()
        h = self.account.holdings.get()
        self.assertEqual(h.quantity, Decimal("50"))
        self.assertEqual(h.invested, Decimal("2100"))
        self.assertFalse(h.asset.is_direct)  # regular plan with an ARN

    def test_reimport_repairs_rows_stored_with_casparser_signs(self):
        cas = _cams([Folio(folio="111", amc="A", schemes=[_scheme("F - Direct", "INF000000001", [
            _tx(TransactionType.REDEMPTION, "2026-08-01", Decimal("-30"), Decimal("60"), Decimal("-1800"))])])])
        self._run(cas)
        Transaction.objects.update(quantity=Decimal("-30"), amount=Decimal("-1800"))  # old importer output
        self.assertEqual(self._run(cas).transactions_created, 0)
        self.assertEqual(Transaction.objects.get().quantity, Decimal("30"))

    def test_same_scheme_same_day_in_two_folios_is_two_transactions(self):
        sip = lambda: [_tx(TransactionType.PURCHASE_SIP, "2026-05-05", Decimal("10"), Decimal("100"), Decimal("1000"))]
        cas = _cams([
            Folio(folio="AAA/1", amc="HDFC", schemes=[_scheme("HDFC Mid Cap - Direct", "INF179K01XX1", sip())]),
            Folio(folio="BBB/2", amc="HDFC", schemes=[_scheme("HDFC Mid Cap - Direct", "INF179K01XX1", sip())]),
        ])
        self.assertEqual(self._run(cas).transactions_created, 2)
        self.assertEqual(Asset.objects.filter(asset_type=Asset.AssetType.MUTUAL_FUND).count(), 1)
        self.assertEqual(self._run(cas).transactions_created, 0)

    def test_scheme_without_isin_and_nav_derives_price(self):
        cas = _cams([Folio(folio="9", amc="Old AMC", schemes=[_scheme("Legacy Scheme", None, [
            _tx(TransactionType.DIVIDEND_REINVEST, "2026-06-01", Decimal("4"), None, Decimal("50"))])])])
        self._run(cas)
        asset = Asset.objects.get(identifier="cas:Legacy Scheme")
        self.assertEqual(asset.transactions.get().price, Decimal("12.5"))

    def test_unreadable_date_is_a_warning_not_a_crash(self):
        cas = _cams([Folio(folio="9", amc="A", schemes=[_scheme("F", "INF000000002", [
            _tx(TransactionType.PURCHASE, "31-31-2026", Decimal("1"), Decimal("10"), Decimal("10")),
            _tx(TransactionType.PURCHASE, "2026-06-01", Decimal("1"), Decimal("10"), Decimal("10"))])])])
        r = self._run(cas)
        self.assertEqual(r.transactions_created, 1)
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("31-31-2026", r.warnings[0])


class NsdlCdslEdgeCaseTests(TestCase):
    """Demat-snapshot branch: real NSDL/CDSL period formats, multiple
    accounts, bonds, regular-plan detection, snapshot re-import."""

    def setUp(self):
        self.user = User.objects.create_user("n", "n@x.in", "p")
        self.account = Account.objects.create(user=self.user, name="CAS — N")

    def _run(self, cas):
        result = cas_mod.CasImportResult()
        cas_mod._import_nsdlcdsl(cas, result, self.account)
        return result

    def _eq(self, qty="10"):
        return Equity(name="Reliance", isin="INE002A01018", num_shares=Decimal(qty),
                      price=Decimal("2500"), value=Decimal(qty) * 2500)

    def test_nsdl_and_cdsl_period_formats(self):
        for to, expected in (("18-Oct-2026", date(2026, 10, 18)), ("17/10/2026", date(2026, 10, 17))):
            Transaction.objects.all().delete()
            self._run(_nsdl([_demat("IN30", "1", equities=[self._eq()])], to=to))
            self.assertEqual(Transaction.objects.get().date, expected)

    def test_missing_period_dates_positions_today(self):
        r = self._run(_nsdl([_demat("IN30", "1", equities=[self._eq()])], to=""))
        self.assertEqual(r.warnings, [])
        self.assertEqual(Transaction.objects.get().date, date.today())

    def test_same_isin_in_nsdl_and_cdsl_accounts_kept_apart(self):
        r = self._run(_nsdl([_demat("IN30", "1", equities=[self._eq()]),
                             _demat("12081600", "2", equities=[self._eq()])]))
        self.assertEqual(r.transactions_created, 2)
        self.assertEqual(Transaction.objects.count(), 2)

    def test_later_snapshot_updates_position_instead_of_doubling(self):
        self._run(_nsdl([_demat("IN30", "1", equities=[self._eq("10")])], to="18-Sep-2026"))
        r = self._run(_nsdl([_demat("IN30", "1", equities=[self._eq("12")])], to="18-Oct-2026"))
        self.assertEqual(r.transactions_created, 0)
        tx = Transaction.objects.get()
        self.assertEqual((tx.quantity, tx.date), (Decimal("12"), date(2026, 10, 18)))

    def test_bonds_zero_balances_and_regular_plan(self):
        r = self._run(_nsdl([_demat("IN30", "1",
            equities=[self._eq("0")],
            mfs=[MutualFund(name="ICICI Pru Bluechip - Regular Growth", isin="INF109K01BL4",
                            balance=Decimal("100"), nav=Decimal("90"), value=Decimal("9000"))],
            bonds=[Bond(name="7.26% GOI 2033", isin="IN0020230016", num_bonds=Decimal("5"),
                        value=Decimal("5100"), face_value=Decimal("1000"))])]))
        self.assertEqual(r.demat_positions, 2)  # zero-share equity skipped
        mf = Asset.objects.get(asset_type=Asset.AssetType.MUTUAL_FUND)
        self.assertFalse(mf.is_direct)
        self.assertEqual(mf.transactions.get().price, Decimal("90"))  # no avg_cost → NAV
        bond = Asset.objects.get(asset_type=Asset.AssetType.BOND).transactions.get()
        self.assertEqual((bond.quantity, bond.price, bond.amount),
                         (Decimal("5"), Decimal("1000"), Decimal("5100")))


class ImportCasPdfDispatchTests(TestCase):
    """import_cas_pdf with casparser.read_cas_pdf mocked: picks the branch by
    the model shape and names the account after the investor."""

    def setUp(self):
        self.user = User.objects.create_user("d", "d@x.in", "p")

    def test_cams_and_nsdl_shapes(self):
        from unittest import mock
        cams = _cams([Folio(folio="1", amc="A", schemes=[_scheme("F - Direct", "INF000000009", [
            _tx(TransactionType.PURCHASE, "2026-06-01", Decimal("1"), Decimal("10"), Decimal("10"))])])])
        nsdl = _nsdl([_demat("IN30", "1", equities=[Equity(
            name="Infy", isin="INE009A01021", num_shares=Decimal("2"), price=Decimal("1500"),
            value=Decimal("3000"))])])
        with mock.patch("casparser.read_cas_pdf", side_effect=[cams, nsdl]) as read:
            r1 = cas_mod.import_cas_pdf(self.user, "/tmp/x.pdf", None)
            r2 = cas_mod.import_cas_pdf(self.user, "/tmp/y.pdf", "PAN123")
        self.assertEqual(read.call_args_list[1].args, ("/tmp/y.pdf", "PAN123"))
        self.assertEqual((r1.folios, r1.transactions_created, r1.demat_positions), (1, 1, 0))
        self.assertEqual((r2.folios, r2.transactions_created, r2.demat_positions), (1, 1, 1))
        self.assertEqual(Account.objects.get(user=self.user).name, "CAS — Dinesh")
        self.assertEqual(Transaction.objects.filter(source="cas").count(), 2)
