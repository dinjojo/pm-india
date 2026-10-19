"""Precompute screener metrics into data/screener_metrics.parquet.

Usage:
    .venv/bin/python manage.py build_screener                # full universe
    .venv/bin/python manage.py build_screener --limit 20     # smoke run
    .venv/bin/python manage.py build_screener --codes 120465,118778
"""
from django.core.management.base import BaseCommand, CommandError

from core import nav_history, screener


class Command(BaseCommand):
    help = "Precompute rolling-consistency screener metrics from the local NAV Parquet"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="cap the universe (smoke run)")
        parser.add_argument("--codes", type=str, default=None, help="comma-separated scheme codes")
        parser.add_argument("--refresh", action="store_true",
                            help="Download and validate the latest AMFI NAV master before rebuilding")

    def handle(self, *args, **opts):
        codes = [int(c) for c in opts["codes"].split(",")] if opts["codes"] else None
        if opts["refresh"]:
            self.stdout.write("Refreshing AMFI NAV master (this can be a large download)...")
            try:
                newest = nav_history.refresh_nav_master()
            except Exception as exc:
                raise CommandError(f"NAV refresh failed; existing data was retained: {exc}") from exc
            self.stdout.write(f"Newest NAV: {newest}")
        # build() loads every benchmark series the selected categories map to
        # (the full universe needs NIFTY 500/200/100, LargeMidcap 250 and
        # Midcap 150 TRI) and scores each fund against its own category's
        # index; a missing series aborts rather than falling back silently.
        try:
            df = screener.build(limit=opts["limit"], codes=codes)
        except screener.BenchmarkUnavailable as exc:
            raise CommandError(f"{exc}; existing metrics were retained") from exc
        loaded = df.attrs.get("benchmarks_loaded", [])
        self.stdout.write("benchmarks: " + ", ".join(
            screener.BENCHMARK_LABELS.get(k, k) for k in loaded))
        self.stdout.write(self.style.SUCCESS(
            f"screener metrics: {len(df)} rows over {df.scheme_code.nunique()} schemes "
            f"-> {screener.SCREENER_PARQUET}"
        ))
