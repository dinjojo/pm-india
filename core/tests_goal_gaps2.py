"""Tests for the last two workbook gaps: income streams with START/STOP years in
retirement (Step 2 rows 31–41) and the take-home-pay SIP affordability check
(Step 2 A24)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.goals import retirement_plan, sip_affordability, young_earner_advice
from core.models import GoalPlan

BASE = dict(current_age=35, retire_age=50, monthly_expense=50_000)


# ------------------------------------------------------------ income windows
class IncomeWindowTests(TestCase):
    def test_stream_active_only_within_window(self):
        # Flat rent from year 2 (included) to year 5 (excluded).
        r = retirement_plan(**BASE, income_streams=[(10_000, 0, 2, 5)])
        incomes = [row["income"] for row in r.cash_flow[:7]]
        self.assertEqual(incomes, [0, 0, 120_000, 120_000, 120_000, 0, 0])

    def test_open_ended_and_default_start(self):
        r = retirement_plan(**BASE, income_streams=[(10_000, 0, None, None)])
        self.assertTrue(all(row["income"] == 120_000 for row in r.cash_flow))
        late = retirement_plan(**BASE, income_streams=[(10_000, 0, 5, None)])
        self.assertEqual(late.cash_flow[4]["income"], 0)
        self.assertEqual(late.cash_flow[5]["income"], 120_000)
        self.assertEqual(late.cash_flow[-1]["income"], 120_000)

    def test_two_tuples_still_mean_whole_retirement(self):
        a = retirement_plan(**BASE, income_streams=[(10_000, 3)])
        b = retirement_plan(**BASE, income_streams=[(10_000, 3, 0, None)])
        self.assertEqual(a.gross_corpus, b.gross_corpus)

    def test_stream_grows_at_escalation_through_window(self):
        r = retirement_plan(**BASE, income_streams=[(10_000, 5, 3, 8)])
        at_ret = 10_000 * 12 * 1.05 ** 15
        self.assertEqual(r.cash_flow[2]["income"], 0)
        self.assertAlmostEqual(r.cash_flow[3]["income"], at_ret * 1.05 ** 3, delta=1)
        self.assertAlmostEqual(r.cash_flow[7]["income"], at_ret * 1.05 ** 7, delta=1)
        self.assertEqual(r.cash_flow[8]["income"], 0)

    def test_window_raises_corpus_vs_lifelong(self):
        life = retirement_plan(**BASE, income_streams=[(20_000, 0)])
        ten = retirement_plan(**BASE, income_streams=[(20_000, 0, 0, 10)])
        self.assertGreater(ten.gross_corpus, life.gross_corpus)
        self.assertGreater(ten.monthly_sip, life.monthly_sip)

    def test_deferred_stream_not_counted_at_retirement(self):
        r = retirement_plan(**BASE, income_streams=[(10_000, 0, 5, None)])
        self.assertEqual(r.income_monthly_at_retirement, 0)
        self.assertEqual(r.income_streams[0]["start"], 5)
        self.assertIsNone(r.income_streams[0]["stop"])
        self.assertEqual(r.income_streams[0]["monthly_at_retirement"], 10_000)

    def test_model_income_streams_returns_windows(self):
        plan = GoalPlan(rent_income=20_000, rent_escalation=5, rent_start=None, rent_stop=15,
                        pension_income=30_000, pension_start=5)
        self.assertEqual(plan.income_streams(),
                         [(20_000.0, 5.0, 0, 15), (30_000.0, 0.0, 5, None), (0.0, 0.0, 0, None)])


# -------------------------------------------------------------- affordability
class AffordabilityTests(TestCase):
    def test_bands_and_boundaries(self):
        self.assertIsNone(sip_affordability(10_000, None))
        self.assertIsNone(sip_affordability(10_000, 0))
        self.assertEqual(sip_affordability(24_400, 100_000)["band"], "comfortable")
        self.assertEqual(sip_affordability(25_000, 100_000)["band"], "stretch")
        self.assertEqual(sip_affordability(40_000, 100_000)["band"], "stretch")
        self.assertEqual(sip_affordability(40_400, 100_000)["band"], "stretch")  # shows 40%
        self.assertEqual(sip_affordability(40_600, 100_000)["band"], "aggressive")
        a = sip_affordability(30_000, 100_000)
        self.assertEqual(a["pct"], 30)
        self.assertIn("stretch but doable", a["note"])
        self.assertIn("comfortably affordable", sip_affordability(1, 100_000)["note"])
        self.assertIn("aggressive", sip_affordability(50_000, 100_000)["note"])

    def test_young_earner_gets_affordability(self):
        kw = dict(current_age=23, monthly_expense=30_000, years_to_retirement=27,
                  net_corpus=50_000_000)
        self.assertIsNone(young_earner_advice(**kw)["affordability"])
        adv = young_earner_advice(**kw, monthly_take_home=100_000)
        self.assertEqual(adv["affordability"]["pct"], round(adv["monthly_sip"] / 1000))


# ------------------------------------------------------------- form + views
class GoalGaps2ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("gaps2", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    POST = {"name": "FI", "kind": "retirement", "current_age": "35", "retire_age": "50",
            "monthly_expense": "50000", "annual_expense": "0", "current_equity": "0",
            "current_taxfree_fi": "0", "current_taxable_fi": "0", "lumpsum_expected": "0",
            "step_up_pct": "5", "rent_income": "20000", "rent_escalation": "0",
            "rent_start": "0", "rent_stop": "10", "pension_income": "30000",
            "pension_start": "5", "pension_stop": "", "other_income": "",
            "monthly_take_home": "150000"}

    def test_form_has_new_fields(self):
        body = self.client.get(reverse("goal_create") + "?kind=retirement").content.decode()
        for name in ("rent_start", "rent_stop", "pension_start", "pension_stop",
                     "other_start", "other_stop", "monthly_take_home"):
            self.assertIn(f'name="{name}"', body)

    def test_form_saves_new_fields_and_detail_shows_them(self):
        r = self.client.post(reverse("goal_create") + "?kind=retirement", self.POST)
        self.assertEqual(r.status_code, 302)
        plan = GoalPlan.objects.get(user=self.user)
        self.assertEqual((plan.rent_start, plan.rent_stop), (0, 10))
        self.assertEqual((plan.pension_start, plan.pension_stop), (5, None))
        self.assertEqual((plan.other_start, plan.other_stop), (None, None))
        self.assertEqual(float(plan.monthly_take_home), 150_000)
        page = self.client.get(r.url)
        self.assertContains(page, "years 0–10 in retirement")
        self.assertContains(page, "from year 5 in retirement")
        aff = page.context["affordability"]
        self.assertEqual(aff["pct"], round(page.context["result"].monthly_sip / 1500))
        self.assertContains(page, "of your monthly take-home")

    def test_stop_before_start_rejected(self):
        r = self.client.post(reverse("goal_create") + "?kind=retirement",
                             {**self.POST, "rent_start": "10", "rent_stop": "5"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(GoalPlan.objects.filter(user=self.user).exists())
        self.assertContains(r, "stop year")

    def test_affordability_hidden_without_take_home(self):
        plan = GoalPlan.objects.create(user=self.user, name="FI", kind="retirement",
                                       current_age=35, retire_age=50, monthly_expense=50_000)
        page = self.client.get(reverse("goal_detail", args=[plan.pk]))
        self.assertIsNone(page.context["affordability"])
        self.assertNotContains(page, "monthly take-home")

    def test_young_banner_shows_affordability(self):
        plan = GoalPlan.objects.create(user=self.user, name="FI", kind="retirement",
                                       current_age=23, retire_age=50, monthly_expense=30_000,
                                       monthly_take_home=80_000)
        page = self.client.get(reverse("goal_detail", args=[plan.pk]))
        self.assertIsNotNone(page.context["young_advice"]["affordability"])
        self.assertContains(page, "of your take-home")
