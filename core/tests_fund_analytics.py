"""Fund-analytics suite: metrics on synthetic + real NAV series, catalog
browse/filter, returns calculator, and the analytics / calculator / browse pages."""
import os
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.test import TestCase

from core import fund_analytics as fa
from core import funds, nav_history

PARQUET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "amfi_nav_master.parquet")
HAS_PARQUET = os.path.exists(PARQUET)


def _bdays(n, start=date(2015, 1, 1)):
    return pd.bdate_range(start, periods=n)


def constant_growth(rate=0.10, n=252 * 6):
    idx = _bdays(n)
    days = (idx - idx[0]).days.values
    return pd.Series(100 * (1 + rate) ** (days / 365.25), index=idx)


def random_walk(mu, sigma, n=252 * 5, seed=1):
    rng = np.random.default_rng(seed)
    r = rng.normal(mu / 252, sigma / np.sqrt(252), n)
    return pd.Series(100 * np.cumprod(1 + r), index=_bdays(n))


class CoreMetricTests(TestCase):
    def test_constant_growth_cagr_and_zero_vol(self):
        m = fa.compute_metrics(constant_growth(0.10))
        self.assertTrue(m["enough"])
        self.assertAlmostEqual(m["returns"]["Since inception"], 10.0, delta=0.05)
        self.assertAlmostEqual(m["returns"]["1Y"], 10.0, delta=0.3)
        self.assertAlmostEqual(m["returns"]["3Y"], 10.0, delta=0.1)
        self.assertLess(m["risk"]["volatility"], 0.5)
        self.assertEqual(m["risk"]["max_drawdown"], 0.0)
        self.assertAlmostEqual(m["risk"]["positive_day_pct"], 100.0, delta=0.1)

    def test_known_drawdown(self):
        idx = _bdays(700)
        nav = np.concatenate([
            np.linspace(100, 200, 300),   # rally to peak at i=299
            np.linspace(200, 100, 101)[1:],  # crash to trough at i=399 (-50%)
            np.linspace(100, 220, 301)[1:],  # recover, surpass 200
        ])
        s = pd.Series(nav, index=idx)
        m = fa.compute_metrics(s)
        dd = m["risk"]
        self.assertAlmostEqual(dd["max_drawdown"], -50.0, places=6)
        self.assertEqual(dd["dd_peak"], idx[299].date().isoformat())
        self.assertEqual(dd["dd_trough"], idx[399].date().isoformat())
        rec = next(i for i in range(400, 700) if nav[i] >= 200)
        self.assertEqual(dd["dd_recovery"], idx[rec].date().isoformat())
        worst = m["drawdowns"][0]
        self.assertAlmostEqual(worst["depth"], -50.0, places=6)
        self.assertEqual(worst["trough"], idx[399].date().isoformat())
        self.assertLessEqual(len(m["drawdowns"]), 5)
        # Calmar = CAGR / |maxDD|
        self.assertAlmostEqual(dd["calmar"], m["returns"]["Since inception"] / 50.0, places=2)

    def test_sharpe_sortino_signs(self):
        up = fa.compute_metrics(random_walk(0.25, 0.15))["risk"]
        down = fa.compute_metrics(random_walk(-0.15, 0.15))["risk"]
        self.assertGreater(up["sharpe"], 0)
        self.assertGreater(up["sortino"], 0)
        self.assertGreater(up["omega"], 1)
        self.assertLess(down["sharpe"], 0)
        self.assertLess(down["sortino"], 0)
        self.assertLess(down["omega"], 1)
        self.assertAlmostEqual(up["volatility"], 15.0, delta=1.5)

    def test_higher_moments_and_best_worst(self):
        s = random_walk(0.12, 0.2, seed=7)
        r = s.pct_change().dropna()
        risk = fa.compute_metrics(s)["risk"]
        self.assertAlmostEqual(risk["best_day"], r.max() * 100, places=6)
        self.assertAlmostEqual(risk["worst_day"], r.min() * 100, places=6)
        self.assertAlmostEqual(risk["skewness"], float(r.skew()), places=4)
        self.assertAlmostEqual(risk["kurtosis"], float(r.kurt()), places=4)
        self.assertLess(abs(risk["kurtosis"]), 1)  # normal draws → ~0 excess

    def test_not_enough_history(self):
        s = constant_growth(0.10, n=150)
        m = fa.compute_metrics(s)
        self.assertFalse(m["enough"])
        self.assertIn("history", m["reason"].lower())

    def test_chart_decimated(self):
        m = fa.compute_metrics(random_walk(0.12, 0.15, n=252 * 10))
        ch = m["chart"]
        self.assertLessEqual(len(ch["dates"]), 420)
        self.assertGreater(len(ch["dates"]), 300)
        for k in ("nav", "ma50", "ma200", "drawdown"):
            self.assertEqual(len(ch[k]), len(ch["dates"]))
        self.assertTrue(all(v is None or v <= 0 for v in ch["drawdown"]))
        self.assertGreater(len(m["rolling_vol"]["dates"]), 10)


