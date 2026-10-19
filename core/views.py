import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import get_object_or_404, redirect, render

from core.importers.banks import BANK_PROVIDERS, import_bank_statement
from core.importers.brokers import PROVIDERS, import_holdings_csv
from core.importers.cas import import_cas_pdf
from core.funds import search_funds, scheme_detail
from core.models import Account, Asset, GoalPlan, Holding, RetirementSettings, Transaction
from core.portfolio import portfolio_view, refresh_holdings
from dataclasses import replace

from core.goals import (glide_text, one_time_goal, recurring_goal, retirement_plan, goal_scenarios,
                        sip_affordability, young_earner_advice, goal_progress,
                        recent_monthly_investing)
from core.report_grouping import holdings_report, tax_report
from core.dashboard_context import build_dashboard_context
from core.snapshots import backfill_snapshots, record_snapshot
from core.tax.gains import (
    FYTaxSummary,
    _fy_of,
    dividends_for_fy,
    harvesting_headroom,
    realized_gains,
    scheme_flows_for_fy,
    unrealized_lots,
    real_estate_whatif,
)

# Asset classes traded in units with a price feed vs balance-type schemes.
UNITS_TYPES = {Asset.AssetType.MUTUAL_FUND, Asset.AssetType.STOCK,
               Asset.AssetType.CRYPTO, Asset.AssetType.SGB, Asset.AssetType.GOLD}
BALANCE_TYPES = {t for t in Asset.AssetType.values} - UNITS_TYPES


