"""Tests for the six goal-planner gaps vs the Freefincal workbook:
post-retirement income, cash-flow schedule, 4-bucket ladder, editable settings,
under-26 advice path, and income flooring (annuity)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.goals import (Assumptions, bucket_ladder, retirement_plan, simulate_drawdown,
                        young_earner_advice)
from core.models import GoalPlan, RetirementSettings

BASE = dict(current_age=35, retire_age=50, monthly_expense=50_000)


# ---------------------------------------------------------------- 1. income
class IncomeStreamTests(TestCase):
    def test_income_stream_reduces_corpus(self):
        plain = retirement_plan(**BASE)
        rent = retirement_plan(**BASE, income_streams=[(20_000, 0)])
        self.assertLess(rent.gross_corpus, plain.gross_corpus)

    def test_escalating_stream_reduces_more(self):
        flat = retirement_plan(**BASE, income_streams=[(20_000, 0)])
        rising = retirement_plan(**BASE, income_streams=[(20_000, 5)])
        self.assertLess(rising.gross_corpus, flat.gross_corpus)

    def test_stream_inflated_to_retirement(self):
        r = retirement_plan(**BASE, income_streams=[(10_000, 5)])
        # 10k * 1.05^15 = 20,789/month at retirement
        self.assertEqual(r.income_monthly_at_retirement, 20_789)

    def test_full_coverage_leaves_only_emergency(self):
        # Pension that out-grows expenses covers everything: only emergency corpus left.
        r = retirement_plan(**BASE, income_streams=[(60_000, 7)])
        self.assertEqual(r.drawdown_corpus, 0)
        self.assertEqual(r.gross_corpus, r.emergency_corpus)

    def test_more_than_three_streams_rejected(self):
        with self.assertRaises(ValueError):
            retirement_plan(**BASE, income_streams=[(1, 0)] * 4)

    def test_blank_streams_are_zero(self):
        self.assertEqual(retirement_plan(**BASE).gross_corpus,
                         retirement_plan(**BASE, income_streams=[(0, 0), (0, 0), (0, 0)]).gross_corpus)

    def test_schedule_shows_income_applied(self):
        r = retirement_plan(**BASE, income_streams=[(10_000, 0)])
        self.assertEqual(r.cash_flow[0]["income"], 120_000)


# ------------------------------------------------------- 6. cash-flow table
class CashFlowTests(TestCase):
    def test_one_row_per_retirement_year_with_columns(self):
        r = retirement_plan(**BASE)
        self.assertEqual(len(r.cash_flow), 40)  # 50 → 90
        row = r.cash_flow[0]
        for key in ("age", "opening", "growth", "expense", "income", "floor",
                    "withdrawal", "closing"):
            self.assertIn(key, row)
        self.assertEqual(row["age"], 50)
        self.assertEqual(len(row["growth"]), 4)  # growth by bucket

    def test_funded_plan_never_runs_out(self):
        r = retirement_plan(**BASE)
        self.assertIsNone(r.depletion_age)
        self.assertTrue(all(row["closing"] >= 0 for row in r.cash_flow))

    def test_closing_rolls_into_next_opening(self):
        r = retirement_plan(**BASE)
        for prev, nxt in zip(r.cash_flow, r.cash_flow[1:]):
            self.assertAlmostEqual(prev["closing"], nxt["opening"], delta=1)

    def test_underfunded_corpus_runs_out(self):
        buckets = bucket_ladder(20, Assumptions())
        rows, depleted_year = simulate_drawdown(1_000_000, [200_000] * 20, buckets)
        self.assertIsNotNone(depleted_year)
        self.assertLess(depleted_year, 20)
        self.assertTrue(rows[depleted_year]["shortfall"] > 0)

    def test_current_path_depletion_age_reported(self):
        r = retirement_plan(**BASE, current_equity=2_000_000)
        self.assertFalse(r.on_track)
        self.assertIsNotNone(r.current_path_depletion_age)
        self.assertTrue(50 <= r.current_path_depletion_age < 90)


# ----------------------------------------------------- 3. 4-bucket ladder
class BucketLadderTests(TestCase):
    def test_workbook_spans_for_40_years(self):
        spans = [b.years for b in bucket_ladder(40, Assumptions())]
        self.assertEqual(spans, [15, 10, 8, 7])

    def test_workbook_spans_short_horizon(self):
        # round(0.4*20)=8 < 15 → low = round(0.5*5)=3 (Excel half-up), med = round(1.25)=1
        spans = [b.years for b in bucket_ladder(20, Assumptions())]
        self.assertEqual(spans, [15, 3, 1, 1])
        self.assertEqual(sum(b.years for b in bucket_ladder(12, Assumptions())), 12)

    def test_each_bucket_has_own_return(self):
        b = bucket_ladder(40, Assumptions())
        self.assertAlmostEqual(b[0].return_pct, 6.0)    # income ladder: taxable FI
        self.assertAlmostEqual(b[1].return_pct, 8.5)    # 50% equity / 50% FI
        self.assertAlmostEqual(b[2].return_pct, 9.1)    # 70% equity
        self.assertAlmostEqual(b[3].return_pct, 10.0)   # 100% equity

    def test_equity_return_now_matters_in_retirement(self):
        """The old blended 5.5% shortcut ignored equity returns post-retirement."""
        low = retirement_plan(**BASE, a=Assumptions(equity_return=8))
        high = retirement_plan(**BASE, a=Assumptions(equity_return=12))
        self.assertGreater(low.drawdown_corpus, high.drawdown_corpus)

    def test_bucket_growth_uses_bucket_rate(self):
        buckets = bucket_ladder(40, Assumptions())
        rows, _ = simulate_drawdown(50_000_000, [1_000_000] * 40, buckets)
        # After the first withdrawal (from the income bucket) each bucket grows at its rate.
        first = rows[0]
        for bal, g, b in zip(first["balances_after_withdrawal"], first["growth"], buckets):
            self.assertAlmostEqual(g, bal * b.return_pct / 100, delta=1)

    def test_corpus_sized_to_last_till_life_expectancy(self):
        r = retirement_plan(**BASE)
        self.assertLess(r.cash_flow[-1]["closing"], r.cash_flow[0]["expense"])


# ------------------------------------------------------------- 2. settings
class SettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("setuser", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    def test_singleton_defaults_from_workbook(self):
        s = RetirementSettings.load()
        self.assertEqual((float(s.inflation_working), float(s.inflation_retired),
                          float(s.equity_return), float(s.taxable_fi_return),
                          float(s.taxfree_return), s.life_expectancy), (6, 6, 10, 6, 7, 90))
        self.assertEqual(RetirementSettings.load().pk, s.pk)
        self.assertEqual(RetirementSettings.objects.count(), 1)

    def test_to_assumptions(self):
        s = RetirementSettings.load()
        s.equity_return = 11
        s.life_expectancy = 85
        s.save()
        a = RetirementSettings.load().to_assumptions()
        self.assertEqual(a.equity_return, 11)
        self.assertEqual(a.life_expectancy, 85)
        self.assertEqual(a.fixed_return, 6)

    def test_settings_page_get_and_post(self):
        self.assertEqual(self.client.get(reverse("goal_settings")).status_code, 200)
        r = self.client.post(reverse("goal_settings"), {
            "inflation_working": "7", "inflation_retired": "6.5", "equity_return": "11",
            "taxable_fi_return": "6", "taxfree_return": "7.5", "life_expectancy": "85"})
        self.assertEqual(r.status_code, 302)
        s = RetirementSettings.load()
        self.assertEqual(float(s.inflation_working), 7)
        self.assertEqual(s.life_expectancy, 85)

    def test_settings_page_rejects_garbage(self):
        self.client.post(reverse("goal_settings"), {
            "inflation_working": "abc", "inflation_retired": "6", "equity_return": "10",
            "taxable_fi_return": "6", "taxfree_return": "7", "life_expectancy": "90"})
        self.assertEqual(float(RetirementSettings.load().inflation_working), 6)

    def test_goals_page_links_settings(self):
        body = self.client.get(reverse("goals")).content.decode()
        self.assertIn(reverse("goal_settings"), body)

    def test_goal_detail_uses_saved_settings(self):
        plan = GoalPlan.objects.create(user=self.user, name="FI", kind="retirement",
                                       current_age=35, retire_age=50, monthly_expense=50_000)
        before = self.client.get(reverse("goal_detail", args=[plan.pk])).context["result"]
        s = RetirementSettings.load()
        s.life_expectancy = 80
        s.save()
        after = self.client.get(reverse("goal_detail", args=[plan.pk])).context["result"]
        self.assertLess(after.gross_corpus, before.gross_corpus)
        self.assertEqual(len(after.cash_flow), 30)


# -------------------------------------------------------- 5. under-26 path
class YoungEarnerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("young", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    def test_advice_math(self):
        adv = young_earner_advice(current_age=23, monthly_expense=30_000, annual_expense=12_000,
                                  years_to_retirement=27, net_corpus=50_000_000)
        self.assertEqual(adv["emergency_fund"], 186_000)  # 6 × (30k + 1k)
        self.assertEqual(adv["step_up_pct"], 10)
        self.assertEqual(adv["equity_pct"], 100)
        self.assertEqual(adv["revisit_in_years"], 3)
        self.assertGreater(adv["monthly_sip"], 0)

    def _plan(self, age):
        return GoalPlan.objects.create(user=self.user, name="FI", kind="retirement",
                                       current_age=age, retire_age=50, monthly_expense=30_000)

    def test_banner_shown_under_26(self):
        r = self.client.get(reverse("goal_detail", args=[self._plan(23).pk]))
        self.assertIsNotNone(r.context["young_advice"])
        self.assertContains(r, "Starting early")
        self.assertContains(r, "revisit")

    def test_no_banner_at_26_plus(self):
        r = self.client.get(reverse("goal_detail", args=[self._plan(30).pk]))
        self.assertIsNone(r.context["young_advice"])
        self.assertNotContains(r, "Starting early")


# ------------------------------------------------------ 4. income flooring
class FloorTests(TestCase):
    def test_floor_covers_share_of_first_year_expense(self):
        r = retirement_plan(**BASE, floor_pct=50)
        self.assertEqual(r.floor_monthly, round(r.first_year_monthly_expense * 0.5))
        self.assertAlmostEqual(r.cash_flow[0]["floor"], r.first_year_monthly_expense * 6, delta=12)

    def test_floor_grows_with_retirement_inflation(self):
        r = retirement_plan(**BASE, floor_pct=50)
        self.assertAlmostEqual(r.cash_flow[1]["floor"], r.cash_flow[0]["floor"] * 1.06, delta=2)

    def test_floor_shrinks_investment_corpus_but_costs_an_annuity(self):
        plain = retirement_plan(**BASE)
        floored = retirement_plan(**BASE, floor_pct=50)
        self.assertLess(floored.drawdown_corpus, plain.drawdown_corpus)
        self.assertGreater(floored.annuity_cost, 0)
        self.assertEqual(plain.annuity_cost, 0)
        self.assertEqual(floored.gross_corpus,
                         floored.drawdown_corpus + floored.annuity_cost + floored.emergency_corpus)

    def test_full_floor_needs_no_drawdown(self):
        r = retirement_plan(**BASE, floor_pct=100)
        self.assertEqual(r.drawdown_corpus, 0)


# --------------------------------------------------------------- form + view
class GoalGapViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("gapview", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    def test_form_has_new_fields(self):
        body = self.client.get(reverse("goal_create") + "?kind=retirement").content.decode()
        for name in ("rent_income", "pension_income", "other_income", "rent_escalation",
                     "pension_escalation", "other_escalation", "pension_floor_pct"):
            self.assertIn(f'name="{name}"', body)

    def test_post_saves_streams_and_floor_and_renders_cash_flow(self):
        r = self.client.post(reverse("goal_create") + "?kind=retirement", {
            "name": "FI", "kind": "retirement", "current_age": "35", "retire_age": "50",
            "monthly_expense": "50000", "annual_expense": "0",
            "current_equity": "2000000", "current_taxfree_fi": "0", "current_taxable_fi": "0",
            "lumpsum_expected": "0", "step_up_pct": "5",
            "rent_income": "15000", "rent_escalation": "5", "pension_income": "",
            "other_income": "", "pension_floor_pct": "30"})
        self.assertEqual(r.status_code, 302)
        plan = GoalPlan.objects.get(user=self.user)
        self.assertEqual(float(plan.rent_income), 15000)
        self.assertEqual(float(plan.rent_escalation), 5)
        self.assertIsNone(plan.pension_income)
        self.assertEqual(float(plan.pension_escalation), 0)
        self.assertEqual(float(plan.pension_floor_pct), 30)
        page = self.client.get(r.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Opening corpus")
        self.assertContains(page, "Guaranteed floor")
        self.assertContains(page, "High risk")
        res = page.context["result"]
        self.assertGreater(res.income_monthly_at_retirement, 0)
        # Existing investments now reach retirement_plan (they used to be dropped).
        self.assertGreater(res.corpus_in_hand, 0)

    def test_depletion_marker_rendered(self):
        plan = GoalPlan.objects.create(user=self.user, name="FI", kind="retirement",
                                       current_age=35, retire_age=50, monthly_expense=50_000,
                                       current_equity=2_000_000)
        page = self.client.get(reverse("goal_detail", args=[plan.pk]))
        self.assertContains(page, "money runs out at age")
