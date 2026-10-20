"""Tests for the last two workbook gaps: income streams with START/STOP years in
retirement (Step 2 rows 31–41) and the take-home-pay SIP affordability check
(Step 2 A24). Also the classic planner factors at the bottom of the file:
existing corpus, de-risking, lumpsum vs SIP, tax notes and the on-track page."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.goals import retirement_plan, sip_affordability, young_earner_advice
from core.models import GoalPlan

BASE = dict(current_age=35, retire_age=50, monthly_expense=50_000)


# ------------------------------------------------------------ spouse age gap
class SpouseAgeGapTests(TestCase):
    """Settings!C10 = C4 - MAX(age, retire age) + (older - younger spouse age)."""

    def test_spouse_gap_extends_plan(self):
        from core.goals import Assumptions, planning_period
        single = retirement_plan(**BASE)
        couple = retirement_plan(**BASE, a=Assumptions(spouse_age=40))  # 5 years older
        self.assertEqual(len(single.cash_flow), 40)
        self.assertEqual(len(couple.cash_flow), 45)
        self.assertGreater(couple.gross_corpus, single.gross_corpus)
        self.assertEqual(planning_period(35, 50, Assumptions(spouse_age=30)), 45)
        self.assertEqual(planning_period(35, 50, Assumptions(spouse_age=35)), 40)
        self.assertEqual(planning_period(70, 60, Assumptions()), 20)  # already retired

    def test_settings_spouse_age_reaches_assumptions(self):
        from core.models import RetirementSettings
        s = RetirementSettings.load()
        self.assertIsNone(s.to_assumptions().spouse_age)
        s.spouse_age = 40
        s.save()
        self.assertEqual(RetirementSettings.load().to_assumptions().spouse_age, 40)


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
        # Step 6B AE: the stream pays C32 (value at retirement) in its start
        # year, then escalates once a year while it pays.
        r = retirement_plan(**BASE, income_streams=[(10_000, 5, 3, 8)])
        at_ret = 10_000 * 12 * 1.05 ** 15
        self.assertEqual(r.cash_flow[2]["income"], 0)
        self.assertAlmostEqual(r.cash_flow[3]["income"], at_ret, delta=1)
        self.assertAlmostEqual(r.cash_flow[7]["income"], at_ret * 1.05 ** 4, delta=1)
        self.assertEqual(r.cash_flow[8]["income"], 0)

    def test_deferred_stream_does_not_escalate_while_waiting(self):
        # Workbook: C32 = B32 * (1 + B33)^B7 (B7 = years to retirement) and AE = C32
        # in the start year. ₹1L a year today at 5%, retiring now, starting in
        # retirement year 5 (workbook F32 = 5, 0-based start 4) pays ₹1L that
        # year, not ₹1L * 1.05^4 = ₹1.2155L.
        r = retirement_plan(current_age=60, retire_age=60, monthly_expense=100_000,
                            income_streams=[(100_000 / 12, 5, 4, None)])
        self.assertEqual(r.cash_flow[3]["income"], 0)
        self.assertAlmostEqual(r.cash_flow[4]["income"], 100_000, delta=1)
        self.assertAlmostEqual(r.cash_flow[5]["income"], 105_000, delta=1)
        # With years to retirement, only that span escalates the start amount.
        later = retirement_plan(**BASE, income_streams=[(100_000 / 12, 5, 4, None)])
        self.assertAlmostEqual(later.cash_flow[4]["income"], 100_000 * 1.05 ** 15, delta=1)

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


# ================================================================
# Classic Freefincal planner factors (a)–(f) on one-time / recurring goals:
# existing corpus over the full horizon, de-risking before the goal,
# lumpsum-vs-SIP as separate answers, post-goal return, and tax notes.
# ================================================================
from datetime import date  # noqa: E402
from unittest import mock  # noqa: E402

from core.goals import (Assumptions, assumption_notes, goal_progress, goal_scenarios,  # noqa: E402
                        lumpsum_growth, one_time_goal, recent_monthly_investing,
                        recurring_goal)

CAR = dict(cost_today=1_000_000, years_to_goal=10, inflation_override=10.0)


class ExistingCorpusTests(TestCase):
    def test_existing_corpus_compounds_from_sip_start_like_workbook(self):
        # Step 4A B19 = value * (1 + r)^(B4 - B5): 5L equity, goal in 10 years,
        # SIP from year 4 -> 5L * 1.10^6 = 8.86L, not 5L * 1.10^10 = 12.97L.
        r = one_time_goal(**CAR, years_to_start=4, current_equity=500_000)
        self.assertEqual(r.fv_current, round(500_000 * 1.10 ** 6))
        self.assertEqual(r.fv_current, 885_781)
        fi = one_time_goal(**CAR, years_to_start=4, current_taxable_fi=100_000,
                           current_taxfree_fi=100_000)
        self.assertEqual(fi.fv_current, round(100_000 * 1.06 ** 6 + 100_000 * 1.07 ** 6))
        now = one_time_goal(**CAR, current_equity=500_000)
        self.assertEqual(now.fv_current, 1_296_871)  # SIP starts now: full 10 years

    def test_existing_corpus_reduces_sip_and_lumpsum(self):
        bare = one_time_goal(**CAR)
        held = one_time_goal(**CAR, current_equity=300_000)
        self.assertLess(held.monthly_sip, bare.monthly_sip)
        self.assertLess(held.lumpsum_now, bare.lumpsum_now)

    def test_no_sip_window_does_not_crash(self):
        r = one_time_goal(**CAR, years_to_start=10)
        self.assertEqual(r.monthly_sip, 0)
        self.assertEqual(r.schedule, [])
        self.assertGreater(r.lumpsum_now, 0)


class GlidePathTests(TestCase):
    """Settings rows 45-48 / Step 4A column F: equity glides from E45 to G45."""

    def test_glide_path_matches_workbook_schedule(self):
        from core.goals import glide_path
        r = lambda p: [round(w, 4) for w in p]  # noqa: E731
        self.assertEqual(r(glide_path(False, 3)), [0, 0, 0])
        self.assertEqual(r(glide_path(True, 3)), [0.1, 0.1, 0.1])
        self.assertEqual(r(glide_path(True, 10)),
                         [0.4] * 4 + [0.3667, 0.3333, 0.3, 0.2667, 0.2333, 0.2])
        p20 = glide_path(False, 20)  # 60% for 8 years, to 30% over 6, to 0% over 6
        self.assertEqual(r(p20[:8]), [0.6] * 8)
        self.assertAlmostEqual(p20[13], 0.3)
        self.assertAlmostEqual(p20[-1], 0.0)
        self.assertAlmostEqual(glide_path(True, 25)[-1], 0.2)

    def test_sip_follows_glide_path_and_tracks_workbook(self):
        # Workbook goal seek: J = (12 * I + J_prev) * (1 + H) each year, H from the glide.
        def workbook_sip(net, flexible, n):
            from core.goals import glide_path
            j, i = 0.0, 1.0
            for w in glide_path(flexible, n):
                j = (12 * i + j) * (1 + w * 0.10 + (1 - w) * 0.06)
                i *= 1.05
            return net / j
        for flexible, n in ((False, 3), (True, 10), (False, 20)):
            r = one_time_goal(cost_today=1_000_000, years_to_goal=n, flexible=flexible)
            wb = workbook_sip(r.net_corpus, flexible, n)
            # Same annual schedule as the workbook: equal up to rounding to the rupee.
            self.assertAlmostEqual(r.monthly_sip, wb, delta=0.5)
        # Audit case: ₹20L in 10 years, rigid: workbook SIP ₹24,435, lump sum ₹26.9L.
        audit = one_time_goal(cost_today=2_000_000, years_to_goal=10, step_up_pct=5)
        self.assertEqual(audit.monthly_sip, 24_435)
        self.assertEqual(audit.lumpsum_now, 2_692_114)
        flex = one_time_goal(cost_today=1_000_000, years_to_goal=10, flexible=True)
        self.assertEqual(flex.equity_alloc_pct, 40)
        self.assertAlmostEqual(flex.year_returns[-1], 6.8)


class LumpsumVsSipTests(TestCase):
    def test_lumpsum_alone_reaches_net_corpus(self):
        r = one_time_goal(**CAR)
        grown = r.lumpsum_now * lumpsum_growth(r.pre_goal_return, 120)
        self.assertLess(abs(grown - r.net_corpus) / r.net_corpus, 0.001)

    def test_lumpsum_is_pv_not_a_quarter_of_corpus(self):
        r = one_time_goal(**CAR)
        self.assertNotEqual(r.lumpsum_now, round(r.net_corpus * 0.25))
        self.assertLess(r.lumpsum_now, r.net_corpus)

    def test_retirement_has_lumpsum_alternative(self):
        r = retirement_plan(**BASE)
        grown = r.lumpsum_now * lumpsum_growth(r.pre_goal_return, 15 * 12)
        self.assertLess(abs(grown - r.net_corpus) / r.net_corpus, 0.001)


class DeriskTests(TestCase):
    def test_derisk_raises_sip_and_hits_corpus(self):
        base = one_time_goal(**CAR)
        safe = one_time_goal(**CAR, derisk_years=3)
        self.assertGreater(safe.monthly_sip, base.monthly_sip)
        bal = 0.0
        for y in range(10):  # annual schedule: (12·SIP + balance)·(1 + r)
            rate = (safe.year_returns[y] if y < 7 else 6.0) / 100
            bal = (12 * safe.monthly_sip * 1.05 ** y + bal) * (1 + rate)
        self.assertLess(abs(bal - safe.net_corpus) / safe.net_corpus, 0.001)

    def test_derisk_moves_existing_equity_to_fi(self):
        r = one_time_goal(**CAR, current_equity=500_000, derisk_years=3)
        self.assertEqual(r.fv_current, round(500_000 * 1.10 ** 7 * 1.06 ** 3))

    def test_recurring_post_goal_return_overrides_fund_rate(self):
        kw = dict(annual_cost_today=200_000, years_until_start=8, years_duration=4)
        same = recurring_goal(**kw)
        safe = recurring_goal(**kw, post_goal_return=5.0)  # below the 7% RD rate
        self.assertEqual(same.post_goal_return, same.pre_goal_return)
        self.assertEqual(safe.pre_goal_return, 5.0)
        self.assertEqual(safe.future_cost, same.future_cost)
        self.assertGreater(safe.monthly_sip, same.monthly_sip)
        self.assertGreater(safe.occurrences[0]["monthly_sip"], same.occurrences[0]["monthly_sip"])
        # A 1-year fund credited at year end earns nothing: G = F / 12 at any rate.
        self.assertEqual(safe.occurrences[1]["monthly_sip"], same.occurrences[1]["monthly_sip"])

    def test_recurring_matches_workbook_sinking_funds(self):
        # Step 4B G = F·(r − g) / (12·((1 + r)^n − (1 + g)^n)), r = 7%, g = 5% step-up,
        # n = 5 for the first payment and 1 (the interval) after that.
        r = recurring_goal(annual_cost_today=200_000, years_until_start=5, years_duration=4,
                           step_up_pct=5)
        self.assertEqual([o["year"] for o in r.occurrences], [5, 6, 7, 8])
        self.assertEqual([o["amount"] for o in r.occurrences],
                         [322_102, 354_312, 389_743, 428_718])
        self.assertEqual([o["monthly_sip"] for o in r.occurrences], [4_251, 29_526, 32_479, 35_726])
        self.assertEqual(r.monthly_sip, 4_251)
        self.assertEqual(r.future_cost, 322_102)
        # Every two years, three times: n = 2 for the later payments.
        two = recurring_goal(annual_cost_today=200_000, years_until_start=5, years_duration=3,
                             every_years=2, step_up_pct=5)
        self.assertEqual([(o["year"], o["invest_from"]) for o in two.occurrences],
                         [(5, 0), (7, 5), (9, 7)])
        f = 200_000 * 1.1 ** 7
        self.assertAlmostEqual(two.occurrences[1]["monthly_sip"],
                               f * 0.02 / (12 * (1.07 ** 2 - 1.05 ** 2)), delta=0.5)

    def test_recurring_each_fund_accumulates_its_payment(self):
        r = recurring_goal(annual_cost_today=200_000, years_until_start=8, years_duration=4,
                           inflation_override=8.0)
        for o in r.occurrences:
            bal = 0.0
            for k in range(o["invest_years"]):  # instalments credited at year end
                bal = bal * 1.07 + 12 * o["monthly_sip"] * 1.05 ** k
            self.assertAlmostEqual(bal, 200_000 * 1.08 ** o["year"], delta=12 * o["invest_years"])
        self.assertEqual(r.schedule[-1][0], 11)  # investing runs to the last payment

    def test_recurring_existing_money_pays_first_payments(self):
        r = recurring_goal(annual_cost_today=200_000, years_until_start=5, years_duration=4,
                           current_taxable_fi=400_000)
        grown = 400_000 * 1.06 ** 5
        self.assertEqual(r.fv_current, round(grown))
        self.assertEqual(r.net_corpus, 0)
        self.assertEqual(r.monthly_sip, 0)
        left = (grown - 322_102) * 1.07
        self.assertAlmostEqual(r.occurrences[1]["net"], 354_312 - left, delta=1)

    def test_recurring_uses_the_rd_rate_not_equity(self):
        # Workbook 4B: recurring goals are saved in an RD / liquid fund at
        # Settings!C37 (7%), never in the equity-blended goal portfolio.
        r = recurring_goal(annual_cost_today=200_000, years_until_start=8, years_duration=4)
        self.assertEqual(r.pre_goal_return, Assumptions().recurring_return)
        self.assertEqual(r.post_goal_return, 7.0)
        self.assertEqual(r.equity_alloc_pct, 0)

    def test_scenarios_offer_derisk_for_goals_only(self):
        one = {s["label"]: s for s in goal_scenarios(kind="one_time", **CAR)}
        self.assertGreater(one["De-risk 3y"]["monthly_sip"], one["Base"]["monthly_sip"])
        rec = [s["label"] for s in goal_scenarios(kind="recurring", annual_cost_today=1e5,
                                                  years_until_start=5, years_duration=4)]
        self.assertIn("De-risk 3y", rec)
        self.assertEqual(rec.count("Base"), 1)
        ret = [s["label"] for s in goal_scenarios(kind="retirement", current_age=40,
                                                  retire_age=55, monthly_expense=50_000)]
        self.assertNotIn("De-risk 3y", ret)
        self.assertIn("Retire at 58", ret)


class AssumptionNotesTests(TestCase):
    def test_tax_note_always_present_and_defaults_are_realistic(self):
        notes = assumption_notes(Assumptions(), kind="one_time", goal_inflation=10)
        self.assertEqual([n["tone"] for n in notes], ["info"])
        self.assertIn("12.5%", notes[0]["text"])
        self.assertIn("₹1.25 lakh", notes[0]["text"])

    def test_optimistic_inputs_warn(self):
        text = " ".join(n["text"] for n in assumption_notes(
            Assumptions(equity_return=14, fixed_return=8, taxfree_return=7),
            kind="one_time", goal_inflation=5))
        self.assertIn("optimistic", text)
        self.assertIn("Real returns", text)
        self.assertIn("pre-tax", text)
        self.assertIn("below general inflation", text)


class GoalProgressTests(TestCase):
    def _plan(self, **kw):
        base = dict(name="Car", kind="one_time", cost_today=1_000_000, years_to_goal=10,
                    inflation_override=10)
        return GoalPlan(**{**base, **kw})

    def _run(self, plan, **kw):
        r = one_time_goal(cost_today=float(plan.cost_today), years_to_goal=plan.years_to_goal,
                          inflation_override=10.0, current_equity=float(plan.current_equity))
        return r, goal_progress(plan, r, Assumptions(), start_year=2026, **kw)

    def test_shortfall_without_sip_equals_net_corpus(self):
        r, p = self._run(self._plan(current_equity=200_000))
        self.assertFalse(p["on_track"])
        self.assertAlmostEqual(-p["gap"], r.net_corpus, delta=2)
        self.assertEqual(p["target"], r.future_cost)
        self.assertIsNone(p["current_sip"])

    def test_paying_required_sip_lands_on_target(self):
        r, p = self._run(self._plan(), current_sip=None)
        _, p2 = self._run(self._plan(), current_sip=r.monthly_sip)
        self.assertLess(abs(p2["projected"] - p2["target"]) / p2["target"], 0.002)
        self.assertEqual(p2["sip_gap"], 0)

    def test_big_corpus_is_on_track_with_surplus(self):
        _, p = self._run(self._plan(current_equity=5_000_000))
        self.assertTrue(p["on_track"])
        self.assertGreater(p["gap"], 0)
        self.assertEqual(p["funded_pct"], 100)

    def test_chart_has_one_point_per_year(self):
        import json
        _, p = self._run(self._plan())
        c = json.loads(p["chart"])
        self.assertEqual(len(c["labels"]), 11)
        self.assertEqual(c["labels"][0], "2026")
        self.assertEqual(len(c["required"]), 11)
        self.assertAlmostEqual(c["required"][-1], c["target"], delta=c["target"] * 0.002)

    def test_retirement_and_recurring_progress(self):
        plan = GoalPlan(name="FI", kind="retirement", current_age=35, retire_age=50,
                        monthly_expense=50_000, current_equity=1_000_000)
        r = retirement_plan(**BASE, current_equity=1_000_000)
        p = goal_progress(plan, r, Assumptions())
        self.assertEqual(p["target"], r.gross_corpus)
        self.assertAlmostEqual(p["projected"], r.corpus_in_hand, delta=2)
        self.assertIn("bucket ladder", dict(p["assumptions"])["Return after the goal"])
        rec_plan = GoalPlan(name="School", kind="recurring", annual_cost=200_000,
                            years_until_start=6, years_duration=4)
        rr = recurring_goal(annual_cost_today=200_000, years_until_start=6, years_duration=4)
        rp = goal_progress(rec_plan, rr, Assumptions(), start_year=2026)
        self.assertEqual(rp["goal_label"], "the first payment in 2032")
        self.assertEqual(rp["years_left"], 6)

    def test_recent_monthly_investing(self):
        today = date(2026, 10, 1)
        buys = [(date(2026, 9, 5), 10_000), (date(2026, 8, 5), 20_000),
                (date(2025, 1, 5), 99_999)]
        self.assertEqual(recent_monthly_investing(buys, today=today), 5_000)
        self.assertIsNone(recent_monthly_investing([], today=today))


class GoalDetailPageTests(TestCase):
    """The redesigned page renders the on-track headline, assumptions and chart
    once the view passes goal_progress() as `progress`."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("progress", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, plan, current_sip=None):
        from core import views
        real = views._compute

        def with_progress(p):
            ctx = real(p)
            ctx["progress"] = goal_progress(p, ctx["result"], ctx.get("assumptions") or Assumptions(),
                                            current_sip=current_sip)
            return ctx
        with mock.patch.object(views, "_compute", with_progress):
            return self.client.get(reverse("goal_detail", args=[plan.pk]))

    def test_shortfall_headline_and_chart(self):
        plan = GoalPlan.objects.create(user=self.user, name="Car", kind="one_time",
                                       cost_today=1_000_000, years_to_goal=8)
        page = self._get(plan, current_sip=2_000)
        self.assertContains(page, "Shortfall")
        self.assertContains(page, "Your current SIP")
        self.assertContains(page, 'id="goalchart"')
        self.assertContains(page, "Return after the goal")
        self.assertContains(page, "after tax")

    def test_on_track_headline(self):
        plan = GoalPlan.objects.create(user=self.user, name="Car", kind="one_time",
                                       cost_today=100_000, years_to_goal=5,
                                       current_equity=1_000_000)
        page = self._get(plan)
        self.assertContains(page, "On track")
        self.assertContains(page, "Surplus")

    def test_retirement_page_still_renders(self):
        plan = GoalPlan.objects.create(user=self.user, name="FI", kind="retirement",
                                       current_age=35, retire_age=50, monthly_expense=50_000)
        page = self._get(plan)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Opening corpus")
        self.assertContains(page, 'id="goalchart"')