def signup(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            login(request, form.save())
            return redirect("dashboard")
    return render(request, "signup.html", {"form": form})


@login_required
def dashboard(request):
    view = portfolio_view(request.user)
    snaps = request.user.value_snapshots.order_by("date")
    trend = {
        "dates": [s.date.isoformat() for s in snaps],
        "invested": [float(s.invested) for s in snaps],
        "current": [float(s.current) for s in snaps],
    }
    return render(request, "dashboard.html", {
        "pv": view,
        "trend": json.dumps(trend),
        "has_history": len(trend["dates"]) >= 2,
        "alloc_labels": json.dumps(list(view.allocation.keys())),
        "alloc_values": json.dumps(list(view.allocation.values())),
        **build_dashboard_context(request.user, view, snaps),
    })


@login_required
def holdings(request):
    view = portfolio_view(request.user)
    group = request.GET.get("group", "class")
    if group not in {"class", "account", "owner", "liquidity"}:
        group = "class"
    groups = view.grouped_holdings if group == "class" else view.grouped_by(group)
    choices = [("class", "Asset class"), ("account", "Account"),
               ("owner", "Owner"), ("liquidity", "Liquidity")]
    # Stacked allocation bar: top 8 classes by value, remainder as "Other".
    bar_colors = ["bg-emerald-600", "bg-sky-600", "bg-amber-500", "bg-violet-600",
                  "bg-rose-500", "bg-teal-600", "bg-orange-600", "bg-indigo-500"]
    breakdown = sorted(view.class_breakdown, key=lambda r: r["current"], reverse=True)
    alloc_bar = [{"label": r["label"], "current": r["current"], "weight": r["weight"],
                  "color": bar_colors[i % len(bar_colors)]}
                 for i, r in enumerate(breakdown[:8])]
    if len(breakdown) > 8:
        other = sum(r["current"] for r in breakdown[8:])
        alloc_bar.append({"label": "Other", "current": other,
                          "weight": float(other / view.total_current * 100) if view.total_current else 0.0,
                          "color": "bg-stone-400"})
    return render(request, "holdings.html",
                  {"pv": view, "groups": groups, "group": group, "group_choices": choices,
                   "alloc_bar": alloc_bar,
                   "hreport": holdings_report(view, groups,
                                              snaps=request.user.value_snapshots.order_by("date"))})


@login_required
def tax(request):
    summaries = realized_gains()
    # FY tabs should also cover years that have dividends or any activity now
    div_fys = {
        _fy_of(tx.date) for tx in Transaction.objects.filter(kind=Transaction.Kind.DIVIDEND)
    }
    by_fy = {s.fy: s for s in summaries}
    all_fys = sorted(set(by_fy) | div_fys | {_fy_of(date.today())})
    summaries = [by_fy.get(fy) or FYTaxSummary(fy=fy) for fy in all_fys]

    selected_fy = request.GET.get("fy") or (summaries[-1].fy if summaries else None)
    summary = next((s for s in summaries if s.fy == selected_fy), None)

    unrealized = unrealized_lots()
    st_unreal = sum((l.gain for l in unrealized if not l.is_long_term), Decimal(0))
    lt_unreal = sum((l.gain for l in unrealized if l.is_long_term), Decimal(0))
    harvest = {
        "headroom": harvesting_headroom(summary) if summary else Decimal("125000"),
        "lt_gains": [l for l in unrealized if l.is_long_term and l.gain > 0],
        "losses": [l for l in unrealized if l.gain < 0],
        "st_unreal": st_unreal,
        "lt_unreal": lt_unreal,
    }
    dividends = dividends_for_fy(selected_fy) if selected_fy else Decimal(0)
    schemes = scheme_flows_for_fy(request.user, selected_fy) if selected_fy else []
    hide_zero = request.GET.get("hide_zero", "1") != "0"
    zero_count = sum(1 for l in unrealized if l.gain == 0)

    # Real-estate sale what-if: run for every property holding at its current
    # (or invested) value so the tax page shows the impact of selling today.
    re_whatifs = []
    re_qs = (Holding.objects
             .filter(account__user=request.user, asset__asset_type=Asset.AssetType.REAL_ESTATE)
             .select_related("asset"))
    for h in re_qs:
        w = real_estate_whatif(h, h.current_value if h.current_value is not None else h.invested)
        if w:
            re_whatifs.append(w)

    # Progress bars: LTCG exemption headroom and 80C-style deduction usage.
    ltcg_cap = Decimal("125000")
    used_exemption = ltcg_cap - harvest["headroom"] if summary else Decimal(0)
    used_exemption = max(Decimal(0), min(used_exemption, ltcg_cap))
    exemption_pct = round(float(used_exemption / ltcg_cap * 100))
    ded_cap = Decimal("150000")
    deductions_used = sum((s["deposits"] for s in schemes), Decimal(0))
    deductions_left = max(Decimal(0), ded_cap - deductions_used)
    deduction_pct = min(100, round(float(deductions_used / ded_cap * 100))) if ded_cap else 0

    return render(request, "tax.html", {
        "summaries": summaries,
        "summary": summary,
        "unrealized": unrealized,
        "harvest": harvest,
        "dividends": dividends,
        "schemes": schemes,
        "re_whatifs": re_whatifs,
        "today": date.today(),
        "hide_zero": hide_zero,
        "zero_count": zero_count,
        "used_exemption": used_exemption,
        "exemption_pct": exemption_pct,
        "deductions_used": deductions_used,
        "deductions_left": deductions_left,
        "deduction_pct": deduction_pct,
        "treport": tax_report(summaries, summary, unrealized, hide_zero, user=request.user),
    })


@login_required
def funds(request):
    """Mutual fund explorer: HTMX live search over the AMFI scheme list."""
    q = request.GET.get("q", "")
    results = search_funds(q) if q else []
    # HTMX swaps only the results fragment; a full-page response would re-render
    # the autofocus input and yank the cursor back to an empty box.
    if request.headers.get("HX-Request"):
        return render(request, "partials/fund_results.html", {"q": q, "results": results})
    return render(request, "funds.html", {"q": q, "results": results})


@login_required
def fund_detail(request, code: int):
    scheme = scheme_detail(code)
    if scheme is None:
        messages.error(request, "Could not fetch that scheme — try again later.")
        return redirect("funds")
    return render(request, "fund_detail.html", {"s": scheme})


@login_required
def goals(request):
    plans = request.user.goal_plans.order_by("kind", "id")
    return render(request, "goals.html", {"plans": plans})


def _compute(plan) -> dict:
    settings = RetirementSettings.load()
    a = settings.to_assumptions()
    equity_kw = dict(
        step_up_pct=float(plan.step_up_pct),
        current_equity=float(plan.current_equity),
        current_taxable_fi=float(plan.current_taxable_fi),
        current_taxfree_fi=float(plan.current_taxfree_fi),
        inflation_override=float(plan.inflation_override) if plan.inflation_override else None,
    )
    if plan.kind == plan.Kind.ONE_TIME:
        kw = dict(cost_today=float(plan.cost_today or 0),
                  years_to_goal=float(plan.years_to_goal or 0),
                  years_to_start=float(plan.years_to_start),
                  lumpsum_expected=float(plan.lumpsum_expected),
                  flexible=plan.flexible, **equity_kw)
        r = one_time_goal(a=a, **kw)
        scenarios = goal_scenarios(kind="one_time", a=a, **kw)
        return {"result": r, "scenarios": scenarios, "young_advice": None,
                "affordability": sip_affordability(r.monthly_sip, plan.monthly_take_home),
                "rows": [("Cost if bought today", f"₹{plan.cost_today:,.0f}"),
                                      ("Cost in the goal year", f"₹{r.future_cost:,.0f}"),
                                      ("Your earmarked money by then", f"₹{r.fv_current:,.0f}"),
                                      ("Corpus still needed", f"₹{r.net_corpus:,.0f}"),
                                      ("Suggested equity share", glide_text(r).replace("equity ", "", 1)
                                       + (" (glide path)" if len(set(r.glide_path)) > 1 else ""))]}
    if plan.kind == plan.Kind.RECURRING:
        kw = dict(annual_cost_today=float(plan.annual_cost or 0),
                  years_until_start=float(plan.years_until_start or 0),
                  years_duration=float(plan.years_duration or 0),
                  flexible=plan.flexible, **equity_kw)
        r = recurring_goal(a=a, **kw)
        scenarios = goal_scenarios(kind="recurring", a=a, **kw)
        return {"result": r, "scenarios": scenarios, "young_advice": None,
                "affordability": sip_affordability(r.monthly_sip, plan.monthly_take_home),
                "rows": [("Corpus needed at goal start", f"₹{r.future_cost:,.0f}"),
                                      ("Your earmarked money by then", f"₹{r.fv_current:,.0f}"),
                                      ("Corpus still needed", f"₹{r.net_corpus:,.0f}"),
                                      ("Suggested equity share", f"{r.equity_alloc_pct}%")]}
    # Retirement: saved settings are the defaults; the goal's inflation field overrides both
    # pre- and post-retirement inflation for this plan only.
    if plan.inflation_override:
        ov = float(plan.inflation_override)
        a = replace(a, inflation=ov, inflation_retirement=ov)
    kw = dict(current_age=plan.current_age or 0, retire_age=plan.retire_age or 0,
              monthly_expense=float(plan.monthly_expense or 0),
              annual_expense=float(plan.annual_expense or 0),
              lumpsum_at_retirement=float(plan.lumpsum_expected),
              step_up_pct=float(plan.step_up_pct),
              current_equity=float(plan.current_equity),
              current_taxable_fi=float(plan.current_taxable_fi),
              current_taxfree_fi=float(plan.current_taxfree_fi),
              income_streams=plan.income_streams(),
              floor_pct=float(plan.pension_floor_pct or 0))
    r = retirement_plan(a=a, **kw)
    scenarios = goal_scenarios(kind="retirement", a=a, **kw)
    young = None
    if plan.current_age and plan.current_age < 26:
        young = young_earner_advice(current_age=plan.current_age,
                                    monthly_expense=float(plan.monthly_expense or 0),
                                    annual_expense=float(plan.annual_expense or 0),
                                    years_to_retirement=r.years_to_retirement,
                                    net_corpus=r.net_corpus,
                                    monthly_take_home=plan.monthly_take_home, a=a)
    rows = [("Years to retirement", str(r.years_to_retirement)),
            ("Monthly expense in the first retirement year", f"₹{r.first_year_monthly_expense:,.0f}")]
    for (key, label), st in zip(INCOME_FIELDS, r.income_streams):
        if not st["monthly_at_retirement"]:
            continue
        window = (f"years {st['start']}–{st['stop']} in retirement" if st["stop"] is not None
                  else f"from year {st['start']} in retirement" if st["start"] else "for life")
        rows.append((f"{label} — ₹/month at retirement, {window}",
                     f"₹{st['monthly_at_retirement']:,.0f}"))
    if r.floor_monthly:
        rows += [(f"Guaranteed floor — annuity covering {r.floor_pct:g}% (monthly, inflation-indexed)",
                  f"₹{r.floor_monthly:,.0f}"),
                 ("Estimated annuity purchase price at retirement", f"₹{r.annuity_cost:,.0f}")]
    rows += [("Bucket corpus for withdrawals", f"₹{r.drawdown_corpus:,.0f}"),
             ("Emergency corpus (6 months)", f"₹{r.emergency_corpus:,.0f}"),
             ("Gross corpus needed", f"₹{r.gross_corpus:,.0f}"),
             ("Your investments grown to retirement", f"₹{r.corpus_in_hand:,.0f}"),
             ("Corpus still needed", f"₹{r.net_corpus:,.0f}"),
             ("Equity share of the bucket ladder at retirement", f"{r.post_ret_equity_pct}%")]
    return {"result": r, "scenarios": scenarios, "young_advice": young, "rows": rows,
            "settings": settings, "assumptions": a,
            "affordability": sip_affordability(r.monthly_sip, plan.monthly_take_home)}


@login_required
def goal_detail(request, pk):
    plan = get_object_or_404(GoalPlan, pk=pk, user=request.user)
    ctx = _compute(plan)
    r = ctx["result"]
    # Funding progress + drawdown chart are retirement-only (GoalResult lacks them).
    if hasattr(r, "gross_corpus"):
        funding_pct = 0
        if r.gross_corpus:
            funding_pct = min(100, round(max(0.0, r.corpus_in_hand) / r.gross_corpus * 100))
        cash_chart = {
            "ages": [row["age"] for row in r.cash_flow],
            "closing": [float(row["closing"]) for row in r.cash_flow],
            "depletion_age": r.current_path_depletion_age,
            "opening": float(r.cash_flow[0]["opening"]) if r.cash_flow else 0,
        }
        ctx.update(funding_pct=funding_pct, cash_chart=json.dumps(cash_chart))
    if ctx.get("scenarios"):
        ctx["max_sip"] = max(s["monthly_sip"] for s in ctx["scenarios"])
    if getattr(r, "buckets", None):
        ctx["max_bucket"] = max(b["allocation"] for b in r.buckets)
    # Tracked portfolio history (the same snapshots that drive the dashboard trend).
    snaps = request.user.value_snapshots.order_by("date")
    snap_trend = {
        "dates": [s.date.isoformat() for s in snaps],
        "invested": [float(s.invested) for s in snaps],
        "current": [float(s.current) for s in snaps],
    }
    ctx.update(snap_trend=json.dumps(snap_trend),
               has_snap_history=len(snap_trend["dates"]) >= 2)
    buys = Transaction.objects.filter(account__user=request.user, kind="buy") \
        .values_list("date", "amount")
    ctx["progress"] = goal_progress(
        plan, r, ctx.get("assumptions"),
        current_sip=recent_monthly_investing(buys))
    return render(request, "goal_detail.html", {"plan": plan, **ctx})


SETTINGS_FIELDS = [
    ("inflation_working", "Inflation before retirement %", 0, 20),
    ("inflation_retired", "Inflation during retirement %", 0, 20),
    ("equity_return", "Equity return after tax %", 0, 25),
    ("taxable_fi_return", "Taxable fixed income return after tax % (FD, debt funds)", 0, 20),
    ("taxfree_return", "Tax-free fixed income return % (PPF / EPF)", 0, 20),
    ("life_expectancy", "Plan income until age", 60, 110),
]


@login_required
def goal_settings(request):
    settings = RetirementSettings.load()
    if request.method == "POST":
        values, errors = {}, []
        for name, label, lo, hi in SETTINGS_FIELDS:
            try:
                v = Decimal(request.POST.get(name, ""))
                if not (lo <= v <= hi):
                    raise ValueError
            except (ArithmeticError, ValueError):
                errors.append(f"{label}: enter a number between {lo} and {hi}.")
                continue
            values[name] = int(v) if name == "life_expectancy" else v
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            for k, v in values.items():
                setattr(settings, k, v)
            settings.save()
            messages.success(request, "Assumptions saved — every plan now uses them.")
            return redirect("goal_settings")
    fields = [(name, label, getattr(settings, name), lo, hi) for name, label, lo, hi in SETTINGS_FIELDS]
    return render(request, "goal_settings.html", {"fields": fields})


INCOME_FIELDS = [("rent", "Rent"), ("pension", "Pension"), ("other", "Other income")]


@login_required
def goal_create(request):
    prefill = {}
    if request.GET.get("prefill") == "portfolio":
        view = portfolio_view(request.user)
        equity = fi_taxable = fi_taxfree = Decimal(0)
        for h in view.holdings:
            if h.asset_type in Asset.EQUITY_LIKE:
                equity += h.current
            elif h.asset_type in {Asset.AssetType.PPF, Asset.AssetType.EPF, Asset.AssetType.NPS}:
                fi_taxfree += h.current
            elif h.asset_type in {Asset.AssetType.FD, Asset.AssetType.BOND}:
                fi_taxable += h.current
        prefill = {"current_equity": equity, "current_taxable_fi": fi_taxable,
                   "current_taxfree_fi": fi_taxfree}
    kind = request.GET.get("kind", "one_time")
    if request.method == "POST":
        d = {k: (request.POST.get(k) or None) for k in
             ["cost_today", "years_to_goal", "annual_cost", "years_duration", "years_until_start",
              "current_age", "retire_age", "monthly_expense", "annual_expense",
              "inflation_override", "current_equity", "current_taxable_fi",
              "current_taxfree_fi", "lumpsum_expected", "step_up_pct",
              "rent_income", "pension_income", "other_income",
              "rent_escalation", "pension_escalation", "other_escalation", "pension_floor_pct",
              "monthly_take_home"]}
        windows, errors = {}, []
        for key, label in INCOME_FIELDS:
            for end in ("start", "stop"):
                v = (request.POST.get(f"{key}_{end}") or "").strip()
                try:
                    windows[f"{key}_{end}"] = int(v) if v else None
                    if v and int(v) < 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"{label}: {end} year must be a whole number of years, 0 or more.")
                    windows[f"{key}_{end}"] = None
            start, stop = windows[f"{key}_start"], windows[f"{key}_stop"]
            if start is not None and stop is not None and stop < start:
                errors.append(f"{label}: the stop year can't be before the start year.")
        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "goal_form.html", {"kind": request.POST.get("kind", kind),
                                                      "prefill": prefill,
                                                      "settings": RetirementSettings.load(),
                                                      "income_fields": INCOME_FIELDS})
        plan = GoalPlan(user=request.user,
                        name=request.POST.get("name") or "Untitled goal",
                        kind=request.POST.get("kind", "one_time"),
                        flexible=request.POST.get("flexible") == "on",
                        years_to_start=request.POST.get("years_to_start") or 0, **windows, **{
                            k: Decimal(v) if v not in (None, "") else None
                            for k, v in d.items()})
        for k in ("current_equity", "current_taxable_fi", "current_taxfree_fi"):
            if getattr(plan, k) is None:
                setattr(plan, k, prefill.get(k, Decimal(0)))
        if plan.step_up_pct is None:
            plan.step_up_pct = Decimal(5)
        for k in ("rent_escalation", "pension_escalation", "other_escalation", "pension_floor_pct"):
            if getattr(plan, k) is None:
                setattr(plan, k, Decimal(0))
        plan.save()
        return redirect("goal_detail", pk=plan.pk)
    return render(request, "goal_form.html", {"kind": kind, "prefill": prefill,
                                              "settings": RetirementSettings.load(),
                                              "income_fields": INCOME_FIELDS})


