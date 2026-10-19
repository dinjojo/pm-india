"""Portfolio tools: MPT optimisation, CAPM, return-overlap, curated baskets."""
import os
from decimal import Decimal
from unittest import mock

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import portfolio_tools as pt
from core.models import Account, Asset, Holding

PARQUET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "amfi_nav_master.parquet")


def _prices_from_returns(rets: dict, start="2020-01-01") -> pd.DataFrame:
    """Daily (business-day) NAV frame from {code: array of daily simple returns}."""
    n = len(next(iter(rets.values())))
    idx = pd.bdate_range(start, periods=n + 1)
    return pd.DataFrame({c: 100 * np.cumprod(np.r_[1.0, 1 + np.asarray(r)]) for c, r in rets.items()},
                        index=idx)


class CsvParsingTests(SimpleTestCase):
    def test_header_and_extra_columns(self):
        rows, errors = pt.parse_holdings_csv("scheme_code,amount,note\n122639,50000,x\n118989, 25000.5 ,y\n")
        self.assertEqual(rows, [(122639, 50000.0), (118989, 25000.5)])
        self.assertEqual(errors, [])

    def test_positional_columns_without_named_header(self):
        rows, errors = pt.parse_holdings_csv("code,value\n122639,1000\n")
        self.assertEqual(rows, [(122639, 1000.0)])

    def test_bad_rows_reported_inline(self):
        rows, errors = pt.parse_holdings_csv("scheme_code,amount\nabc,100\n122639,-5\n120197,\n118989,200\n")
        self.assertEqual(rows, [(118989, 200.0)])
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("line 2" in e for e in errors))

    def test_duplicate_codes_are_summed(self):
        rows, _ = pt.parse_holdings_csv("scheme_code,amount\n122639,100\n122639,50\n")
        self.assertEqual(rows, [(122639, 150.0)])

    def test_empty_upload(self):
        rows, errors = pt.parse_holdings_csv("")
        self.assertEqual(rows, [])
        self.assertTrue(errors)


class AlignmentTests(SimpleTestCase):
    def test_short_history_dropped_with_reason_and_dates_intersected(self):
        long_a = [(d.date().isoformat(), 10 + i * 0.01) for i, d in enumerate(pd.bdate_range("2020-01-01", periods=800))]
        long_b = [(d, n * 2) for d, n in long_a[100:]]
        short = [(d, n) for d, n in long_a[-100:]]
        hist = {1: long_a, 2: long_b, 3: short, 4: []}
        prices, dropped = pt.load_price_matrix([1, 2, 3, 4], history_fn=lambda c: hist[c])
        self.assertEqual(list(prices.columns), [1, 2])
        self.assertEqual(len(prices), 700)  # intersection of 1 and 2
        reasons = {d["code"]: d["reason"] for d in dropped}
        self.assertIn("history", reasons[3])
        self.assertIn("no NAV", reasons[4])


class OptimisationTests(SimpleTestCase):
    def test_min_variance_two_asset_matches_analytic(self):
        s1, s2, rho = 0.20, 0.10, 0.3
        cov = np.array([[s1 ** 2, rho * s1 * s2], [rho * s1 * s2, s2 ** 2]])
        w = pt.min_variance_weights(cov)
        w1 = (s2 ** 2 - rho * s1 * s2) / (s1 ** 2 + s2 ** 2 - 2 * rho * s1 * s2)
        self.assertAlmostEqual(w[0], w1, places=4)
        self.assertAlmostEqual(w.sum(), 1.0, places=9)

    def test_min_variance_long_only_clips_negative_weight(self):
        # rho high + very different vols → unconstrained solution shorts asset 0
        s1, s2, rho = 0.30, 0.10, 0.9
        cov = np.array([[s1 ** 2, rho * s1 * s2], [rho * s1 * s2, s2 ** 2]])
        w = pt.min_variance_weights(cov)
        self.assertGreaterEqual(w.min(), 0)
        self.assertAlmostEqual(w[1], 1.0, places=4)

    def test_max_sharpe_two_asset_matches_tangency(self):
        mu = np.array([0.14, 0.10])
        s1, s2, rho = 0.20, 0.12, 0.2
        cov = np.array([[s1 ** 2, rho * s1 * s2], [rho * s1 * s2, s2 ** 2]])
        w = pt.max_sharpe_weights(mu, cov, rf=0.06)
        raw = np.linalg.solve(cov, mu - 0.06)
        tangency = raw / raw.sum()
        np.testing.assert_allclose(w, tangency, atol=2e-3)

    def test_efficient_frontier_shape(self):
        mu = np.array([0.08, 0.12, 0.16])
        vols = np.array([0.05, 0.15, 0.25])
        cov = np.diag(vols ** 2)
        pts = pt.efficient_frontier(mu, cov, n_points=30)
        self.assertGreaterEqual(len(pts), 20)
        rets = [p["ret"] for p in pts]
        self.assertEqual(rets, sorted(rets))
        self.assertAlmostEqual(rets[-1], 0.16, places=3)
        for p in pts:
            self.assertAlmostEqual(sum(p["weights"]), 1.0, places=6)