class MovingAverageSignalTests(TestCase):
    def test_v_shape_is_buy_with_golden_cross(self):
        idx = _bdays(700)
        nav = np.concatenate([np.linspace(200, 100, 350), np.linspace(100, 300, 350)])
        sig = fa.compute_metrics(pd.Series(nav, index=idx))["ma"]
        self.assertEqual(sig["signal"], "BUY")
        self.assertEqual(sig["last_cross_type"], "golden")
        self.assertGreater(sig["last_cross_date"], idx[350].date().isoformat())

    def test_inverted_v_is_sell_with_death_cross(self):
        idx = _bdays(700)
        nav = np.concatenate([np.linspace(100, 300, 350), np.linspace(300, 100, 350)])
        sig = fa.compute_metrics(pd.Series(nav, index=idx))["ma"]
        self.assertEqual(sig["signal"], "SELL")
        self.assertEqual(sig["last_cross_type"], "death")

    def test_mixed_is_hold(self):
        # Long uptrend (50 > 200), then a short sharp dip below the 200-DMA.
        idx = _bdays(600)
        nav = np.concatenate([np.linspace(100, 300, 560), np.linspace(300, 240, 40)])
        sig = fa.compute_metrics(pd.Series(nav, index=idx))["ma"]
        self.assertEqual(sig["signal"], "HOLD")


class MonteCarloTests(TestCase):
    def test_var_bounds(self):
        rng = np.random.default_rng(3)
        r = rng.normal(0.12 / 252, 0.15 / np.sqrt(252), 252 * 5)
        mc = fa.monte_carlo(r, n_paths=10_000, seed=11)
        self.assertEqual(mc["paths"], 10_000)
        # Normal approx: 1y return ~ N(12%, 15%) → 95% VaR ≈ 12%, 99% ≈ 23%.
        self.assertGreater(mc["var95_pct"], 3)
        self.assertLess(mc["var95_pct"], 25)
        self.assertGreaterEqual(mc["var99_pct"], mc["var95_pct"])
        self.assertGreaterEqual(mc["cvar95_pct"], mc["var95_pct"])
        self.assertGreaterEqual(mc["cvar99_pct"], mc["var99_pct"])
        self.assertEqual(sum(mc["hist_counts"]), 10_000)
        self.assertEqual(len(mc["hist_counts"]), len(mc["hist_labels"]))

    def test_scale_to_amount(self):
        mc = {"var95_pct": 10.0, "var99_pct": 20.0, "cvar95_pct": 15.0, "cvar99_pct": 25.0}
        out = fa.scale_var(mc, 200_000)
        self.assertEqual(out["var95_inr"], 20_000)
        self.assertEqual(out["cvar99_inr"], 50_000)


class CapmTests(TestCase):
    def test_beta_of_levered_fund(self):
        rng = np.random.default_rng(5)
        idx = _bdays(252 * 4)
        br = rng.normal(0.10 / 252, 0.15 / np.sqrt(252), len(idx))
        bench = pd.Series(100 * np.cumprod(1 + br), index=idx)
        fund = pd.Series(100 * np.cumprod(1 + 1.5 * br), index=idx)
        c = fa.capm(fund, bench, rf=0.06)
        self.assertTrue(c["ok"])
        self.assertAlmostEqual(c["beta"], 1.5, places=3)
        exp = 6.0 + c["beta"] * (c["bench_cagr"] - 6.0)
        self.assertAlmostEqual(c["expected"], exp, places=6)
        self.assertAlmostEqual(c["alpha"], c["fund_cagr"] - c["expected"], places=6)
        self.assertIn(c["verdict"], ("beating", "underperforming"))

    def test_short_overlap_hidden(self):
        idx = _bdays(100)
        s = pd.Series(np.linspace(100, 110, 100), index=idx)
        c = fa.capm(s, s, rf=0.06)
        self.assertFalse(c["ok"])
        self.assertTrue(c["reason"])

    def test_missing_benchmark(self):
        c = fa.capm(constant_growth(), None, rf=0.06)
        self.assertFalse(c["ok"])