@login_required
def goal_delete(request, pk):
    plan = get_object_or_404(GoalPlan, pk=pk, user=request.user)
    if request.method == "POST":
        plan.delete()
    return redirect("goals")


@login_required
def import_cas(request):
    context = {"providers": PROVIDERS}
    if request.method == "POST" and request.FILES.get("cas_pdf"):
        pdf = request.FILES["cas_pdf"]
        password = request.POST.get("password", "")
        tmp = f"/tmp/{request.user.id}_cas.pdf"
        with open(tmp, "wb") as f:
            for chunk in pdf.chunks():
                f.write(chunk)
        try:
            result = import_cas_pdf(request.user, tmp, password or None)
            refresh_holdings()
            record_snapshot(request.user)
            context["cas_result"] = result
        except Exception as exc:  # bad password / malformed PDF
            context["error"] = f"Import failed: {exc}"
    return render(request, "import.html", context)


@login_required
def import_broker(request):
    """Holdings CSV import for a broker/distributor (Zerodha, Groww, HDFC, Kotak, generic)."""
    context = {"providers": PROVIDERS}
    if request.method == "POST":
        upload = request.FILES.get("csv_file")
        provider_key = request.POST.get("provider", "generic")
        if not upload:
            context["error"] = "Choose a CSV file exported from your broker."
        else:
            result = import_holdings_csv(request.user, upload.read(), provider_key)
            refresh_holdings()
            record_snapshot(request.user)
            context["broker_result"] = result
    return render(request, "import.html", context)