class CapmTests(SimpleTestCase):
    def test_beta_of_half_benchmark_is_half(self):
        rng = np.random.default_rng(1)
        bench = pd.Series(rng.normal(0.0005, 0.01, 1000))
        self.assertAlmostEqual(pt.estimate_beta(bench * 0.5, bench), 0.5, places=6)

    def test_capm_verdict(self):
        res = pt.capm_evaluation(fund_cagr=0.15, beta=1.0, market_cagr=0.12, rf=0.06)
        self.assertAlmostEqual(res["expected"], 0.12)
        self.assertAlmostEqual(res["alpha"], 0.03)
        self.assertIn("beating", res["verdict"])
        behind = pt.capm_evaluation(fund_cagr=0.08, beta=1.2, market_cagr=0.12, rf=0.06)
        self.assertIn("behind", behind["verdict"])


class OverlapTests(SimpleTestCase):
    def test_perfectly_correlated_series_have_r2_one(self):
        rng = np.random.default_rng(7)
        r = rng.normal(0.0004, 0.01, 600)
        prices = _prices_from_returns({1: r, 2: 2 * r, 3: rng.normal(0.0004, 0.01, 600)})
        ov = pt.return_overlap(prices)
        i, j = ov["codes"].index(1), ov["codes"].index(2)
        self.assertAlmostEqual(ov["r2"][i][j], 1.0, places=2)
        self.assertIn((1, 2), [(p["a"], p["b"]) for p in ov["redundant"]])
        k = ov["codes"].index(3)
        self.assertLess(ov["r2"][i][k], 0.5)

    def test_holdings_overlap_is_a_marked_stub(self):
        with self.assertRaises(NotImplementedError):
            pt.holdings_overlap(122639, 118989)


class BasketTests(SimpleTestCase):
    def _pool(self):
        rng = np.random.default_rng(3)
        n, months = 12, 120
        market = rng.normal(0.008, 0.04, months)
        rets, pool = {}, []
        for k in range(n):
            vol = 0.01 + 0.006 * k                 # monthly idiosyncratic scale
            beta = 0.2 + 0.1 * k
            r = 0.004 + 0.0008 * k + beta * market + rng.normal(0, vol, months)
            rets[1000 + k] = r
        # an exact clone of fund 1011 should never join alongside it
        rets[2000] = rets[1011] * 1.0
        df = pd.DataFrame(rets, index=pd.date_range("2015-01-31", periods=months, freq="ME"))
        for c in df.columns:
            mu = df[c].mean() * 12
            pool.append({"code": c, "name": f"Fund {c}", "category": "equity",
                         "cagr": mu, "vol": df[c].std() * np.sqrt(12)})
        return pool, df

    def test_basket_respects_vol_cap(self):
        pool, df = self._pool()
        for cap in (0.10, 0.15, 0.20):
            res = pt.build_basket(pool, df, periods_per_year=12, target_return=0.30, max_vol=cap)
            self.assertTrue(res["funds"])
            self.assertLessEqual(res["vol"], cap + 1e-6)
            self.assertLessEqual(len(res["funds"]), 6)
            self.assertAlmostEqual(sum(f["weight"] for f in res["funds"]), 1.0, places=6)

    def test_basket_skips_highly_correlated_candidates(self):
        pool, df = self._pool()
        res = pt.build_basket(pool, df, periods_per_year=12, target_return=0.40, max_vol=0.40)
        codes = {f["code"] for f in res["funds"]}
        self.assertFalse({1011, 2000} <= codes)

    def test_classify_scheme(self):
        self.assertEqual(pt.classify_scheme("HDFC Mid Cap Fund - Growth Option - Direct Plan"), "equity")
        self.assertEqual(pt.classify_scheme("ICICI Prudential Liquid Fund - Direct Plan - Growth"), "debt")
        self.assertEqual(pt.classify_scheme("ICICI Prudential Equity & Debt Fund - Direct Plan - Growth"), "hybrid")
        self.assertEqual(pt.classify_scheme("Kotak Balanced Advantage Fund - Direct Plan - Growth"), "hybrid")
        self.assertEqual(pt.classify_scheme("SBI Magnum Gilt Fund - Direct Plan - Growth"), "debt")


