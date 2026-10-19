"""Local NAV history (tigzig full-dataset Parquet) accessor tests."""
import os
from unittest import mock

from django.test import TestCase

from core import nav_history

PARQUET = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "amfi_nav_master.parquet")


@mock.patch.object(nav_history, "NAV_PARQUET", PARQUET)
@mock.patch.object(nav_history, "_parquet_exists", lambda: os.path.exists(PARQUET))
class LocalNavHistoryTests(TestCase):
    def test_returns_ascending_history(self):
        rows = nav_history.local_nav_history(122639)
        self.assertGreater(len(rows), 3000)
        dates = [d for d, _ in rows]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates[0], "2013-05-28")  # PPFCF direct inception

    def test_unknown_scheme_returns_empty(self):
        self.assertEqual(nav_history.local_nav_history(999999999), [])

    def test_missing_file_returns_empty_not_crash(self):
        with mock.patch.object(nav_history, "_parquet_exists", lambda: False):
            self.assertEqual(nav_history.local_nav_history(122639), [])

    def test_row_shape(self):
        rows = nav_history.local_nav_history(122639)
        d, nav = rows[-1]
        self.assertIsInstance(d, str)
        self.assertIsInstance(nav, float)
        self.assertGreater(nav, 0)

    def test_scheme_meta(self):
        meta = nav_history.local_scheme_meta(122639)
        self.assertIn("Parag Parikh", meta["scheme_name"])
        self.assertTrue(meta["isin_growth"].startswith("INF"))
        self.assertEqual(nav_history.local_scheme_meta(999999999), {})