class CalculatorMathTests(TestCase):
    def test_lumpsum(self):
        r = fa.lumpsum_fv(100_000, 10, 10, inflation=6)
        self.assertAlmostEqual(r["nominal"], 259_374.25, places=0)
        self.assertAlmostEqual(r["real"], 259_374.25 / 1.06 ** 10, places=0)
        self.assertEqual(r["invested"], 100_000)
        self.assertEqual(len(r["series"]), 11)

    def test_sip(self):
        r = fa.sip_fv(10_000, 12, 10, inflation=6)
        # Annuity-due, monthly compounding at 12%/12: ₹23,23,391.
        self.assertAlmostEqual(r["nominal"], 2_323_391, delta=1)
        self.assertEqual(r["invested"], 1_200_000)
        self.assertAlmostEqual(r["gain"], r["nominal"] - 1_200_000, places=6)
        self.assertAlmostEqual(r["real"], r["nominal"] / 1.06 ** 10, places=0)

    def test_zero_rate(self):
        self.assertAlmostEqual(fa.sip_fv(1000, 0, 2, inflation=0)["nominal"], 24_000)


class CatalogParseTests(TestCase):
    def test_parse_direct_growth_flexi(self):
        p = funds.parse_scheme_name("Parag Parikh Flexi Cap Fund - Direct Plan - Growth")
        self.assertEqual(p["fund_house"], "Parag Parikh")
        self.assertEqual(p["plan"], "Direct")
        self.assertEqual(p["payout"], "Growth")
        self.assertEqual(p["group"], "Equity")
        self.assertEqual(p["category"], "Flexi Cap")

    def test_parse_regular_payout_liquid(self):
        p = funds.parse_scheme_name("HDFC Liquid Fund - Regular Plan - IDCW Daily Payout")
        self.assertEqual(p["fund_house"], "HDFC")
        self.assertEqual(p["plan"], "Regular")
        self.assertEqual(p["payout"], "Payout")
        self.assertEqual((p["group"], p["category"]), ("Debt", "Liquid"))

    def test_parse_misc(self):
        self.assertEqual(funds.parse_scheme_name("Nippon India ETF Gold BeES")["group"], "Gold")
        self.assertEqual(funds.parse_scheme_name(
            "DSP World Mining Overseas Equity Omni FoF - Regular Plan - Growth")["group"], "FoF")
        p = funds.parse_scheme_name("Kotak ELSS Tax Saver Fund-Payout of Income Distribution cum capital withdrawal option")
        self.assertEqual((p["group"], p["category"], p["payout"]), ("Equity", "ELSS", "Payout"))
        self.assertEqual(funds.parse_scheme_name("ICICI Prudential Gilt Fund - Dividend")["payout"], "IDCW")
        self.assertEqual(funds.parse_scheme_name("Aditya Birla Sun Life MNC Fund - DIRECT - IDCW")["fund_house"],
                         "Aditya Birla Sun Life")

    def test_filter(self):
        rows = [
            {"code": 1, "name": "A Flexi Cap Fund - Direct - Growth", "plan": "Direct", "payout": "Growth", "group": "Equity", "active": True},
            {"code": 2, "name": "A Liquid Fund - Regular - Growth", "plan": "Regular", "payout": "Growth", "group": "Debt", "active": True},
            {"code": 3, "name": "B Flexi Cap Fund - Regular - IDCW", "plan": "Regular", "payout": "IDCW", "group": "Equity", "active": False},
        ]
        f = funds.filter_catalog
        self.assertEqual([r["code"] for r in f(rows, q="flexi")], [1])  # inactive hidden by default
        self.assertEqual([r["code"] for r in f(rows, q="flexi", include_inactive=True)], [1, 3])
        self.assertEqual([r["code"] for r in f(rows, plan="regular")], [2])
        self.assertEqual([r["code"] for r in f(rows, group="equity", include_inactive=True)], [1, 3])
        self.assertEqual([r["code"] for r in f(rows, payout="idcw", include_inactive=True)], [3])
        self.assertEqual([r["code"] for r in f(rows, q="1")], [1])  # scheme-code search


class _TempCacheMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp()
        p = mock.patch.object(funds, "CACHE_DIR", self._tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self._tmp, True)