@mock.patch.object(pt, "benchmark_series", lambda: None)
class RealDataTests(TestCase):
    def setUp(self):
        if not os.path.exists(PARQUET):
            self.skipTest("local NAV parquet absent")

    def test_three_real_schemes_end_to_end(self):
        res = pt.analyse_portfolio([(122639, 50000), (118989, 30000), (120197, 20000)])
        self.assertEqual([f["code"] for f in res["funds"]], [122639, 118989, 120197])
        self.assertFalse(res["dropped"])
        for key in ("current", "min_var", "max_sharpe"):
            self.assertAlmostEqual(sum(res["portfolios"][key]["weights"]), 1.0, places=6)
        self.assertAlmostEqual(res["portfolios"]["current"]["weights"][0], 0.5, places=6)
        # liquid fund is by far the least volatile → dominates min-variance
        mv = res["portfolios"]["min_var"]["weights"]
        self.assertGreater(mv[2], 0.9)
        self.assertLess(res["portfolios"]["min_var"]["vol"], res["portfolios"]["current"]["vol"])
        self.assertGreaterEqual(len(res["frontier"]), 20)
        self.assertEqual(len(res["overlap"]["r2"]), 3)
        vols = {f["code"]: f["vol"] for f in res["funds"]}
        self.assertLess(vols[120197], 0.02)
        self.assertGreater(vols[118989], 0.10)

    def test_resolve_isin_and_name(self):
        self.assertEqual(pt.resolve_isin("INF879O01027"), 122639)
        self.assertEqual(pt.match_scheme_by_name("HDFC Mid-Cap Opportunities Direct-G"), 118989)


def _fake_bench():
    rows = pt.nav_history.local_nav_history(122639)
    return pd.Series([n for _, n in rows], index=pd.to_datetime([d for d, _ in rows]))


class ViewTests(TestCase):
    def setUp(self):
        if not os.path.exists(PARQUET):
            self.skipTest("local NAV parquet absent")
        self.user = get_user_model().objects.create_user("pt", password="pw")
        self.client.login(username="pt", password="pw")
        self.url = reverse("portfolio_tools")

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_get_renders_inputs(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "scheme_code,amount")
        self.assertContains(r, "Use my tracked mutual funds")

    @mock.patch.object(pt, "benchmark_series", _fake_bench)
    def test_csv_upload_runs_analysis_and_reports_bad_codes(self):
        csv = b"scheme_code,amount\n122639,50000\n118989,30000\n999999999,100\nxyz,5\n"
        r = self.client.post(self.url, {"action": "csv",
                                        "file": SimpleUploadedFile("p.csv", csv, "text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Max-Sharpe")
        self.assertContains(r, "999999999")
        self.assertContains(r, "line 5")
        self.assertContains(r, "CAPM")
        self.assertContains(r, "return co-movement")

    @mock.patch.object(pt, "benchmark_series", _fake_bench)
    def test_use_tracked_mutual_funds(self):
        acct = Account.objects.create(user=self.user, name="Folio")
        for name, isin, val in (("Parag Parikh Flexi Cap Direct", "INF879O01027", 60000),
                                ("HDFC Mid-Cap Opportunities Direct-G", "", 40000)):
            a = Asset.objects.create(asset_type=Asset.AssetType.MUTUAL_FUND, name=name,
                                     identifier=isin or f"x:{name}", isin=isin)
            Holding.objects.create(asset=a, account=acct, quantity=1, invested=Decimal(val),
                                   current_value=Decimal(val))
        r = self.client.post(self.url, {"action": "tracked"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Parag Parikh Flexi Cap Fund - Direct Plan - Growth")
        self.assertContains(r, "HDFC Mid Cap Fund")
        self.assertContains(r, "Max-Sharpe")

    def test_basket_builder_view(self):
        pool = [{"code": 122639, "name": "PPFAS Flexi", "category": "equity", "cagr": 0.18, "vol": 0.13},
                {"code": 118989, "name": "HDFC Mid", "category": "equity", "cagr": 0.20, "vol": 0.17},
                {"code": 120197, "name": "ICICI Liquid", "category": "debt", "cagr": 0.065, "vol": 0.005}]
        with mock.patch.object(pt, "basket_candidate_pool", lambda categories: pool):
            r = self.client.post(self.url, {"action": "basket", "target_return": "12",
                                            "max_vol": "10", "categories": ["equity", "debt"]})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "screening tool")
        self.assertContains(r, "ICICI Liquid")
