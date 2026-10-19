"""Goal engine + goal view tests (Freefincal robo-advisor port)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.goals import one_time_goal, recurring_goal, retirement_plan, goal_scenarios


class EngineMathTests(TestCase):
    def test_one_time_future_cost_and_fv(self):
        r = one_time_goal(cost_today=1_000_000, years_to_goal=10, inflation_override=10.0,
                          current_equity=500_000)
        self.assertEqual(r.future_cost, 2_593_742)   # 10L * 1.1^10
        self.assertEqual(r.fv_current, 1_296_871)    # 5L * 1.10^10
        self.assertEqual(r.net_corpus, 1_296_871)

    def test_one_time_sip_reaches_corpus(self):
        r = one_time_goal(cost_today=1_000_000, years_to_goal=10, inflation_override=10.0,
                          current_equity=500_000)
        bal, m, rr = 0.0, r.monthly_sip, (0.4 * 10 + 0.6 * 6) / 100 / 12
        for i in range(120):
            if i and i % 12 == 0:
                m *= 1.05
            bal = bal * (1 + rr) + m
        self.assertLess(abs(bal - r.net_corpus) / r.net_corpus, 0.001)

    def test_flexible_goal_takes_more_equity(self):
        rigid = one_time_goal(cost_today=1_000_000, years_to_goal=10, flexible=False)
        flex = one_time_goal(cost_today=1_000_000, years_to_goal=10, flexible=True)
        self.assertGreater(flex.equity_alloc_pct, rigid.equity_alloc_pct)

    def test_recurring_corpus_grows_with_years(self):
        short = recurring_goal(annual_cost_today=100_000, years_until_start=0, years_duration=6)
        long_ = recurring_goal(annual_cost_today=100_000, years_until_start=0, years_duration=12)
        self.assertGreater(long_.future_cost, short.future_cost)

    def test_retirement_first_year_expense(self):
        r = retirement_plan(current_age=35, retire_age=50, monthly_expense=50_000)
        self.assertEqual(r.first_year_monthly_expense, 119_828)  # 50k * 1.06^15

    def test_retirement_corpus_is_realistic(self):
        """6% inflation vs 5.5% return over 40y must produce crores, not lakhs."""
        r = retirement_plan(current_age=35, retire_age=50, monthly_expense=50_000)
        self.assertGreater(r.gross_corpus, 30_000_000)

    def test_retirement_on_track_with_big_corpus(self):
        r = retirement_plan(current_age=35, retire_age=50, monthly_expense=50_000,
                            current_equity=80_000_000)
        self.assertTrue(r.on_track)
        self.assertEqual(r.monthly_sip, 0)


class ScenarioTests(TestCase):
    def test_three_market_scenarios_present(self):
        r = goal_scenarios(kind="one_time", cost_today=1_000_000, years_to_goal=10)
        names = [s["label"] for s in r]
        self.assertIn("Bear", names)
        self.assertIn("Base", names)
        self.assertIn("Bull", names)

    def test_bear_needs_bigger_sip_than_bull(self):
        r = goal_scenarios(kind="one_time", cost_today=1_000_000, years_to_goal=10)
        sip = {s["label"]: s["monthly_sip"] for s in r}
        self.assertGreater(sip["Bear"], sip["Base"])
        self.assertGreater(sip["Base"], sip["Bull"])

    def test_retirement_late_retirement_needs_smaller_sip(self):
        r = goal_scenarios(kind="retirement", current_age=35, retire_age=50,
                           monthly_expense=50_000)
        sip = {s["label"]: s["monthly_sip"] for s in r}
        # Base case is retiring at 50; delaying to 53 cuts the SIP.
        self.assertLess(sip["Retire at 53"], sip["Base"])

    def test_higher_step_up_needs_smaller_initial_sip(self):
        r = goal_scenarios(kind="one_time", cost_today=1_000_000, years_to_goal=10)
        sip = {s["label"]: s["monthly_sip"] for s in r}
        self.assertLess(sip["10% step-up"], sip["Base"])


class GoalViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("goaluser", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    def test_retirement_goal_detail_renders(self):
        """Regression: _compute used to pass one-time-only kwargs to retirement_plan."""
        r = self.client.post(reverse("goal_create") + "?kind=retirement&prefill=portfolio", {
            "name": "FI", "kind": "retirement", "current_age": "35", "retire_age": "50",
            "monthly_expense": "50000", "annual_expense": "60000",
            "current_equity": "4000000", "current_taxfree_fi": "1697400",
            "current_taxable_fi": "500000", "lumpsum_expected": "0", "step_up_pct": "5"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(r.url).status_code, 200)

    def test_one_time_goal_shows_schedule(self):
        r = self.client.post(reverse("goal_create") + "?kind=one_time&prefill=portfolio", {
            "name": "Car", "kind": "one_time", "cost_today": "1500000", "years_to_goal": "5",
            "current_equity": "100000", "current_taxfree_fi": "0", "current_taxable_fi": "0",
            "lumpsum_expected": "0", "years_to_start": "0", "step_up_pct": "5"})
        self.assertEqual(self.client.get(r.url).status_code, 200)

    def test_goal_list_shows_scenarios(self):
        """Goal detail must include the scenario table."""
        r = self.client.post(reverse("goal_create") + "?kind=one_time&prefill=portfolio", {
            "name": "Car", "kind": "one_time", "cost_today": "1500000", "years_to_goal": "5",
            "current_equity": "100000", "current_taxfree_fi": "0", "current_taxable_fi": "0",
            "lumpsum_expected": "0", "years_to_start": "0", "step_up_pct": "5"})
        body = self.client.get(r.url).content.decode()
        self.assertIn("Scenarios", body)
        self.assertIn("Bear", body)
        self.assertIn("Bull", body)
