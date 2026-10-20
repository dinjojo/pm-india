"""Screener: metric computation against synthetic ground truth and the view."""
import os
import io
import tempfile
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


class WorkbookBenchmarkMappingTests(SimpleTestCase):
    """Category defaults follow the AUG-2021 freefincal workbook."""
    WORKBOOK = {
        "ELSS": "nifty200_tri",
        "Focussed Fund": "largemid250_tri",
        "Multi Cap Fund": "largemid250_tri",
        "Small Cap Fund": "midcap150_tri",
        "Value Fund": "nifty100_tri",
        "Contra Fund": "nifty100_tri",
        "Dividend Yield Fund": "nifty100_tri",
        "Sectoral/Thematic": "nifty100_tri",
    }

    def test_changed_categories_use_workbook_index(self):
        for cat, key in self.WORKBOOK.items():
            with self.subTest(cat=cat):
                self.assertEqual(sc.BENCHMARK_MAP[cat], (key, None))

    def test_scheme_benchmarks_selects_workbook_series(self):
        bench = {k: object() for k in sc.BENCHMARK_LABELS}
        for cat, key in self.WORKBOOK.items():
            with self.subTest(cat=cat):
                self.assertEqual(list(sc.scheme_benchmarks(cat, bench)), [key])

    def test_every_mapped_key_has_label_and_source(self):
        for key in sc.needed_benchmarks():
            with self.subTest(key=key):
                self.assertIn(key, sc.BENCHMARK_LABELS)
                if key != "crisil6535":
                    self.assertIn(key, sc.JUGAAD_INDICES)
        self.assertEqual(sc.BENCHMARK_LABELS["nifty200_tri"], "NIFTY 200 TRI")


class BuildScreenerBenchmarkTests(SimpleTestCase):
    def _universe(self):
        cats = list(sc.BENCHMARK_MAP)
        return pd.DataFrame({"scheme_code": range(1, len(cats) + 1),
                             "scheme_name": [f"Fund {c}" for c in cats],
                             "category": cats, "n": 2000})

    def test_full_build_loads_every_required_series(self):
        from django.core.management import call_command
        idx = pd.bdate_range("2006-03-01", periods=3000)
        series = pd.Series(np.linspace(100, 500, len(idx)), index=idx)
        seen = {}

        def fake_metrics(code, bench):
            seen[code] = sorted(bench)
            return [{"scheme_code": code, "benchmark": k, "ro3Y": 0.5} for k in bench]

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sc, "SCREENER_PARQUET", os.path.join(tmp, "m.parquet")), \
             mock.patch.object(sc, "universe", return_value=self._universe()), \
             mock.patch.object(sc, "load_nifty500_tri", return_value=series), \
             mock.patch.object(sc, "load_benchmark",
                               side_effect=lambda k: (series, sc.BENCHMARK_LABELS[k])) as lb, \
             mock.patch.object(sc, "build_crisil_proxy", return_value=series), \
             mock.patch.object(sc, "scheme_metrics", side_effect=fake_metrics), \
             mock.patch.object(nav_history, "latest_nav_date",
                               return_value=idx[-1].date()):
            call_command("build_screener", stdout=out)
            written = pd.read_parquet(sc.SCREENER_PARQUET)
        loaded = {c.args[0] for c in lb.call_args_list}
        self.assertEqual(loaded, {"nifty100_tri", "nifty200_tri", "largemid250_tri",
                                  "midcap150_tri"})
        for label in ("NIFTY 500 TRI", "NIFTY 200 TRI", "NIFTY 100 TRI",
                      "NIFTY LARGEMIDCAP 250 TRI", "NIFTY MIDCAP 150 TRI"):
            self.assertIn(label, out.getvalue())
        # every fund was scored against its own category's series only
        uni = self._universe()
        for code, cat in zip(uni.scheme_code, uni.category):
            expected = sorted(k for k in sc.BENCHMARK_MAP[cat] if k)
            self.assertEqual(seen[code], expected, cat)
        self.assertIn("nav_as_of", written.columns)

    def test_missing_series_aborts_instead_of_mislabelled_fallback(self):
        from django.core.management import call_command, CommandError
        idx = pd.bdate_range("2006-03-01", periods=3000)
        series = pd.Series(np.linspace(100, 500, len(idx)), index=idx)
        uni = self._universe()
        uni = uni[uni.category == "ELSS"]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sc, "SCREENER_PARQUET", os.path.join(tmp, "m.parquet")), \
             mock.patch.object(sc, "universe", return_value=uni), \
             mock.patch.object(sc, "load_nifty500_tri", return_value=series), \
             mock.patch.object(sc, "load_benchmark",
                               return_value=(None, "NIFTY 500 TRI")), \
             mock.patch.object(sc, "scheme_metrics") as metrics:
            with self.assertRaisesRegex(CommandError, "nifty200_tri"):
                call_command("build_screener", stdout=io.StringIO())
            metrics.assert_not_called()
            self.assertFalse(os.path.exists(sc.SCREENER_PARQUET))


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


