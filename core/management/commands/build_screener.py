"""Precompute screener metrics into data/screener_metrics.parquet.

Usage:
    .venv/bin/python manage.py build_screener                # full universe
    .venv/bin/python manage.py build_screener --limit 20     # smoke run
    .venv/bin/python manage.py build_screener --codes 120465,118778
"""
from django.core.management.base import BaseCommand

from core import screener


class Command(BaseCommand):
    help = "Precompute rolling-consistency screener metrics from the local NAV Parquet"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="cap the universe (smoke run)")
        parser.add_argument("--codes", type=str, default=None, help="comma-separated scheme codes")

    def handle(self, *args, **opts):
        codes = [int(c) for c in opts["codes"].split(",")] if opts["codes"] else None
        df = screener.build(limit=opts["limit"], codes=codes)
        self.stdout.write(self.style.SUCCESS(
            f"screener metrics: {len(df)} rows over {df.scheme_code.nunique()} schemes "
            f"-> {screener.SCREENER_PARQUET}"
        ))