@login_required
def import_bank(request):
    """Bank statement (CSV) import: HDFC, ICICI, Yes Bank, generic."""
    context = {"banks": BANK_PROVIDERS}
    if request.method == "POST":
        upload = request.FILES.get("csv_file")
        provider_key = request.POST.get("bank", "generic")
        name = request.POST.get("account_name", "").strip() or provider_key.title() + " Savings"
        if not upload:
            context["error"] = "Choose a bank statement CSV."
        else:
            result = import_bank_statement(request.user, upload.read(), provider_key, name)
            refresh_holdings()
            record_snapshot(request.user)
            context["bank_result"] = result
    return render(request, "import.html", context)


@login_required
def add_investment(request):
    """Create an asset and its first transaction, or append a transaction
    to an existing asset via ?asset=<pk>.

    Units-based classes (MF/stock/crypto/SGB/gold) ask quantity + price;
    balance-type schemes (PPF/EPF/NPS/FD/bond/real estate/other) ask a
    deposit or withdrawal amount only.
    """
    existing = None
    if request.GET.get("asset"):
        existing = get_object_or_404(Asset, pk=request.GET["asset"],
                                     holdings__account__user=request.user)

    error = ""
    if request.method == "POST":
        try:
            asset_type = request.POST.get("asset_type") or (existing.asset_type if existing else "")
            kind = request.POST["kind"]
            day = request.POST["date"]
            if not day:
                raise ValueError("Pick a date.")
            account = Account.objects.get(pk=request.POST["account"], user=request.user)

            if kind == "dividend":
                # Payouts are income (slab-taxed), not units: record amount only.
                amount = Decimal(request.POST.get("amount") or "0")
                if amount <= 0:
                    raise ValueError("Enter the dividend amount greater than 0.")
                tx = dict(kind=kind, date=day, quantity=Decimal(0),
                          price=Decimal(0), amount=amount)
            elif asset_type in {t.value for t in UNITS_TYPES}:
                qty = Decimal(request.POST["quantity"] or "0")
                price = Decimal(request.POST["price"] or "0")
                if qty <= 0 or price <= 0:
                    raise ValueError("Enter quantity and price greater than 0.")
                tx = dict(kind=kind, date=day, quantity=qty, price=price)
            else:
                amount = Decimal(request.POST["amount"] or "0")
                if amount <= 0:
                    raise ValueError("Enter an amount greater than 0.")
                # Deposits and withdrawals are amounts, not units: store
                # quantity=amount at price=1; refresh_holdings applies the
                # buy/sell sign.
                tx = dict(kind=kind, date=day, quantity=amount, price=Decimal(1))

            if existing:
                asset = existing
            else:
                name = request.POST.get("name", "").strip()
                if not name:
                    raise ValueError("Give the investment a name.")
                asset, _ = Asset.objects.get_or_create(
                    asset_type=asset_type,
                    identifier=request.POST.get("identifier", "").strip(),
                    defaults={"name": name, "isin": request.POST.get("isin", "").strip()},
                )
            Transaction.objects.create(asset=asset, account=account,
                                       source="manual", **tx)
            refresh_holdings()
            record_snapshot(request.user)
            messages.success(request, f"{asset.name} saved.")
            return redirect("holdings")
        except (ValueError, KeyError, Account.DoesNotExist, ArithmeticError) as exc:
            error = str(exc) or "Check the form and try again."

    accounts = Account.objects.filter(user=request.user)
    if not accounts.exists():
        accounts = [Account.objects.create(user=request.user, name="Default")]
    return render(request, "add.html", {
        "existing": existing,
        "accounts": accounts,
        "units_types": sorted(t.value for t in UNITS_TYPES),
        "asset_type_choices": Asset.AssetType.choices,
        "error": error,
    })