class ScreenerQueryPaginationTests(SimpleTestCase):
    def _run(self, fn, *args, **kwargs):
        con_mock = mock.MagicMock()
        con_mock.execute.return_value.fetchdf.return_value = pd.DataFrame()
        con_mock.execute.return_value.fetchone.return_value = (1234,)
        with mock.patch.object(nav_history, "_connect", return_value=con_mock):
            out = fn(*args, **kwargs)
        sql, params = con_mock.execute.call_args[0]
        return out, sql, params

    def test_old_positional_call_has_no_silent_cap(self):
        _, sql, _ = self._run(sc.query, None, None, "3Y", 0.6, None, None, None)
        self.assertNotIn("LIMIT", sql)

    def test_order_has_deterministic_tiebreaker(self):
        _, sql, _ = self._run(sc.query, None, None, "5Y", 0.6, None, None, None)
        self.assertIn("ORDER BY ro5Y DESC, scheme_code ASC, benchmark ASC", sql)

    def test_limit_offset_are_bound_params(self):
        _, sql, params = self._run(sc.query, "ELSS", None, "3Y", 0.6, None, None, None,
                                   limit=50, offset=100)
        self.assertTrue(sql.endswith("LIMIT ? OFFSET ?"))
        self.assertEqual(params[-2:], [50, 100])
        self.assertIn("ELSS", params)

    def test_count_uses_same_filters(self):
        n, sql, params = self._run(sc.query_count, "ELSS", None, "3Y", 0.6, 0.7, None, None)
        self.assertEqual(n, 1234)
        self.assertIn("SELECT count(*)", sql)
        self.assertIn("up3Y >= ?", sql)
        self.assertNotIn("LIMIT", sql)
        self.assertEqual(params[1:], ["ELSS", 0.6, 0.7])

    def test_count_distinct_schemes(self):
        _, sql, _ = self._run(sc.query_count, None, None, "3Y", None, None, None, "excess",
                              distinct_schemes=True)
        self.assertIn("count(DISTINCT scheme_code)", sql)
        self.assertIn("ex3Y > 0", sql)


