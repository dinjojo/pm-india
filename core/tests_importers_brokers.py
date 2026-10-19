"""Broker / distributor holdings CSV import (core.importers.brokers), one
fixture per PROVIDERS entry plus ISIN matching, dates and malformed rows."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from core.importers.brokers import PROVIDERS, import_holdings_csv
from core.models import Asset, Transaction

ZERODHA_CONSOLE = b"""Symbol,ISIN,Sector,Quantity Available,Quantity Discrepant,Average Price,Previous Closing Price
RELIANCE,INE002A01018,Energy,10,0,"2,450.50",2900
INFY,INE009A01021,IT,5,0,1500,1800
"""
KITE = b"""Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L
TCS,3,3400.25,4000,12000,1799.25
"""
GROWW_MF = b"""Scheme Name,ISIN,Units,Average Cost
Parag Parikh Flexi Cap Fund Direct Growth,INF879O01027,"1,234.567",62.10
Some NFO Without ISIN,,100,10
"""
HDFC_SKY = b"""Instrument Name,ISIN,Qty,Avg Cost
HDFCBANK,INE040A01034,20,1600
"""
KOTAK = b"""Scrip,ISIN,Holding Qty,Avg Price
ITC,INE154A01025,100,410.5
"""


class BrokerImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("k", "k@x.in", "p")

    def test_every_provider_has_a_test_here(self):
        self.assertEqual({p.key for p in PROVIDERS}, {"zerodha", "groww", "hdfc", "kotak", "generic"})

    def test_zerodha_console(self):
        r = import_holdings_csv(self.user, ZERODHA_CONSOLE, "zerodha")
        self.assertEqual(r.errors, [])
        self.assertEqual((r.holdings_created, r.transactions_created), (2, 2))
        rel = Asset.objects.get(identifier="INE002A01018")
        self.assertEqual(rel.asset_type, Asset.AssetType.STOCK)
        tx = rel.transactions.get()
        self.assertEqual((tx.kind, tx.quantity, tx.price), ("buy", Decimal("10"), Decimal("2450.50")))
        self.assertEqual(tx.amount, Decimal("24505.00"))
        self.assertEqual(tx.account.name, "Zerodha Console import")

    def test_zerodha_kite_dotted_headers(self):
        r = import_holdings_csv(self.user, KITE, "zerodha")
        self.assertEqual(r.errors, [])
        tx = Transaction.objects.get()
        self.assertEqual((tx.asset.name, tx.quantity, tx.price), ("TCS", Decimal("3"), Decimal("3400.25")))

    def test_groww_mf_lands_on_the_cas_mutual_fund_asset(self):
        cas_asset = Asset.objects.create(asset_type=Asset.AssetType.MUTUAL_FUND,
                                         identifier="INF879O01027", isin="INF879O01027",
                                         name="PPFAS Flexi Cap - Direct")
        r = import_holdings_csv(self.user, GROWW_MF, "groww")
        self.assertEqual(r.errors, [])
        self.assertEqual(r.transactions_created, 2)
        self.assertEqual(cas_asset.transactions.get().quantity, Decimal("1234.567"))
        # No ISIN: falls back to a provider-scoped name key (equity default type)
        nfo = Asset.objects.get(identifier="groww:some nfo without isin")
        self.assertEqual(nfo.transactions.get().quantity, Decimal("100"))

    def test_hdfc_and_kotak(self):
        self.assertEqual(import_holdings_csv(self.user, HDFC_SKY, "hdfc").transactions_created, 1)
        self.assertEqual(import_holdings_csv(self.user, KOTAK, "kotak").transactions_created, 1)
        self.assertEqual(Asset.objects.get(identifier="INE154A01025").transactions.get().price,
                         Decimal("410.5"))
        self.assertEqual(Transaction.objects.get(asset__identifier="INE040A01034").account.name,
                         "HDFC (Sky / Securities / MF) import")

    def test_generic_auto_detects(self):
        r = import_holdings_csv(self.user, ZERODHA_CONSOLE, "generic")
        self.assertEqual(r.provider, "zerodha")
        self.assertEqual(r.transactions_created, 2)

    def test_date_column_is_used(self):
        csv = b"Symbol,ISIN,Quantity,Average Price,Date\nSBIN,INE062A01020,4,600,2026-03-31\n" \
              b"ITC,INE154A01025,2,400,15-Jan-2026\n"
        import_holdings_csv(self.user, csv, "zerodha")
        self.assertEqual(Transaction.objects.get(asset__identifier="INE062A01020").date, date(2026, 3, 31))
        self.assertEqual(Transaction.objects.get(asset__identifier="INE154A01025").date, date(2026, 1, 15))

    def test_undated_snapshot_reimport_on_a_later_day_does_not_double(self):
        import_holdings_csv(self.user, ZERODHA_CONSOLE, "zerodha")
        Transaction.objects.update(date=date.today() - timedelta(days=3))  # imported earlier
        again = import_holdings_csv(self.user, ZERODHA_CONSOLE, "zerodha")
        self.assertEqual(again.transactions_created, 0)
        self.assertIn("already imported", again.errors[0])
        self.assertEqual(Transaction.objects.count(), 2)

    def test_malformed_rows_are_row_errors(self):
        csv = (b"Symbol,ISIN,Quantity,Average Price,Date\n"
               b"GOOD,INE000000001,1,10,\n"
               b"BADQTY,INE000000002,ten,10,\n"
               b"NANQTY,INE000000003,NaN,10,\n"
               b"BADDATE,INE000000004,1,10,31/31/2026\n"
               b"ZERO,INE000000005,0,10,\n"
               b",INE000000006,5,10,\n")
        r = import_holdings_csv(self.user, csv, "zerodha")
        self.assertEqual(r.transactions_created, 1)
        self.assertEqual(r.skipped, 2)  # zero qty, missing name
        self.assertEqual(len(r.errors), 3)
        self.assertTrue(r.errors[0].startswith("Row 3:"))
        self.assertIn("NANQTY", r.errors[1])
        self.assertIn("31/31/2026", r.errors[2])

    def test_unusable_files_give_clean_errors(self):
        self.assertIn("Unknown provider", import_holdings_csv(self.user, KITE, "upstox").errors[0])
        self.assertIn("no header", import_holdings_csv(self.user, b"", "generic").errors[0])
        r = import_holdings_csv(self.user, b"Foo,Bar\n1,2\n", "generic")
        self.assertIn("name, quantity", r.errors[0])
        self.assertFalse(Transaction.objects.exists())