@mock.patch.object(nav_history, "NAV_PARQUET", PARQUET)
class RealSchemeTests(_TempCacheMixin, TestCase):
    """PPFCF Direct Growth (122639) vs hand-rolled numpy on the same Parquet series."""

    def setUp(self):
        super().setUp()
        if not HAS_PARQUET:
            self.skipTest("local NAV Parquet absent")

    def test_metrics_match_numpy(self):
        rows = nav_history.local_nav_history(122639)
        dates = np.array([np.datetime64(d) for d, _ in rows])
        nav = np.array([n for _, n in rows])
        days = (dates[-1] - dates[0]).astype(int)
        cagr = ((nav[-1] / nav[0]) ** (365.25 / days) - 1) * 100
        r = nav[1:] / nav[:-1] - 1
        vol = r.std(ddof=1) * np.sqrt(252) * 100
        mdd = (nav / np.maximum.accumulate(nav) - 1).min() * 100
        rf_d = 1.06 ** (1 / 252) - 1
        sharpe = (r - rf_d).mean() * 252 / (r.std(ddof=1) * np.sqrt(252))

        m = fa.scheme_analytics(122639)
        self.assertTrue(m["enough"])
        self.assertAlmostEqual(m["returns"]["Since inception"], cagr, places=6)
        self.assertAlmostEqual(m["risk"]["volatility"], vol, places=6)
        self.assertAlmostEqual(m["risk"]["max_drawdown"], mdd, places=6)
        self.assertAlmostEqual(m["risk"]["sharpe"], sharpe, places=6)
        self.assertIn("Parag Parikh", m["name"])
        self.assertIn(m["ma"]["signal"], ("BUY", "HOLD", "SELL"))
        self.assertEqual(m["mc"]["paths"], 10_000)

    def test_cached(self):
        first = fa.scheme_analytics(122639)
        with mock.patch.object(nav_history, "local_nav_history", side_effect=AssertionError("cache miss")):
            again = fa.scheme_analytics(122639)
        self.assertEqual(first["risk"], again["risk"])


@mock.patch.object(nav_history, "NAV_PARQUET", PARQUET)
class PageTests(_TempCacheMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("fa_user", password="x")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_calculator_sip(self):
        r = self.client.get("/funds/calculator/?mode=sip&amount=10000&rate=12&years=10&inflation=6")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "₹23.23 L")
        self.assertContains(r, "₹12.00 L")  # invested

    def test_calculator_lumpsum_default_and_prefill(self):
        r = self.client.get("/funds/calculator/?rate=14.5")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'value="14.5"')
        self.assertContains(r, "Lumpsum")

    def test_calculator_bad_input_does_not_crash(self):
        r = self.client.get("/funds/calculator/?mode=sip&amount=abc&rate=&years=-3")
        self.assertEqual(r.status_code, 200)

    def test_analytics_short_history_state(self):
        short = constant_growth(0.1, n=120)
        with mock.patch.object(fa, "load_series", return_value=(short, {"scheme_name": "Tiny New Fund"})):
            r = self.client.get("/funds/999001/analytics/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Not enough history")
        self.assertContains(r, "Tiny New Fund")

    def test_analytics_unknown_scheme(self):
        with mock.patch.object(fa, "load_series", return_value=(None, {})):
            r = self.client.get("/funds/999002/analytics/")
        self.assertEqual(r.status_code, 404)

    def test_analytics_real_page(self):
        if not HAS_PARQUET:
            self.skipTest("local NAV Parquet absent")
        r = self.client.get("/funds/122639/analytics/?amount=200000")
        self.assertEqual(r.status_code, 200)
        for s in ("Sharpe", "Sortino", "Omega", "Calmar", "50-DMA", "statistical estimate, not a forecast",
                  "6.0%", "/funds/calculator/?", "Parag Parikh"):
            self.assertContains(r, s)
        self.assertContains(r, "₹2.00 L")  # MC amount echoed in shorthand

    def test_browse_filters(self):
        if not HAS_PARQUET:
            self.skipTest("local NAV Parquet absent")
        r = self.client.get("/funds/?q=parag+parikh+flexi&plan=direct&payout=growth")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "/funds/122639/analytics/")
        self.assertContains(r, "/funds/122639/")
        r2 = self.client.get("/funds/?group=debt&page=2")
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "page=3")

    def test_fund_detail_links_to_analytics(self):
        if not HAS_PARQUET:
            self.skipTest("local NAV Parquet absent")
        r = self.client.get("/funds/122639/")
        self.assertContains(r, "/funds/122639/analytics/")
        self.assertContains(r, "/funds/calculator/")
