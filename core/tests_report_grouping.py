"""Grouping/subtotal logic for the Holdings and Tax pages, plus template rendering."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.shortcuts import render as django_render
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import report_grouping as rg
from core.models import Account, Asset, PricePoint, Transaction, ValueSnapshot
from core.portfolio import HoldingView, PortfolioView, refresh_holdings
from core.tax.gains import FYTaxSummary, Gain, UnrealizedLot, realized_gains, unrealized_lots

D = Decimal


def _hv(pk, name, t, inv, cur, priority=False, manual=False):
    return HoldingView(holding_pk=pk, asset_pk=pk, name=name, asset_type=t, account="Acc",
                       owner="", quantity=D(1), invested=D(inv), current=D(cur),
                       manual_value=manual, priority=priority)


def _gain(name, t, cost, proceeds, days, sold=date(2025, 6, 1), pk=None, user_id=None):
    return Gain(asset_name=name, asset_type=t, buy_date=sold - timedelta(days=days), sell_date=sold,
                quantity=D(1), cost=D(cost), proceeds=D(proceeds), holding_period_days=days,
                asset_pk=pk, user_id=user_id)


def _ulot(name, t, cost, current, days_ago, pk=None, user_ids=()):
    return UnrealizedLot(asset_name=name, asset_type=t, buy_date=date.today() - timedelta(days=days_ago),
                         quantity=D(1), cost=D(cost), current=D(current),
                         asset_pk=pk, user_ids=frozenset(user_ids))


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

    def test_decision_cues_rows_contributors_concentration(self):
        rep = rg.holdings_report(self.pv, self.pv.grouped_holdings)
        mf = next(s for s in rep["sections"] if s["type"] == "mf")
        self.assertEqual(mf["top_gainer"].name, "Index fund")
        self.assertEqual(mf["top_loser"].name, "Debt fund")
        self.assertEqual(mf["largest"]["name"], "Debt fund")
        row = next(r for r in mf["rows"] if r["h"].name == "Debt fund")
        self.assertAlmostEqual(row["weight"], 190 / 1100 * 100)
        self.assertAlmostEqual(row["share"], 190 / 340 * 100)
        stock = next(s for s in rep["sections"] if s["type"] == "stock")
        self.assertIsNone(stock["top_gainer"])  # single holding: no contributor callout
        c = rep["concentration"]
        self.assertEqual((c["n"], c["positions"]), (4, 4))
        self.assertAlmostEqual(c["weight"], 100.0)
        self.assertEqual(c["largest"]["name"], "PPF")
        self.assertIsNone(rep["spark"])  # no snapshots passed

    def test_concentration_merges_same_asset_across_accounts(self):
        a = _hv(1, "Index fund", "mf", 100, 150)
        b = _hv(2, "Index fund", "mf", 100, 150)
        b.asset_pk = 1
        c = rg.concentration([a, b, _hv(3, "INFY", "stock", 100, 100)], D(400))
        self.assertEqual(c["positions"], 2)
        self.assertEqual((c["top"][0]["current"], c["top"][0]["accounts"]), (D(300), 2))

    def test_sparklines_from_snapshots_class_grouping_only(self):
        snaps = [ValueSnapshot(date=date(2026, 1, i), invested=D(1000), current=D(1000 + i * 10),
                               breakdown={"mf": 300 + i, "stock": 360 - i}, estimated=(i == 1))
                 for i in range(1, 6)]
        rep = rg.holdings_report(self.pv, self.pv.grouped_holdings, snaps=snaps)
        mf = next(s for s in rep["sections"] if s["type"] == "mf")
        self.assertEqual((mf["spark"]["first"], mf["spark"]["last"]), (301.0, 305.0))
        self.assertTrue(mf["spark"]["estimated"])
        self.assertEqual(len(mf["spark"]["points"].split()), 5)
        ppf = next(s for s in rep["sections"] if s["type"] == "ppf")
        self.assertIsNone(ppf["spark"])  # all zero in breakdown -> nothing drawn
        self.assertEqual(rep["spark_range"], (date(2026, 1, 1), date(2026, 1, 5)))
        by_owner = rg.holdings_report(self.pv, self.pv.grouped_by("owner"), snaps=snaps)
        self.assertIsNone(by_owner["sections"][0]["spark"])
        self.assertIsNotNone(by_owner["spark"])

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
            _gain("Small cap", "mf", 100, 120, 100, pk=7),
            _gain("SGB 2017", "sgb", 100, 110, 900),
            _gain("Gold ETF", "gold", 100, 130, 900),
        ]
        rep = rg.realised_by_category(lots, starred={7})
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
            _ulot("C", "mf", 100, 200, 800, pk=3),  # LT gain, starred
            _ulot("D", "stock", 50, 60, 10),
        ]
        rep = rg.unrealised_by_category(lots, starred={3}, hide_zero=True)
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

    def test_starring_matches_asset_pk_and_owner_not_name(self):
        lots = [_ulot("Same name", "mf", 100, 90, 10, pk=1, user_ids={10}),
                _ulot("Same name", "mf", 100, 80, 10, pk=2, user_ids={10}),
                _ulot("No identity", "mf", 100, 80, 10)]
        rep = rg.unrealised_by_category(lots, starred={1}, hide_zero=False)
        self.assertEqual(rep["starred"]["count"], 1)
        self.assertEqual([r["lot"].asset_pk for r in rep["groups"][0]["rows"] if r["priority"]], [1])
        # per-user: another user's view does not star user 10's lot
        self.assertEqual(rg.unrealised_by_category(lots, {1}, False, user_id=11)["starred"]["count"], 0)
        self.assertTrue(rg.is_starred(_gain("X", "mf", 1, 2, 3, pk=1, user_id=10), {1}, user_id=10))
        self.assertFalse(rg.is_starred(_gain("X", "mf", 1, 2, 3, pk=1, user_id=10), {1}, user_id=11))

    def test_harvest_now_loss_setoff_and_tax_free_booking(self):
        fy = rg._fy_of(date.today())
        cur = FYTaxSummary(fy=fy, lots=[_gain("A", "stock", 100000, 300000, 30, sold=date.today()),   # STCG 2L
                                        _gain("B", "mf", 100000, 150000, 400, sold=date.today())])   # LTCG 50k
        lots = [_ulot("L1", "mf", 100000, 40000, 30),       # ST loss -60k
                _ulot("L2", "stock", 50000, 30000, 500),    # LT loss -20k
                _ulot("G1", "mf", 100000, 200000, 800),     # LT gain 1L (exempt pool)
                _ulot("G2", "gold", 10000, 20000, 800),     # LT gain, not in 112A pool
                _ulot("C1", "crypto", 10000, 5000, 10)]     # crypto loss: no set-off
        h = rg.harvest_now([cur], lots)
        self.assertEqual(h["fy"], fy)
        self.assertEqual((h["st_loss"], h["lt_loss"], h["crypto_loss"]), (D(-60000), D(-20000), D(-5000)))
        self.assertEqual(h["tax_now"], D(200000) * D("0.20"))            # LTCG 50k inside exemption
        self.assertEqual(h["tax_after"], D(140000) * D("0.20"))          # STCG 2L-60k; LTCG 50k-20k
        self.assertEqual(h["saving"], D(12000))
        self.assertEqual((h["used"], h["headroom"]), (D(50000), D(75000)))
        self.assertEqual(h["used_after"], D(30000))
        self.assertEqual(h["lt_gain_pool"], D(100000))
        self.assertEqual((h["book_free"], h["book_free_after"]), (D(75000), D(95000)))
        self.assertEqual(h["carry_forward"], 0)

    def test_harvest_now_short_term_loss_offsets_ltcg_and_carries_forward(self):
        h = rg.harvest_now([], [_ulot("L", "stock", 100, 40, 10)], today=date(2026, 5, 1))
        self.assertEqual(h["fy"], "2026-27")
        self.assertEqual((h["tax_now"], h["tax_after"], h["carry_forward"]), (0, 0, D(60)))

    def test_tax_rules_attached_per_category(self):
        rep = rg.realised_by_category([_gain("BTC", "crypto", 1, 2, 3)], set())
        self.assertIn("30%", rep["groups"][0]["rule"])

    def test_tax_report_handles_missing_summary(self):
        rep = rg.tax_report([], None, [], starred=set())
        self.assertEqual(rep["realised"]["groups"], [])
        self.assertEqual(rep["fy"]["rows"], [])


def _render_with_reports(request, template, ctx=None, *a, **kw):
    """Mimics the one-line view wiring: add hreport / treport to the context."""
    ctx = dict(ctx or {})
    if template == "holdings.html":
        ctx["hreport"] = rg.holdings_report(ctx["pv"], ctx["groups"],
                                            snaps=request.user.value_snapshots.order_by("date"))
    elif template == "tax.html":
        ctx["treport"] = rg.tax_report(ctx["summaries"], ctx["summary"], ctx["unrealized"],
                                       ctx["hide_zero"], user=request.user)
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
        for i, cur in enumerate((9000, 9500, 10400)):
            ValueSnapshot.objects.create(user=cls.user, date=today - timedelta(days=2 - i), invested=D(16000),
                                         current=D(cur), breakdown={"mf": cur / 2, "stock": cur / 2})

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
        self.assertContains(h, "Concentration")
        self.assertContains(h, "Portfolio value trend")  # snapshot sparkline once snaps are wired
        self.assertContains(t, "What if I harvest now?")
        self.assertContains(t, "If you sold today")
        self.assertContains(t, "Flat 30%", count=0)  # no crypto lots -> no crypto rule line


class StarIdentityDBTests(TestCase):
    """Starring joins on Asset pk + owning user, end to end through gains.py."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.me = User.objects.create_user("me", password="x")
        cls.other = User.objects.create_user("other", password="x")
        mine = Account.objects.create(user=cls.me, name="Mine")
        theirs = Account.objects.create(user=cls.other, name="Theirs")
        # Two distinct assets sharing type + name; only one is starred.
        cls.starred = Asset.objects.create(asset_type="mf", name="Flexi Cap", identifier="A1",
                                           isin="INF000000001", priority=True)
        cls.twin = Asset.objects.create(asset_type="mf", name="Flexi Cap", identifier="A2")
        cls.foreign = Asset.objects.create(asset_type="stock", name="TCS", identifier="TCS", priority=True)
        d = date.today() - timedelta(days=500)
        for asset, acc in ((cls.starred, mine), (cls.twin, mine), (cls.foreign, theirs)):
            Transaction.objects.create(asset=asset, account=acc, kind="buy", date=d, quantity=D(10), price=D(10))
            Transaction.objects.create(asset=asset, account=acc, kind="sell", date=date.today() - timedelta(days=3),
                                       quantity=D(4), price=D(12))

    def test_lots_carry_identity(self):
        lots = {l.asset_pk: l for l in unrealized_lots()}
        self.assertEqual(lots[self.starred.pk].isin, "INF000000001")
        self.assertEqual(lots[self.starred.pk].user_ids, frozenset({self.me.pk}))
        gains = [g for s in realized_gains() for g in s.lots]
        g = next(g for g in gains if g.asset_pk == self.starred.pk)
        self.assertEqual(g.user_id, self.me.pk)

    def test_priority_keys_per_user(self):
        self.assertEqual(rg.priority_keys(), frozenset({self.starred.pk, self.foreign.pk}))
        self.assertEqual(rg.priority_keys(self.me), frozenset({self.starred.pk}))

    def test_starred_exactly_once_and_only_for_owner(self):
        summaries = realized_gains()
        summary = summaries[-1]
        rep = rg.tax_report(summaries, summary, unrealized_lots(), hide_zero=False, user=self.me)
        u = rep["unrealised"]
        self.assertEqual(u["starred"]["count"], 1)  # twin "Flexi Cap" and other user's TCS not starred
        starred_rows = [r["lot"] for g in u["groups"] for r in g["rows"] if r["priority"]]
        self.assertEqual([l.asset_pk for l in starred_rows], [self.starred.pk])
        realised = [r["lot"].asset_pk for g in rep["realised"]["groups"] for r in g["rows"] if r["priority"]]
        self.assertEqual(realised, [self.starred.pk])
