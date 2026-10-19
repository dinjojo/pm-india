"""Dashboard context: movement split, goal classification, narrative, value gaps."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from core.dashboard_context import (build_dashboard_context, classify_goal, goal_statuses,
                                    smart_narrative, snapshot_movement, value_gaps)
from core.models import Account, Asset, GoalPlan, Holding, PricePoint
from core.portfolio import portfolio_view


def snap(d, invested, current, estimated=False):
    return SimpleNamespace(date=d, invested=Decimal(invested), current=Decimal(current),
                           estimated=estimated)


class SnapshotMovementTests(SimpleTestCase):
    def test_needs_two_points(self):
        self.assertIsNone(snapshot_movement([]))
        self.assertIsNone(snapshot_movement([snap(date(2026, 1, 1), 100, 100)]))

    def test_base_is_last_point_on_or_before_cutoff(self):
        d = date(2026, 10, 19)
        snaps = [snap(d - timedelta(days=60), 1000, 1000),
                 snap(d - timedelta(days=31), 1000, 1100),
                 snap(d - timedelta(days=10), 1200, 1250),
                 snap(d, 1200, 1300)]
        m = snapshot_movement(snaps, days=30)
        self.assertEqual(m["from_date"], d - timedelta(days=31))
        self.assertEqual(m["change"], Decimal(200))
        self.assertEqual(m["flows"], Decimal(200))   # new money, not market
        self.assertEqual(m["market"], Decimal(0))

    def test_short_history_falls_back_to_first_point(self):
        d = date(2026, 10, 19)
        m = snapshot_movement([snap(d - timedelta(days=5), 1000, 1000),
                               snap(d, 1000, 950)], days=30)
        self.assertEqual(m["days"], 5)
        self.assertEqual(m["market"], Decimal(-50))
        self.assertEqual(m["market_abs"], Decimal(50))
        self.assertAlmostEqual(m["change_pct"], -5.0)

    def test_estimated_backfill_is_ignored(self):
        d = date(2026, 10, 19)
        snaps = [snap(d - timedelta(days=60), 900, 900, estimated=True),
                 snap(d - timedelta(days=1), 1000, 1100),
                 snap(d, 1000, 1120)]
        m = snapshot_movement(snaps, days=30)
        self.assertEqual(m["from_date"], d - timedelta(days=1))
        self.assertEqual(m["market"], Decimal(20))
        self.assertIsNone(snapshot_movement(snaps[:2], days=30))  # one exact point only


class ClassifyGoalTests(SimpleTestCase):
    def test_retirement_uses_engine_flag(self):
        self.assertEqual(classify_goal("retirement", net_corpus=1e6, on_track=False, affordability=None), "off_track")
        self.assertEqual(classify_goal("retirement", net_corpus=0, on_track=True, affordability=None), "funded")

    def test_other_goals_use_affordability_band(self):
        self.assertEqual(classify_goal("one_time", net_corpus=0, on_track=None, affordability=None), "funded")
        self.assertEqual(classify_goal("one_time", net_corpus=5e5, on_track=None,
                                       affordability={"band": "aggressive"}), "off_track")
        self.assertEqual(classify_goal("recurring", net_corpus=5e5, on_track=None,
                                       affordability={"band": "stretch"}), "stretch")
        self.assertEqual(classify_goal("one_time", net_corpus=5e5, on_track=None, affordability=None), "on_track")


class NarrativeTests(SimpleTestCase):
    def test_empty(self):
        self.assertIn("No holdings yet", smart_narrative(total_current=0, total_invested=0, holdings_count=0,
                                                         movement=None, attention_count=0))

    def test_loss_and_attention_wording(self):
        s = smart_narrative(total_current=Decimal(90000), total_invested=Decimal(100000), holdings_count=3,
                            movement={"days": 30, "market": Decimal(-2500)}, attention_count=1)
        self.assertIn("worth ₹90.0 K, down ₹10.0 K (10.0%)", s)
        self.assertIn("markets took off ₹2.5 K over the last 30 days", s)
        self.assertIn("1 item needs a look", s)


class DbBuildersTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="p")
        self.acct = Account.objects.create(user=self.user, name="Demat")

    def _holding(self, name, ident, invested, current_value=None, atype="mf"):
        a = Asset.objects.create(asset_type=atype, name=name, identifier=ident)
        return Holding.objects.create(asset=a, account=self.acct, quantity=10,
                                      invested=invested, current_value=current_value)

    def test_value_gaps_missing_and_stale(self):
        today = date(2026, 10, 19)
        self._holding("No price", "INF1", 5000)
        self._holding("Manual", "", 3000, current_value=Decimal(3100), atype="ppf")
        self._holding("Stale", "INF2", 1000)
        self._holding("Fresh", "INF3", 1000)
        PricePoint.objects.create(asset_type="mf", identifier="INF2", price=10, as_of=today - timedelta(days=20))
        PricePoint.objects.create(asset_type="mf", identifier="INF3", price=10, as_of=today - timedelta(days=2))
        gaps = value_gaps(self.user, today=today)
        self.assertEqual([g["name"] for g in gaps["missing"]], ["No price"])
        self.assertEqual([g["name"] for g in gaps["stale"]], ["Stale"])

    def test_goals_sorted_off_track_first(self):
        GoalPlan.objects.create(user=self.user, name="Far funded", kind="one_time",
                                cost_today=100000, years_to_goal=10, current_equity=10_000_000)
        GoalPlan.objects.create(user=self.user, name="Retire", kind="retirement",
                                current_age=35, retire_age=50, monthly_expense=50000)
        names = [g["name"] for g in goal_statuses(self.user)]
        self.assertEqual(names, ["Retire", "Far funded"])

    def test_build_context_renders_dashboard_shape(self):
        self._holding("No price", "INF1", 5000)
        ctx = build_dashboard_context(self.user, portfolio_view(self.user))["dash"]
        self.assertEqual(ctx["attention_count"], 1)  # the missing value
        self.assertIsNone(ctx["movement"])
        self.assertTrue(ctx["narrative"].startswith("Your 1 holding is worth ₹5.0 K"))
        self.assertIn("1 item needs a look", ctx["narrative"])