class CategorizeTests(SimpleTestCase):
    def test_existing_categories(self):
        cases = {
            "Axis Large Cap Fund - Direct Plan - Growth": "Large Cap Fund",
            "Canara Robeco Large and Mid Cap Fund - Regular Plan - Growth": "Large & Mid Cap Fund",
            "Mirae Asset Large & Midcap Fund - Direct Plan - Growth": "Large & Mid Cap Fund",
            "Parag Parikh Flexi Cap Fund - Direct Plan - Growth": "Flexi Cap Fund",
            "HDFC Aggressive Hybrid Fund - Growth Plan": "Aggressive Hybrid Fund",
            "SBI Small Cap Fund - Regular Plan - Growth": "Small Cap Fund",
            "Kotak ELSS Tax Saver Fund - Growth": "ELSS",
            "ICICI Prudential Technology Fund - Growth": "Sectoral/Thematic",
        }
        for name, cat in cases.items():
            self.assertEqual(sc.categorize(name), cat, name)

    def test_new_hybrid_categories(self):
        self.assertEqual(sc.categorize("ICICI Prudential Multi Asset Allocation Fund - Growth"),
                         "Multi Asset Allocation Fund")
        self.assertEqual(sc.categorize("quant Multi Asset Allocation Fund - Growth"),
                         "Multi Asset Allocation Fund")       # not Sectoral via "quant"
        self.assertEqual(sc.categorize("UTI Balanced Hybrid Fund-Direct Plan Growth Option"),
                         "Balanced Hybrid Fund")
        self.assertEqual(sc.categorize("SBI AGGRESIVE HYBRID FUND - REGULAR PLAN -Growth"),
                         "Aggressive Hybrid Fund")            # AMFI typo
        self.assertEqual(sc.categorize("Aditya Birla Sun Life Multi-Cap Fund-Direct Growth"),
                         "Multi Cap Fund")
        for cat in ("Multi Asset Allocation Fund", "Balanced Hybrid Fund"):
            self.assertIn(cat, sc.BENCHMARK_MAP)

    def test_thematic_keyword_gaps(self):
        for name in ("SBI MNC FUND - DIRECT PLAN -GROWTH",
                     "Mirae Asset Great Consumer Fund - Direct Plan - Growth",
                     "Nippon India Power & Infra Fund-Growth Plan -Growth Option",
                     "DSP India T.I.G.E.R. Fund - Regular Plan - Growth",
                     "SBI COMMA Fund - REGULAR PLAN - Growth",
                     "Sundaram Services Fund Direct Plan - Growth",
                     "Tata Ethical Fund-Regular Plan - Growth Option",
                     "Franklin Build India Fund Growth Plan"):
            self.assertEqual(sc.categorize(name), "Sectoral/Thematic", name)

    def test_sector_funds_named_opportunities_are_not_flexi(self):
        self.assertEqual(sc.categorize("SBI Healthcare Opportunities Fund - Growth"),
                         "Sectoral/Thematic")
        self.assertEqual(sc.categorize("Invesco India PSU Equity Fund - Growth"),
                         "Sectoral/Thematic")
        self.assertEqual(sc.categorize("Franklin India Opportunities Fund - Growth"),
                         "Flexi Cap Fund")

    def test_formerly_known_as_does_not_veto(self):
        self.assertEqual(sc.categorize(
            "Groww ELSS Tax Saver Fund (formerly known as Indiabulls Tax Savings Fund)"
            "-Regular Plan-Growth Option"), "ELSS")

    def test_non_equity_stays_out(self):
        for name in ("Quantum Dynamic Term Fund - Direct Plan Growth",
                     "HDFC Banking and PSU Debt Fund - Growth",
                     "ICICI Prudential Equity Arbitrage Fund - Growth",
                     "UTI Nifty 50 Index Fund - Growth",
                     "Nippon India ETF Nifty BeES",
                     "SBI Gold Fund - Growth",
                     "Kotak Equity Savings Fund - Growth",
                     "ICICI Prudential Balanced Advantage Fund - Growth",
                     "HDFC Retirement Fund - Equity Plan - Growth Option",
                     "Axis Children's Fund - Lock in - Direct Growth",
                     "Nippon India US Equity Opportunities Fund - Growth",
                     "ICICI Prudential US Bluechip Equity Fund - Growth",
                     "HDFC Dynamic Term Fund - Growth"):
            self.assertIsNone(sc.categorize(name), name)


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

    @mock.patch.object(sc, "query_count", return_value=1)
    @mock.patch.object(sc, "query")
    def test_results_render(self, q, _qc):
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

    @mock.patch.object(sc, "query_count")
    @mock.patch.object(sc, "query")
    def test_pagination_offsets_and_exact_count(self, q, qc):
        qc.side_effect = lambda *a, distinct_schemes=False: 120 if distinct_schemes else 137
        q.return_value = pd.DataFrame([{
            "scheme_code": 1, "scheme_name": "Paged Fund", "category": "ELSS",
            "benchmark": "nifty500_tri", "ro3Y": 0.7, "up3Y": None, "dc3Y": None,
            "tr3Y": 0.1, "tb3Y": 0.09, "ex3Y": 0.01, "n3Y": 60,
        }])
        c = self._login()
        resp = c.get(reverse("screener"), {"window": "3Y", "go": "1", "page": "2"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(q.call_args.kwargs, {"limit": 50, "offset": 50})
        self.assertContains(resp, "137 funds matched")
        self.assertContains(resp, "120 distinct schemes")
        self.assertContains(resp, "Page 2 of 3")
        self.assertContains(resp, "page=3")
        self.assertContains(resp, "window=3Y")      # filters survive in pager links

    @mock.patch.object(sc, "query_count", return_value=137)
    @mock.patch.object(sc, "query", return_value=pd.DataFrame())
    def test_out_of_range_page_clamps_to_last(self, q, _qc):
        c = self._login()
        resp = c.get(reverse("screener"), {"go": "1", "page": "999"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(q.call_args.kwargs, {"limit": 37, "offset": 100})

    def test_bad_window_falls_back(self):
        c = self._login()
        resp = c.get(reverse("screener"), {"window": "7Y", "go": "1"})
        self.assertEqual(resp.status_code, 200)

    @mock.patch.object(sc, "query_count", return_value=0)
    def test_thresholds_default_to_workbook_and_can_be_overridden(self, qc):
        c = self._login()
        c.get(reverse("screener"), {"window": "3Y", "go": "1"})
        # (category, benchmark, window, outperf, upside, downside, mode)
        self.assertEqual(qc.call_args.args[3:6], (0.7, 0.7, 0.7))
        c.get(reverse("screener"), {"window": "3Y", "go": "1", "outperf_min": "0.5",
                                    "upside_min": "", "downside_min": "0.9"})
        self.assertEqual(qc.call_args.args[3:6], (0.5, None, 0.9))


class ScreenerFreshnessTests(TestCase):
    def test_refresh_command_replaces_master_rebuilds_and_dates_results(self):
        from django.core.management import call_command
        from django.contrib.auth import get_user_model
        idx = pd.bdate_range("2010-01-01", periods=2600)
        source = pd.DataFrame({"date": idx.strftime("%Y-%m-%d"),
            "scheme_code": 12345, "scheme_name": "Example Flexi Cap Fund - Growth",
            "isin": "INF000000001", "nav": np.linspace(10, 37, len(idx))})
        payload = io.BytesIO()
        source.to_parquet(payload, index=False)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [payload.getvalue()]
        benchmark = pd.Series(np.linspace(10, 20, len(idx)), index=idx)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(nav_history, "NAV_PARQUET", os.path.join(tmp, "nav.parquet")), \
             mock.patch.object(sc, "SCREENER_PARQUET", os.path.join(tmp, "metrics.parquet")), \
             mock.patch.object(sc, "load_nifty500_tri", return_value=benchmark), \
             mock.patch.object(sc, "_fetch_jugaad_index", return_value=None), \
             mock.patch.object(sc, "build_crisil_proxy", return_value=None), \
             mock.patch("core.nav_history.requests.get", return_value=response):
            call_command("build_screener", "--refresh", "--codes", "12345", stdout=io.StringIO())
            self.assertEqual(nav_history.local_nav_history(12345)[-1][1], 37)
            self.assertEqual(sc.data_as_of(), idx[-1].date())
            rows = sc.query(None, None, "3Y", None, None, None, None)
            self.assertEqual(list(rows.scheme_code), [12345])
            self.assertEqual(rows.iloc[0].scheme_name, "Example Flexi Cap Fund - Growth")
            self.client.force_login(get_user_model().objects.create_user("fresh"))
            result = self.client.get(reverse("screener"), {"go": "1", "outperf_min": "0",
                                                          "upside_min": "", "downside_min": ""})
            self.assertContains(result, f'datetime="{idx[-1].date().isoformat()}"')
            self.assertContains(result, 'href="/funds/12345/"')
            # An ordinary rebuild must not download anything.
            with mock.patch("core.nav_history.requests.get", side_effect=AssertionError("Unexpected download")):
                call_command("build_screener", "--codes", "12345", stdout=io.StringIO())

    def test_failed_refresh_retains_previous_master_and_metrics(self):
        from django.core.management import call_command, CommandError
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"not a parquet file"]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(nav_history, "NAV_PARQUET", os.path.join(tmp, "nav.parquet")), \
             mock.patch.object(sc, "SCREENER_PARQUET", os.path.join(tmp, "metrics.parquet")), \
             mock.patch("core.nav_history.requests.get", return_value=response):
            for path in (nav_history.NAV_PARQUET, sc.SCREENER_PARQUET):
                with open(path, "wb") as f:
                    f.write(b"previous data")
            with self.assertRaises(CommandError):
                call_command("build_screener", "--refresh", stdout=io.StringIO())
            for path in (nav_history.NAV_PARQUET, sc.SCREENER_PARQUET):
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), b"previous data")
            self.assertEqual(len(os.listdir(tmp)), 2)

    def test_old_or_missing_metrics_do_not_claim_a_fresh_date(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sc, "SCREENER_PARQUET", os.path.join(tmp, "metrics.parquet")):
            self.assertIsNone(sc.data_as_of())
            pd.DataFrame({"scheme_code": [1]}).to_parquet(sc.SCREENER_PARQUET)
            self.assertIsNone(sc.data_as_of())
