"""Grouping/subtotal logic for the Holdings and Tax pages, plus template rendering."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.shortcuts import render as django_render
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import report_grouping as rg
from core.models import Account, Asset, PricePoint, Transaction
from core.portfolio import HoldingView, PortfolioView, refresh_holdings
from core.tax.gains import FYTaxSummary, Gain, UnrealizedLot

D = Decimal


def _hv(pk, name, t, inv, cur, priority=False, manual=False):
    return HoldingView(holding_pk=pk, asset_pk=pk, name=name, asset_type=t, account="Acc",
                       owner="", quantity=D(1), invested=D(inv), current=D(cur),
                       manual_value=manual, priority=priority)


def _gain(name, t, cost, proceeds, days, sold=date(2025, 6, 1)):
    return Gain(asset_name=name, asset_type=t, buy_date=sold - timedelta(days=days), sell_date=sold,
                quantity=D(1), cost=D(cost), proceeds=D(proceeds), holding_period_days=days)


def _ulot(name, t, cost, current, days_ago):
    return UnrealizedLot(asset_name=name, asset_type=t, buy_date=date.today() - timedelta(days=days_ago),
                         quantity=D(1), cost=D(cost), current=D(current))


class HoldingsReportTests(SimpleTestCase):
    def setUp(self):
        self.pv = PortfolioView(holdings=[
            _hv(1, "Index fund", "mf", 100, 150, priority=True),
            _hv(2, "Debt fund", "mf", 200, 190),
            _hv(3, "INFY", "stock", 300, 360),
            _hv(4, "PPF", "ppf", 400, 400, manual=True),
        ])
        self.pv.total_invested = D(1000)
        self.pv.total_current = D(1100)

    def test_class_sections_have_subtotals_and_counts(self):
        rep = rg.holdings_report(self.pv, self.pv.grouped_holdings)
        mf = next(s for s in rep["sections"] if s["type"] == "mf")
        self.assertEqual((mf["invested"], mf["current"], mf["gain"]), (D(300), D(340), D(40)))
        self.assertEqual((mf["count"], mf["starred_count"]), (2, 1))
        self.assertEqual(mf["holdings"][0].name, "Index fund")  # starred pinned first
        ppf = next(s for s in rep["sections"] if s["type"] == "ppf")
        self.assertEqual(ppf["manual_count"], 1)
        self.assertEqual(rep["totals"]["gain"], D(100))
        self.assertEqual(rep["totals"]["count"], 4)
        self.assertEqual([s["slug"] for s in rep["sections"]],
                         [f"grp-{i}" for i in range(len(rep["sections"]))])

    def test_works_for_alternate_groupings(self):
        rep = rg.holdings_report(self.pv, self.pv.grouped_by("liquidity"))
        self.assertEqual(sum(s["count"] for s in rep["sections"]), 4)
        self.assertEqual(rep["totals"]["current"], D(1100))


class TaxReportTests(SimpleTestCase):
    def test_fy_overview_rows_and_totals(self):
        s1 = FYTaxSummary(fy="2024-25", lots=[_gain("A", "mf", 100, 200, 400)])
        s2 = FYTaxSummary(fy="2025-26", lots=[_gain("B", "stock", 100, 150, 30)])
        ov = rg.fy_overview([s1, s2], "2025-26")
        self.assertEqual([r["selected"] for r in ov["rows"]], [False, True])
        self.assertEqual(ov["totals"]["ltcg"], D(100))
        self.assertEqual(ov["totals"]["stcg"], D(50))
        self.assertEqual(ov["totals"]["lots"], 2)

    def test_realised_grouped_by_category_with_st_lt_split_and_priority_first(self):
        lots = [
            _gain("INFY", "stock", 100, 80, 30),
            _gain("Index fund", "mf", 100, 300, 500),
            _gain("Small cap", "mf", 100, 120, 100),
            _gain("SGB 2017", "sgb", 100, 110, 900),
            _gain("Gold ETF", "gold", 100, 130, 900),
        ]
        rep = rg.realised_by_category(lots, starred={("mf", "Small cap")})
        self.assertEqual([g["label"] for g in rep["groups"]],
                         ["Mutual funds", "Stocks & ETFs", "Gold & SGBs"])
        mf = rep["groups"][0]
        self.assertEqual((mf["gain"], mf["st_gain"], mf["lt_gain"]), (D(220), D(20), D(200)))
        self.assertEqual(mf["rows"][0]["lot"].asset_name, "Small cap")
        self.assertTrue(mf["rows"][0]["priority"])
        self.assertEqual(rep["groups"][2]["count"], 2)  # SGB + gold merged
        self.assertEqual(rep["totals"]["gain"], D(220 - 20 + 10 + 30))

    def test_unrealised_subtotals_cover_hidden_zero_lots(self):
        lots = [
            _ulot("A", "mf", 100, 100, 10),      # zero gain -> hidden row
            _ulot("B", "mf", 100, 80, 10),       # ST loss
            _ulot("C", "mf", 100, 200, 800),     # LT gain, starred
            _ulot("D", "stock", 50, 60, 10),
        ]
        rep = rg.unrealised_by_category(lots, starred={("mf", "C")}, hide_zero=True)
        mf = rep["groups"][0]
        self.assertEqual((mf["count"], mf["hidden"], len(mf["rows"])), (3, 1, 2))
        self.assertEqual(mf["cost"], D(300))
        self.assertEqual((mf["st_gain"], mf["lt_gain"], mf["losses"]), (D(-20), D(100), D(-20)))
        self.assertEqual(mf["rows"][0]["lot"].asset_name, "C")
        self.assertEqual(rep["starred"]["count"], 1)
        self.assertEqual(rep["totals"]["hidden"], 1)
        self.assertEqual(rep["totals"]["gain"], D(90))
        shown = rg.unrealised_by_category(lots, starred=set(), hide_zero=False)
        self.assertEqual(len(shown["groups"][0]["rows"]), 3)

    def test_tax_report_handles_missing_summary(self):
        rep = rg.tax_report([], None, [], starred=set())
        self.assertEqual(rep["realised"]["groups"], [])
        self.assertEqual(rep["fy"]["rows"], [])


def _render_with_reports(request, template, ctx=None, *a, **kw):
    """Mimics the one-line view wiring: add hreport / treport to the context."""
    ctx = dict(ctx or {})
    if template == "holdings.html":
        ctx["hreport"] = rg.holdings_report(ctx["pv"], ctx["groups"])
    elif template == "tax.html":
        ctx["treport"] = rg.tax_report(ctx["summaries"], ctx["summary"], ctx["unrealized"],
                                       ctx["hide_zero"])
    return django_render(request, template, ctx, *a, **kw)


class PageRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("grouper", password="x")
        acc = Account.objects.create(user=cls.user, name="Zerodha")
        mf = Asset.objects.create(asset_type="mf", name="Nifty Index Fund", identifier="1001", priority=True)
        st = Asset.objects.create(asset_type="stock", name="INFY", identifier="INFY")
        today = date.today()
        for asset, price in ((mf, 100), (st, 1500)):
            Transaction.objects.create(asset=asset, account=acc, kind="buy", date=today - timedelta(days=600),
                                       quantity=10, price=price, amount=10 * price)
        Transaction.objects.create(asset=st, account=acc, kind="sell", date=today - timedelta(days=5),
                                   quantity=4, price=1800, amount=7200)
        PricePoint.objects.create(asset_type="mf", identifier="1001", price=120, as_of=today)
        PricePoint.objects.create(asset_type="stock", identifier="INFY", price=1400, as_of=today)
        refresh_holdings()

    def setUp(self):
        self.client.force_login(self.user)

    def test_pages_render_without_reports_wired(self):
        r = self.client.get(reverse("holdings"))
        self.assertContains(r, "subtotal")
        self.assertContains(r, "Update value")
        self.assertEqual(self.client.get(reverse("tax")).status_code, 200)

    def test_pages_render_grouped_with_reports_wired(self):
        with mock.patch("core.views.render", _render_with_reports):
            h = self.client.get(reverse("holdings"))
            t = self.client.get(reverse("tax"))
        self.assertContains(h, "Mutual Fund subtotal")
        self.assertContains(h, "★ Starred")
        self.assertContains(h, 'hx-post="/', html=False)  # toggle_priority still wired
        self.assertContains(t, "Stocks &amp; ETFs subtotal")
        self.assertContains(t, "Mutual funds subtotal")
        self.assertContains(t, "Harvestable losses")