@login_required
def scan(request):
    """Photo of any statement/slip -> OCR draft -> editable table -> import."""
    from core.ocr import SlipDraft, guess_type, ocr_image, parse_slip
    from django.core.files.storage import default_storage
    import os

    step = "upload"
    context = {"step": step, "asset_type_choices": Asset.AssetType.choices}

    if request.method == "POST" and request.POST.get("step") == "extract":
        image = request.FILES.get("photo")
        hint = request.POST.get("hint", "").strip()
        if not image:
            context["error"] = "Attach a photo of the statement or slip."
        elif not hint:
            context["error"] = "Tell us what kind of document this is (e.g. “PPF passbook page”)."
        else:
            tmp = f"/tmp/{request.user.id}_scan.jpg"
            with open(tmp, "wb") as f:
                for chunk in image.chunks():
                    f.write(chunk)
            try:
                text = ocr_image(tmp)
            except Exception:
                text = ""
            draft = parse_slip(text, hint)
            draft.hint = hint
            context.update({"step": "review", "draft": draft, "hint": hint})
            os.remove(tmp)
            return render(request, "scan.html", context)

    elif request.method == "POST" and request.POST.get("step") == "confirm":
        account = Account.objects.filter(user=request.user).first()
        if not account:
            account = Account.objects.create(user=request.user, name="Default")
        created, failed = 0, []
        assets_touched: set = set()
        row_count = int(request.POST.get("row_count", "0"))
        for i in range(row_count):
            g = lambda key: (request.POST.get(f"row_{i}_{key}", "") or "").strip()
            name, asset_type = g("name"), g("asset_type") or Asset.AssetType.OTHER
            day, kind = g("date"), g("kind") or "buy"
            if not name or not day:
                if name or g("amount"):
                    failed.append(f"Row {i + 1}: missing name or date.")
                continue
            try:
                asset, _ = Asset.objects.get_or_create(
                    asset_type=asset_type,
                    identifier=f"scan:{asset_type}:{name.lower()[:40]}",
                    defaults={"name": name},
                )
                if kind == "dividend":
                    amount = Decimal(g("amount") or "0")
                    if amount <= 0:
                        raise ValueError("amount")
                    tx = dict(kind=kind, date=day, quantity=Decimal(0),
                              price=Decimal(0), amount=amount)
                elif asset_type in UNITS_TYPES:
                    qty, price = Decimal(g("quantity") or "0"), Decimal(g("price") or "0")
                    if qty <= 0 or price <= 0:
                        raise ValueError("quantity/price")
                    tx = dict(kind=kind, date=day, quantity=qty, price=price)
                else:
                    amount = Decimal(g("amount") or "0")
                    if amount <= 0:
                        raise ValueError("amount")
                    tx = dict(kind=kind, date=day, quantity=amount, price=Decimal(1))
                Transaction.objects.create(asset=asset, account=account, source="scan", **tx)
                assets_touched.add(asset)
                created += 1
            except (InvalidOperation, ValueError, ArithmeticError):
                failed.append(f"Row {i + 1} ({name or 'untitled'}): check the numbers.")
        refresh_holdings()
        # If the draft carried a closing balance and a single asset, apply it
        closing = request.POST.get("closing_value", "").strip().replace(",", "")
        if closing and len(assets_touched) == 1:
            asset = assets_touched.pop()
            try:
                for h in Holding.objects.filter(asset=asset):
                    h.current_value = Decimal(closing)
                    h.save()
            except ArithmeticError:
                pass
        record_snapshot(request.user)
        if created:
            messages.success(request, f"Imported {created} entr{'ies' if created != 1 else 'y'} from your scan.")
        for f_ in failed:
            messages.error(request, f_)
        return redirect("holdings")

    return render(request, "scan.html", context)


