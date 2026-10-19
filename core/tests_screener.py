"""Screener: metric computation against synthetic ground truth and the view."""
import os
from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import nav_history, screener as sc

PARQUET = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "data", "amfi_nav_master.parquet")
HAS_PARQUET = os.path.exists(PARQUET)


def _monthly(nav_or_bench: np.ndarray) -> pd.Series:
    idx = pd.date_range("2006-04-30", periods=len(nav_or_bench), freq="ME")
    return pd.Series(nav_or_bench, index=idx, name="value")


class RollingCagrTests(SimpleTestCase):
    def test_cagr_matches_closed_form(self):
        # 12 months of +1% monthly returns -> every 12m window CAGR = (1.01^12 - 1)
        nav = _monthly(100 * np.cumprod(np.full(13, 1.01)))
        got = sc.rolling_cagrs(nav, 12)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(float(got[0]), 1.01 ** 12 - 1, places=10)

    def test_window_longer_than_series_is_empty(self):
        nav = _monthly(np.linspace(100, 110, 6))
        self.assertEqual(len(sc.rolling_cagrs(nav, 12)), 0)

    def test_zero_nav_does_not_crash(self):
        v = np.array([100.0, 0.0, 100.0, 100.0, 100.0])
        nav = _monthly(v)
        got = sc.rolling_cagrs(nav, 2)
        self.assertTrue(np.isfinite(got).all())


class BuildCrisilProxyTests(SimpleTestCase):
    def test_monthly_equity_series_is_not_resampled_again(self):
        equity = _monthly(np.linspace(100, 200, 80))
        debt = _monthly(np.linspace(100, 120, 80))
        with mock.patch.object(sc, "_debt_proxy", return_value=debt):
            proxy = sc.build_crisil_proxy(equity)
        self.assertIsNotNone(proxy)
        self.assertEqual(len(proxy), len(equity) - 1)

    def test_daily_equity_series_gets_month_ended(self):
        idx = pd.bdate_range("2006-04-03", periods=2200)   # ~104 months
        equity = pd.Series(np.linspace(100, 150, 2200), index=idx)
        debt = _monthly(np.linspace(100, 120, 110))
        with mock.patch.object(sc, "_debt_proxy", return_value=debt):
            proxy = sc.build_crisil_proxy(equity)
        self.assertIsNotNone(proxy)
        # month-end timestamps only, roughly one per month
        self.assertTrue(len(proxy) <= 105)


class FetchJugaadIndexTests(SimpleTestCase):
    def test_list_of_dicts_is_converted(self):
        # 600 rows: _fetch_jugaad_index rejects anything under 500
        rows = [{"Date": f"01 {m} 2026", "TotalReturnsIndex": str(1000 + i)}
                for i, m in enumerate(["Jan", "Feb", "Mar", "Apr"] * 150)]
        with mock.patch("jugaad_data.nse.index_tri_raw", return_value=rows) as f:
            got = sc._fetch_jugaad_index("nifty100_tri")
        f.assert_called_once()
        kwargs = f.call_args.kwargs
        from datetime import date
        self.assertIsInstance(kwargs["from_date"], date)
        self.assertIsInstance(kwargs["to_date"], date)   # a string crashes jugaad
        self.assertEqual(len(got), 600)

    def test_short_series_returns_none(self):
        rows = [{"Date": "01 Jan 2026", "TotalReturnsIndex": "1000"}]
        with mock.patch("jugaad_data.nse.index_tri_raw", return_value=rows):
            self.assertIsNone(sc._fetch_jugaad_index("nifty100_tri"))

    def test_dash_values_are_dropped(self):
        rows = [{"Date": "01 Jan 2026", "TotalReturnsIndex": "-"},
                {"Date": "01 Feb 2026", "TotalReturnsIndex": "1000"}]
        with mock.patch("jugaad_data.nse.index_tri_raw", return_value=rows):
            self.assertIsNone(sc._fetch_jugaad_index("nifty100_tri"))


class SchemeBenchmarksTests(SimpleTestCase):
    def test_hybrid_category_gets_two_series(self):
        bench = {k: object() for k in ("nifty100_tri", "crisil6535", "nifty500_tri")}
        got = sc.scheme_benchmarks("Aggressive Hybrid Fund", bench)
        self.assertEqual(sorted(got), ["crisil6535", "nifty100_tri"])

    def test_equity_category_gets_only_its_mapped_series(self):
        bench = {k: object() for k in ("nifty100_tri", "midcap150_tri", "nifty500_tri")}
        got = sc.scheme_benchmarks("Mid Cap Fund", bench)
        self.assertEqual(list(got), ["midcap150_tri"])

    def test_unknown_category_falls_back_to_nifty500(self):
        bench = {"nifty500_tri": object(), "nifty100_tri": object()}
        got = sc.scheme_benchmarks("Not A Category", bench)
        self.assertEqual(list(got), ["nifty500_tri"])


class ScreenerQueryTests(SimpleTestCase):
    def test_none_thresholds_skip_predicates(self):
        from unittest import mock
        con_mock = mock.MagicMock()
        con_mock.execute.return_value.fetchdf.return_value = pd.DataFrame()
        with mock.patch.object(nav_history, "_connect", return_value=con_mock):
            sc.query(None, None, "3Y", 0.6, None, None, None)
        sql = con_mock.execute.call_args[0][0]
        self.assertIn("ro3Y >= ?", sql)
        self.assertNotIn("up3Y", sql)
        self.assertNotIn("dc3Y", sql)
        with mock.patch.object(nav_history, "_connect", return_value=con_mock):
            sc.query(None, None, "3Y", 0.6, 0.7, None, None)
        sql = con_mock.execute.call_args[0][0]
        self.assertIn("up3Y >= ?", sql)
        self.assertNotIn("dc3Y", sql)


class ScreenerViewTests(TestCase):
    def test_login_required(self):
        resp = self.client.get(reverse("screener"))
        self.assertEqual(resp.status_code, 302)

    def _login(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        User = get_user_model()
        u, _ = User.objects.get_or_create(username="scr", defaults={"is_staff": False})
        u.set_password("pw")
        u.save()
        c = Client()
        c.force_login(u)
        return c

    def test_unsubmitted_page_renders(self):
        c = self._login()
        resp = c.get(reverse("screener"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "How to read the results")

    @mock.patch.object(sc, "query")
    def test_results_render(self, q):
        if not HAS_PARQUET:
            self.skipTest("nav parquet not built")
        q.return_value = pd.DataFrame([{
            "scheme_code": 118989, "scheme_name": "Test Flexi Fund - Growth",
            "category": "Flexi Cap Fund", "benchmark": "nifty500_tri",
            "ro3Y": 0.75, "up3Y": 0.8, "dc3Y": 0.7, "tr3Y": 0.14,
            "tb3Y": 0.12, "ex3Y": 0.02, "n3Y": 120,
        }])
        c = self._login()
        resp = c.get(reverse("screener"), {"window": "3Y", "go": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Flexi Fund")
        self.assertContains(resp, "75.0%")

    def test_bad_window_falls_back(self):
        c = self._login()
        resp = c.get(reverse("screener"), {"window": "7Y", "go": "1"})
        self.assertEqual(resp.status_code, 200)
