"""Tests for the six goal-planner gaps vs the Freefincal workbook:
post-retirement income, cash-flow schedule, 4-bucket ladder, editable settings,
under-26 advice path, and income flooring (annuity)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.goals import (Assumptions, _blended, bucket_ladder, retirement_plan,
                        simulate_drawdown, young_earner_advice)
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
        # low stress buckets F17:F19 = ROUND(glide-path average, 3): each risk
        # bucket glides from its start equity to 0% over the years until it starts.
        b = bucket_ladder(40, Assumptions())
        self.assertAlmostEqual(b[0].return_pct, 6.0)    # income ladder: taxable FI (B13)
        self.assertAlmostEqual(b[1].return_pct, 7.8)    # 40% → 0% over 15 years
        self.assertAlmostEqual(b[2].return_pct, 8.6)    # 70% → 0% over 25 years
        self.assertAlmostEqual(b[3].return_pct, 9.3)    # 100% → 0% over 33 years

    def test_glide_returns_match_workbook_cached_values(self):
        # 'bucket backend'!Z2 / W2 / T2 cached values with workbook defaults
        # (T = 45: low starts in 15 years, medium in 26, high in 35).
        from core.goals import bucket_glide_return
        self.assertAlmostEqual(bucket_glide_return(15, 0.4), 0.07819213074036879, places=12)
        self.assertAlmostEqual(bucket_glide_return(26, 0.7), 0.08593157157394415, places=12)
        self.assertAlmostEqual(bucket_glide_return(35, 1.0), 0.0933191264488713, places=12)
        b = bucket_ladder(45, Assumptions())
        self.assertEqual([x.return_pct for x in b], [6.0, 7.8, 8.6, 9.3])

    def test_low_bucket_glide_capped_at_its_mix(self):
        # Workbook E146 starts the low bucket at 40% even when its mix C20 is
        # 20% (T <= 25); the intended rule caps the start at C20.
        from core.goals import bucket_glide_return
        b = bucket_ladder(20, Assumptions())
        self.assertAlmostEqual(b[1].return_pct, round(bucket_glide_return(15, 0.2), 3) * 100)
        self.assertLess(b[1].return_pct, round(bucket_glide_return(15, 0.4), 3) * 100)

    def test_bucket_corpus_is_workbook_present_value(self):
        # Audit case: age 35, spouse 30, retire 50, ₹50k/month. M6 / R6 / W6 / AB6:
        # each year laddered at 6% (income) or 5% inside its bucket, each risk
        # bucket discounted at its own return until it starts.
        r = retirement_plan(**BASE, a=Assumptions(spouse_age=30))
        self.assertEqual([b["allocation"] for b in r.buckets],
                         [21_569_024, 12_888_916, 7_160_694, 5_133_627])
        self.assertEqual(r.drawdown_corpus, 46_752_261)   # G25
        self.assertEqual(r.emergency_corpus, 2_337_613)   # F13 = 5% of G25
        self.assertEqual(r.gross_corpus, 49_089_874)      # F9
        self.assertEqual(r.post_ret_equity_pct, 35)       # D41
        # R6 = R5 / (1 + F17)^15, R5 = Σ need / 1.05^o over the low bucket's 11 years
        self.assertAlmostEqual(r.buckets[1]["allocation"],
                               sum(r.cash_flow[15 + o]["need"] / 1.05 ** o for o in range(11))
                               / 1.078 ** 15, delta=1)

    def test_emergency_is_five_percent_of_larger_of_corpus_or_investments(self):
        plain = retirement_plan(**BASE)
        self.assertEqual(plain.emergency_corpus, round(0.05 * plain.drawdown_corpus))
        rich = retirement_plan(**BASE, current_equity=80_000_000)
        self.assertGreater(rich.corpus_in_hand, rich.drawdown_corpus)
        self.assertEqual(rich.emergency_corpus, round(0.05 * rich.corpus_in_hand))

    def test_equity_return_now_matters_in_retirement(self):
        """The old blended 5.5% shortcut ignored equity returns post-retirement."""
        low = retirement_plan(**BASE, a=Assumptions(equity_return=8))
        high = retirement_plan(**BASE, a=Assumptions(equity_return=12))
        self.assertGreater(low.drawdown_corpus, high.drawdown_corpus)

    def test_annual_sip_reaches_net_corpus(self):
        # Step 3: N2 = 12·M2·(1+L2), N3 = (12·M3 + N2)·(1+L3), M3 = M2·(1+step),
        # with L the per-year pre-retirement glide rate (Step 3 H column).
        r = retirement_plan(**BASE)
        n, m = 0.0, r.monthly_sip
        for g, l in enumerate(r.pre_ret_glide):
            n = (12 * m * 1.05 ** g + n) * (1 + l / 100)
        self.assertLess(abs(n - r.net_corpus) / r.net_corpus, 1e-5)
        self.assertEqual(r.schedule[-1][0], 15)
        self.assertAlmostEqual(r.schedule[-1][2], n, delta=r.net_corpus * 1e-5)
        # pre_goal_return summarizes the glide; the glide starts at 60% equity
        # (Step 3 E25) and ends at the ladder's weighted equity share (D41).
        compound = 1.0
        for l in r.pre_ret_glide:
            compound *= 1 + l / 100
        self.assertAlmostEqual(r.pre_goal_return, (compound ** (1 / len(r.pre_ret_glide)) - 1) * 100,
                               places=6)
        self.assertAlmostEqual(r.pre_ret_glide[0],
                               _blended(0.60, Assumptions().equity_return,
                                        Assumptions().taxfree_return), delta=0.01)

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

    def test_floor_is_flat_nominal_priced_at_annuity_rate(self):
        # income flooring L51 is a flat amount every year; F12 = L51 / Settings!H7 (4%).
        r = retirement_plan(**BASE, floor_pct=50)
        self.assertEqual(r.cash_flow[1]["floor"], r.cash_flow[0]["floor"])
        self.assertEqual(r.cash_flow[-1]["floor"], r.cash_flow[0]["floor"])
        self.assertEqual(r.annuity_cost, round(r.cash_flow[0]["floor"] / 0.04))
        self.assertEqual(r.annuity_cost, 17_974_186)  # audit: 50% of 50k·12·1.06^15 / 4%

    def test_floor_covers_share_of_shortfall_after_income(self):
        # L51 = (year-1 expense − incomes) × B9
        r = retirement_plan(**BASE, floor_pct=50, income_streams=[(10_000, 0)])
        exp1 = r.cash_flow[0]["expense"]
        self.assertAlmostEqual(r.cash_flow[0]["floor"], (exp1 - 120_000) * 0.5, delta=1)

    def test_floor_shrinks_investment_corpus_but_costs_an_annuity(self):
        plain = retirement_plan(**BASE)
        floored = retirement_plan(**BASE, floor_pct=50)
        self.assertLess(floored.drawdown_corpus, plain.drawdown_corpus)
        self.assertGreater(floored.annuity_cost, 0)
        self.assertEqual(plain.annuity_cost, 0)
        self.assertEqual(floored.gross_corpus,
                         floored.drawdown_corpus + floored.annuity_cost + floored.emergency_corpus)

    def test_full_floor_leaves_only_expense_growth(self):
        # A flat pension of the whole first-year expense: the buckets fund only
        # the inflation growth above it.
        plain = retirement_plan(**BASE)
        r = retirement_plan(**BASE, floor_pct=100)
        exp1 = r.cash_flow[0]["expense"]
        self.assertEqual(r.cash_flow[0]["need"], 0)
        self.assertAlmostEqual(r.cash_flow[1]["need"], exp1 * 0.06, delta=1)
        self.assertGreater(r.drawdown_corpus, 0)
        self.assertLess(r.drawdown_corpus, plain.drawdown_corpus)


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
