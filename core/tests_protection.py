from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import InsurancePolicy, RetirementSettings
from core.portfolio import portfolio_view


class ProtectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("protected")
        cls.other = get_user_model().objects.create_user("other")

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("protection")

    def policy(self, **kwargs):
        data = dict(user=self.user, policy_type="term", insurer="Example Life",
                    cover_amount=5000000, annual_premium=18000,
                    renewal_date=date.today() + timedelta(days=30))
        data.update(kwargs)
        return InsurancePolicy.objects.create(**data)

    def settings_post(self, **kwargs):
        data = {"action": "settings", "settings-protection_annual_income": "1000000",
                "settings-term_cover_multiple": "12", "settings-protection_major_loans": "3000000",
                "settings-protection_liquid_assets": "2000000"}
        data.update(kwargs)
        return self.client.post(self.url, data, follow=True)

    def test_auth_and_empty_state(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No policies recorded")
        self.assertContains(response, "Set your annual income")
        self.assertIsNone(response.context["recommended"])
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, {"action": "policy"}).status_code, 302)

    def test_create_edit_delete_policy_keeps_insurance_out_of_portfolio(self):
        payload = {"action": "policy", "policy-policy_type": "health",
                   "policy-insurer": "Example Health", "policy-cover_amount": "750000",
                   "policy-annual_premium": "25432.50", "policy-renewal_date": "2027-07-19"}
        response = self.client.post(self.url, payload, follow=True)
        self.assertContains(response, "Example Health")
        self.assertEqual(response.context["annual_premium"], Decimal("25432.50"))
        policy = InsurancePolicy.objects.get(user=self.user)
        self.assertEqual(portfolio_view(self.user).total_current, 0)
        self.assertEqual(self.client.get(self.url, {"edit": policy.pk}).status_code, 200)
        payload.update({"policy_id": policy.pk, "policy-annual_premium": "27000"})
        self.client.post(self.url, payload)
        policy.refresh_from_db()
        self.assertEqual(policy.annual_premium, 27000)
        self.client.post(self.url, {"action": "delete", "policy_id": policy.pk})
        self.assertFalse(InsurancePolicy.objects.exists())

    def test_adequacy_uses_only_current_term_cover_and_correct_signs(self):
        self.policy()
        self.policy(insurer="Term due today", cover_amount=2000000, renewal_date=date.today())
        overdue = self.policy(insurer="Overdue term", cover_amount=10000000,
                              renewal_date=date.today() - timedelta(days=1))
        self.policy(policy_type="health", cover_amount=50000000, annual_premium=23000)
        self.policy(user=self.other, cover_amount=80000000, annual_premium=99999)
        response = self.settings_post()
        # 12 * 10L + 30L - 20L = 130L; current term = 70L, not 670L.
        self.assertEqual(response.context["recommended"], 13000000)
        self.assertEqual(response.context["term_cover"], 7000000)
        self.assertEqual(response.context["shortfall"], 6000000)
        self.assertContains(response, "Add ₹60.00 L cover")
        self.assertEqual(response.context["annual_premium"], 77000)
        self.assertEqual(response.context["next_renewal"], overdue)
        self.assertContains(response, "Renewal overdue")

    def test_multiplier_choices_and_exact_adequacy_boundary(self):
        self.policy(cover_amount=11000000)
        for multiplier, shortfall in [(10, 0), (15, 5000000)]:
            response = self.settings_post(**{"settings-term_cover_multiple": str(multiplier)})
            self.assertEqual(response.context["shortfall"], shortfall)
            if shortfall == 0:
                self.assertContains(response, "Adequate")
        for invalid in ("9", "16", "12.5"):
            response = self.settings_post(**{"settings-term_cover_multiple": invalid})
            self.assertTrue(response.context["settings_form"].errors)
            self.assertEqual(RetirementSettings.load().term_cover_multiple, 15)
        response = self.settings_post(**{"settings-protection_liquid_assets": "90000000"})
        self.assertEqual(response.context["recommended"], 0)
        self.assertEqual(response.context["shortfall"], 0)

    def test_policy_and_settings_validation_does_not_persist_invalid_data(self):
        response = self.client.post(self.url, {"action": "policy", "policy-policy_type": "investment",
            "policy-insurer": "", "policy-cover_amount": "-100", "policy-annual_premium": "-1",
            "policy-renewal_date": "not-a-date"})
        self.assertTrue(response.context["policy_form"].errors)
        self.assertFalse(InsurancePolicy.objects.exists())
        response = self.settings_post(**{"settings-protection_annual_income": "-1"})
        self.assertTrue(response.context["settings_form"].errors)
        self.assertIsNone(response.context["recommended"])

    def test_cannot_read_edit_or_delete_another_users_policy(self):
        policy = self.policy(user=self.other, insurer="Private insurer")
        self.assertNotContains(self.client.get(self.url), "Private insurer")
        self.assertEqual(self.client.get(self.url, {"edit": policy.pk}).status_code, 404)
        for action in ("policy", "delete"):
            self.assertEqual(self.client.post(self.url, {"action": action, "policy_id": policy.pk}).status_code, 404)
        self.assertTrue(InsurancePolicy.objects.filter(pk=policy.pk).exists())