@login_required
def toggle_priority(request, pk):
    """Star/unstar an asset (HTMX): priority holdings pin first everywhere."""
    holding = get_object_or_404(Holding, pk=pk, account__user=request.user)
    asset = holding.asset
    asset.priority = not asset.priority
    asset.save(update_fields=["priority"])
    return render(request, "partials/priority_star.html",
                  {"holding_pk": pk, "priority": asset.priority})


@login_required
def update_value(request, pk):
    """Set the manual current value of a balance-type holding (HTMX inline).

    GET returns the inline form, POST saves it and re-renders the table row.
    """
    holding = get_object_or_404(Holding, pk=pk, account__user=request.user)
    view = portfolio_view(request.user)
    hv = next((h for h in view.holdings if h.holding_pk == pk), None)
    if request.method == "POST":
        raw = request.POST.get("current_value", "").strip().replace(",", "")
        try:
            holding.current_value = Decimal(raw) if raw else None
        except ArithmeticError:
            holding.current_value = None
        holding.save()
        refresh_holdings()
        record_snapshot(request.user)
        view = portfolio_view(request.user)
        hv = next((h for h in view.holdings if h.holding_pk == pk), None)
        return render(request, "partials/holding_row.html", {"h": hv})

    if request.GET.get("cancel"):
        return render(request, "partials/holding_row.html", {"h": hv})
    return render(request, "partials/value_form.html", {"holding": holding})
