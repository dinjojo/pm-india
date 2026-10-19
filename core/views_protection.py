from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from core.models import InsurancePolicy, RetirementSettings


class PolicyForm(forms.ModelForm):
    class Meta:
        model = InsurancePolicy
        fields = ["policy_type", "insurer", "cover_amount", "annual_premium", "renewal_date"]
        widgets = {"renewal_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})}
        labels = {"cover_amount": "Cover amount (₹)", "annual_premium": "Annual premium (₹)"}


class ProtectionSettingsForm(forms.ModelForm):
    class Meta:
        model = RetirementSettings
        fields = ["protection_annual_income", "term_cover_multiple",
                  "protection_major_loans", "protection_liquid_assets"]
        labels = {"protection_annual_income": "Annual income (₹)",
                  "term_cover_multiple": "Income multiplier",
                  "protection_major_loans": "Outstanding major loans (₹)",
                  "protection_liquid_assets": "Liquid assets available to dependants (₹)"}


@login_required
def protection(request):
    policies = request.user.insurance_policies.all()
    assumptions = RetirementSettings.load()
    pk = request.POST.get("policy_id") if request.method == "POST" else request.GET.get("edit")
    policy = get_object_or_404(policies, pk=pk) if pk else None
    policy_form = PolicyForm(instance=policy, prefix="policy")
    settings_form = ProtectionSettingsForm(instance=assumptions, prefix="settings")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete" and policy:
            policy.delete()
            messages.success(request, "Policy removed.")
            return redirect("protection")
        if action == "policy":
            policy_form = PolicyForm(request.POST, instance=policy, prefix="policy")
            if policy_form.is_valid():
                saved = policy_form.save(commit=False)
                saved.user = request.user
                saved.save()
                messages.success(request, "Policy saved. Insurance stays separate from investments.")
                return redirect("protection")
        elif action == "settings":
            settings_form = ProtectionSettingsForm(request.POST, instance=assumptions, prefix="settings")
            if settings_form.is_valid():
                settings_form.save()
                messages.success(request, "Protection assumptions saved.")
                return redirect("protection")

    # Use persisted settings on validation errors, not an invalid bound instance.
    assumptions.refresh_from_db()
    today = date.today()
    policies = list(policies)
    annual_premium = sum((p.annual_premium for p in policies), Decimal(0))
    term_cover = sum((p.cover_amount for p in policies
                      if p.policy_type == InsurancePolicy.Type.TERM and p.renewal_date >= today), Decimal(0))
    recommended = shortfall = None
    if assumptions.protection_annual_income is not None:
        recommended = max(Decimal(0), assumptions.protection_annual_income * assumptions.term_cover_multiple
                          + assumptions.protection_major_loans - assumptions.protection_liquid_assets)
        shortfall = max(Decimal(0), recommended - term_cover)

    return render(request, "protection.html", {
        "policies": policies, "annual_premium": annual_premium,
        "next_renewal": policies[0] if policies else None,
        "term_cover": term_cover, "recommended": recommended, "shortfall": shortfall,
        "policy_form": policy_form, "settings_form": settings_form,
        "editing": policy, "today": today, "assumptions": assumptions,
    })
