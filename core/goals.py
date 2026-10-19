"""Goal planning engine — a faithful port of the Freefincal robo-advisor workbook
(July 2025) goal math: one-time goals, recurring goals, and the low-stress
retirement estimate. Pure functions; no Django dependencies.

Workbook conventions preserved:
- Existing corpus grows at its own post-tax rate (equity / taxable FI / tax-free FI).
- Goal cost inflates at education-style inflation (default 10%, overridable).
- Net corpus = inflated cost − FV of earmarked investments − expected lumpsums.
- Monthly SIP solves for net corpus with an annual step-up (default 5%).
- Retirement: expenses that persist inflate at 6% to retirement; corpus must
  fund inflation-protected withdrawals until life expectancy through the
  workbook's 4-bucket ladder (income / low / medium / high risk), each bucket
  earning its own return and refilled from the equity end every year.
- Post-retirement income (rent, pension…) and an optional annuity floor reduce
  what the investment corpus must fund.
"""
import math
from dataclasses import dataclass, field


@dataclass
class Assumptions:
    inflation: float = 6.0            # Settings!C3
    inflation_retirement: float = 6.0 # Settings!C5
    equity_return: float = 10.0       # Settings!C7 (post-tax)
    fixed_return: float = 6.0         # Settings!C8 − 1% (taxable, post-tax)
    taxfree_return: float = 7.0       # Settings!C8 (EPF/PPF)
    goal_inflation: float = 10.0      # education/lifestyle inflation
    life_expectancy: int = 90         # younger spouse


def _blended(equity_w: float, eq_ret: float, fi_ret: float) -> float:
    return equity_w * eq_ret + (1 - equity_w) * fi_ret


def fv(amount: float, rate_pct: float, years: float) -> float:
    return amount * (1 + rate_pct / 100) ** years


@dataclass
class GoalResult:
    future_cost: float
    fv_current: float
    net_corpus: float
    lumpsum_now: float
    monthly_sip: float
    step_up_pct: float
    equity_alloc_pct: float
    schedule: list = field(default_factory=list)  # [(year, monthly_amt, year_end_value)]
    # lumpsum_now is the one-time amount that, invested today in the goal portfolio,
    # alone reaches net_corpus: an alternative to the SIP, not a part of it.
    pre_goal_return: float = 0.0       # % return of the goal portfolio while accumulating
    post_goal_return: float | None = None  # % return while the corpus is being spent (recurring)
    derisk_years: float = 0.0          # final years before the goal held entirely in fixed income
    years_to_goal: float = 0.0         # years from today to the goal (recurring: to the first payment)
    years_to_start: float = 0.0        # years from today until the SIP begins


def suggest_equity_weight(flexible: bool, years: float) -> float:
    """Workbook spirit: rigid goals stay conservative; flexible goals take more
    equity the further away they are."""
    if not flexible:
        return 0.25 if years <= 3 else 0.40
    return 0.60 if years <= 7 else 0.75


def _monthly_rates(annual_return_pct: float, months: int, derisk_months: int = 0,
                   derisk_return_pct: float | None = None) -> list[float]:
    """Per-month rates: the goal portfolio's return, then (de-risking) the
    fixed-income return for the final derisk_months."""
    r = annual_return_pct / 100 / 12
    if not derisk_months or derisk_return_pct is None:
        return [r] * months
    r2 = derisk_return_pct / 100 / 12
    cut = max(0, months - int(derisk_months))
    return [r if i < cut else r2 for i in range(months)]


def lumpsum_growth(annual_return_pct: float, months: int, derisk_months: int = 0,
                   derisk_return_pct: float | None = None) -> float:
    """Growth factor of ₹1 invested now over `months`, monthly compounding,
    with an optional fixed-income tail (same convention as solve_sip)."""
    g = 1.0
    for r in _monthly_rates(annual_return_pct, months, derisk_months, derisk_return_pct):
        g *= 1 + r
    return g


def _fv_existing(equity: float, taxable_fi: float, taxfree_fi: float, years: float,
                 a: "Assumptions", derisk_years: float = 0.0) -> float:
    """Earmarked investments compounded to the goal, each at its own post-tax rate.
    De-risking moves the equity part to taxable fixed income for the final years."""
    d = min(max(0.0, derisk_years), max(0.0, years))
    eq = fv(fv(equity, a.equity_return, years - d), a.fixed_return, d)
    return eq + fv(taxable_fi, a.fixed_return, years) + fv(taxfree_fi, a.taxfree_return, years)


def solve_sip(net_corpus: float, annual_return_pct: float, months: int,
              step_up_pct: float, derisk_months: int = 0,
              derisk_return_pct: float | None = None) -> tuple[float, list]:
    """Monthly SIP that grows step_up% each year and reaches net_corpus.
    Binary-searched on a month-by-month simulation (mirrors the workbook's
    investment schedule). With derisk_months, the portfolio earns
    derisk_return_pct (fixed income) for the last derisk_months instead."""
    if months <= 0:
        return 0.0, []
    rates = _monthly_rates(annual_return_pct, months, derisk_months, derisk_return_pct)

    def end_value(first_month: float) -> float:
        bal, m = 0.0, first_month
        for i in range(months):
            if i and i % 12 == 0:
                m *= 1 + step_up_pct / 100
            bal = bal * (1 + rates[i]) + m
        return bal

    lo, hi = 0.0, max(net_corpus, 1.0)
    for _ in range(80):
        mid = (lo + hi) / 2
        if end_value(mid) < net_corpus:
            lo = mid
        else:
            hi = mid
    sip = (lo + hi) / 2
    # year-end schedule
    schedule, bal, m = [], 0.0, sip
    for i in range(months):
        if i and i % 12 == 0:
            m *= 1 + step_up_pct / 100
        bal = bal * (1 + rates[i]) + m
        if (i + 1) % 12 == 0 or i == months - 1:
            schedule.append(((i + 1) // 12 if (i + 1) % 12 == 0 else None, m, round(bal)))
    return round(sip, 0), schedule


def one_time_goal(*, cost_today: float, years_to_goal: float, years_to_start: float = 0.0,
                  flexible: bool = False, inflation_override: float | None = None,
                  current_equity: float = 0.0, current_taxable_fi: float = 0.0,
                  current_taxfree_fi: float = 0.0, lumpsum_expected: float = 0.0,
                  step_up_pct: float = 5.0, derisk_years: float = 0.0,
                  a: Assumptions | None = None) -> GoalResult:
    """Step 4A block.

    - Earmarked investments exist today, so they compound for the full
      years_to_goal (not just the SIP window) at their own post-tax rates.
    - derisk_years > 0: the whole goal portfolio (SIP and earmarked equity) sits
      in taxable fixed income for the final derisk_years — Freefincal's "move
      the money to safety before the goal" rule. 0 keeps the workbook's single
      blended return.
    - lumpsum_now is the one-time investment today that alone reaches the net
      corpus (PV at the goal portfolio's return); monthly_sip is the separate
      SIP-only answer.
    """
    a = a or Assumptions()
    infl = inflation_override if inflation_override else a.goal_inflation
    years = max(0.0, float(years_to_goal))
    d = min(max(0.0, float(derisk_years)), years)
    future_cost = max(0.0, fv(cost_today, infl, years))
    fv_current = _fv_existing(current_equity, current_taxable_fi, current_taxfree_fi, years, a, d)
    net = max(0.0, future_cost - fv_current - lumpsum_expected)
    eq_w = suggest_equity_weight(flexible, years)
    ret = _blended(eq_w, a.equity_return, a.fixed_return)
    months = int(round(years - years_to_start) * 12) if years > years_to_start else 0
    derisk_m = int(round(d * 12))
    schedule: list = []
    if months > 0:
        sip, schedule = solve_sip(net, ret, months, step_up_pct,
                                  min(months, derisk_m), a.fixed_return)
    else:
        sip = 0.0  # no SIP window left: only a lumpsum can fund it
    total_m = int(round(years * 12))
    lumpsum = net / lumpsum_growth(ret, total_m, derisk_m, a.fixed_return) if total_m > 0 else net
    return GoalResult(future_cost=round(future_cost), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(max(0.0, lumpsum)),
                      monthly_sip=sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=round(eq_w * 100), schedule=schedule,
                      pre_goal_return=ret, post_goal_return=None, derisk_years=d,
                      years_to_goal=years, years_to_start=float(years_to_start))


def recurring_goal(*, annual_cost_today: float, years_until_start: float, years_duration: float,
                   flexible: bool = False, inflation_override: float | None = None,
                   current_equity: float = 0.0, current_taxable_fi: float = 0.0,
                   current_taxfree_fi: float = 0.0, step_up_pct: float = 5.0,
                   derisk_years: float = 0.0, post_goal_return: float | None = None,
                   a: Assumptions | None = None) -> GoalResult:
    """Step 4B: recurring goals are funded with a sinking fund, not an equity
    corpus.

    The workbook is explicit: "for all these goals, you use a simple RD or
    sweep in SB acct or liquid fund". So the money earns the fixed-income
    (liquid / RD) rate both while saving and while spending, and the corpus is
    the PV of the cost stream discounted at that same rate. Costs are paid at
    the start of each year (fees, premiums), so the first payment is due on the
    goal date: PV is an annuity-due. post_goal_return overrides the spend rate
    (None = the same liquid rate). derisk_years and lumpsum_now behave as in
    one_time_goal, though de-risking is a no-op once everything is already in
    fixed income."""
    a = a or Assumptions()
    infl = (inflation_override if inflation_override else a.goal_inflation) / 100
    ret_pct = a.fixed_return
    post_pct = ret_pct if post_goal_return is None else float(post_goal_return)
    c1 = annual_cost_today * (1 + infl) ** years_until_start
    n, g, r = int(round(years_duration)), infl, post_pct / 100
    if abs(r - g) > 1e-9:
        pv = c1 * (1 - ((1 + g) / (1 + r)) ** n) / (r - g) * (1 + r)
    else:
        pv = c1 * n
    pv = max(0.0, pv)
    y = max(0.0, float(years_until_start))
    d = min(max(0.0, float(derisk_years)), y)
    fv_current = _fv_existing(current_equity, current_taxable_fi, current_taxfree_fi, y, a, d)
    net = max(0.0, pv - fv_current)
    months = int(round(y) * 12)
    derisk_m = int(round(d * 12))
    if months > 0:
        sip, schedule = solve_sip(net, ret_pct, months, step_up_pct, derisk_m, a.fixed_return)
        lumpsum = net / lumpsum_growth(ret_pct, months, derisk_m, a.fixed_return)
    else:
        sip, schedule, lumpsum = 0.0, [], net
    return GoalResult(future_cost=round(pv), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(max(0.0, lumpsum)),
                      monthly_sip=sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=0, schedule=schedule,
                      pre_goal_return=ret_pct, post_goal_return=post_pct, derisk_years=d,
                      years_to_goal=y, years_to_start=0.0)


def goal_scenarios(*, kind: str, a: Assumptions | None = None, **kwargs) -> list[dict]:
    """What-if table for a goal: market regimes and decision changes.

    Bear  : equity 7%, FI 5%, inflation 7% everywhere
    Base  : workbook assumptions
    Bull  : equity 12%, FI 7%, inflation 5%
    Decision variants on the base case: 10% step-up instead of the plan's,
    (retirement only) retiring 3 years later, and (one-time / recurring) de-risking:
    the goal money sits in fixed income for the last 3 years before the goal
    (recurring money is already all fixed income, so de-risking is a no-op there).
    Returns [{label, monthly_sip, net_corpus, note}].
    """
    a = a or Assumptions()
    variants = [
        ("Bear", Assumptions(inflation=7, inflation_retirement=7, equity_return=7,
                             fixed_return=5, taxfree_return=6, goal_inflation=12,
                             life_expectancy=a.life_expectancy), kwargs),
        ("Base", a, kwargs),
        ("Bull", Assumptions(inflation=5, inflation_retirement=5, equity_return=12,
                             fixed_return=7, taxfree_return=7.5, goal_inflation=8,
                             life_expectancy=a.life_expectancy), kwargs),
    ]
    step = float(kwargs.get("step_up_pct", 5))
    variants.append(("10% step-up", a, {**kwargs, "step_up_pct": 10}))
    if kind == "retirement":
        late = {**kwargs, "retire_age": int(kwargs.get("retire_age", 50)) + 3}
        variants.append((f"Retire at {late['retire_age']}", a, late))
    else:
        variants.append(("De-risk 3y", a, {**kwargs, "derisk_years": 3}))

    out = []
    for label, asm, kw in variants:
        kw = {k: v for k, v in kw.items() if k not in ("step_up_pct",)}
        step_up = 10 if label == "10% step-up" else step
        if kind == "one_time":
            r = one_time_goal(a=asm, step_up_pct=step_up, **kw)
            note = (f"all fixed income for the last {r.derisk_years:g} years" if r.derisk_years
                    else f"equity {r.equity_alloc_pct}%")
        elif kind == "recurring":
            r = recurring_goal(a=asm, step_up_pct=step_up, **kw)
            if label == "De-risk 3y":
                note = "spent from a 25%-equity mix" + (
                    f", fixed income for the last {r.derisk_years:g} years" if r.derisk_years else "")
            else:
                note = f"equity {r.equity_alloc_pct}%"
        else:
            r = retirement_plan(a=asm, step_up_pct=step_up, **kw)
            note = ("on track" if r.on_track
                    else f"post-ret equity {r.post_ret_equity_pct}%")
        out.append({"label": label, "monthly_sip": r.monthly_sip,
                    "net_corpus": r.net_corpus, "note": note})
    return out


# ---------------------------------------------------------------- retirement

def _xround(x: float) -> int:
    """Excel ROUND(x, 0): half away from zero (Python's round() is banker's)."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


@dataclass
class Bucket:
    name: str
    years: int
    return_pct: float
    equity_pct: int
    short: str = ""


def bucket_ladder(period: int, a: Assumptions | None = None) -> list[Bucket]:
    """The workbook's 4-bucket ladder (Settings!E13:E16 tenures, C19:C22 mixes).

    Income bucket: first min(T, 15) years, a fixed-income ladder (workbook 6% ≈
    taxable FI). Then Low / Medium / High risk buckets whose tenures and equity
    shares depend on the total period T; each blends equity with the workbook's
    all-FI return (Settings!C8 = tax-free FI rate)."""
    a = a or Assumptions()
    t = max(0, int(period))
    income = min(t, 15)
    if _xround(0.4 * t) < 15:
        low = max(0, _xround(0.5 * (t - income)))
        med = max(0, _xround(0.25 * (t - income)))
    else:
        low, med = _xround(0.25 * t), _xround(0.2 * t)
    high = max(0, t - income - low - med)
    if t <= 25:
        eq = (0.2, 0.3, 0.7)
    elif t <= 35:
        eq = (0.4, 0.5, 1.0)
    else:
        eq = (0.5, 0.7, 1.0)

    def ret(w: float) -> float:
        return round(w * a.equity_return + (1 - w) * a.taxfree_return, 4)

    return [Bucket("Income ladder (FD / debt funds)", income, a.fixed_return, 0, short="Income"),
            Bucket("Low risk", low, ret(eq[0]), round(eq[0] * 100), short="Low"),
            Bucket("Medium risk", med, ret(eq[1]), round(eq[1] * 100), short="Med"),
            Bucket("High risk (equity)", high, ret(eq[2]), round(eq[2] * 100), short="Equity")]


def _allocate(total: float, remaining_needs: list[float], buckets: list[Bucket]) -> list[float]:
    """Refill the ladder: each near bucket gets the PV (at its own return) of the
    years it now covers; the high-risk bucket holds whatever is left. If money
    is short, nearer buckets are filled first."""
    out, start, left = [], 0, total
    for b in buckets[:-1]:
        r = b.return_pct / 100
        target = sum(n / (1 + r) ** k
                     for k, n in enumerate(remaining_needs[start:start + b.years], start=start))
        take = min(left, target)
        out.append(take)
        left -= take
        start += b.years
    out.append(left)
    return out


def simulate_drawdown(start_corpus: float, needs: list[float],
                      buckets: list[Bucket]) -> tuple[list[dict], int | None]:
    """Year-by-year bucket drawdown (Step 6 style). Each year: rebalance the
    ladder forward, withdraw the year's need (start of year) from the income
    bucket first, then grow every bucket at its own return.
    Returns (rows, index of the first year with a shortfall or None)."""
    rows, total, depleted = [], max(0.0, start_corpus), None
    for t, need in enumerate(needs):
        bal = _allocate(total, needs[t:], buckets)
        opening, start_bal = total, list(bal)
        due = need
        for i in range(len(bal)):
            take = min(bal[i], due)
            bal[i] -= take
            due -= take
        withdrawal = need - due
        growth = [x * b.return_pct / 100 for x, b in zip(bal, buckets)]
        total = sum(bal) + sum(growth)
        if due > 1 and depleted is None:
            depleted = t
        rows.append({"year": t + 1, "opening": opening, "balances": start_bal,
                     "balances_after_withdrawal": list(bal), "growth": growth,
                     "need": need, "withdrawal": withdrawal, "shortfall": due if due > 1 else 0.0,
                     "closing": total})
    return rows, depleted


def required_corpus(needs: list[float], buckets: list[Bucket]) -> float:
    """Smallest starting corpus that meets every year's need via the ladder."""
    if not needs or sum(needs) <= 0:
        return 0.0
    lo, hi = 0.0, sum(needs)
    while simulate_drawdown(hi, needs, buckets)[1] is not None:
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        if simulate_drawdown(mid, needs, buckets)[1] is None:
            hi = mid
        else:
            lo = mid
    return hi


@dataclass
class RetirementResult:
    years_to_retirement: float
    first_year_monthly_expense: float
    gross_corpus: float
    corpus_in_hand: float
    net_corpus: float
    monthly_sip: float
    step_up_pct: float
    post_ret_equity_pct: int
    emergency_corpus: float
    on_track: bool
    schedule: list = field(default_factory=list)
    drawdown_corpus: float = 0
    annuity_cost: float = 0
    floor_pct: float = 0
    floor_monthly: float = 0
    income_monthly_at_retirement: float = 0          # streams paying in year 0
    income_streams: list = field(default_factory=list)  # [{monthly_at_retirement, escalation_pct, start, stop}]
    buckets: list = field(default_factory=list)      # [{name, years, return_pct, equity_pct, allocation}]
    cash_flow: list = field(default_factory=list)    # year-by-year drawdown of the funded plan
    depletion_age: int | None = None                 # in cash_flow
    current_path_depletion_age: int | None = None    # with today's investments only
    lumpsum_now: float = 0                           # one-time investment today instead of the SIP
    pre_goal_return: float = 0.0                     # % return assumed while accumulating


def retirement_plan(*, current_age: int, retire_age: int, monthly_expense: float,
                    annual_expense: float = 0.0, current_equity: float = 0.0,
                    current_taxable_fi: float = 0.0, current_taxfree_fi: float = 0.0,
                    lumpsum_at_retirement: float = 0.0, step_up_pct: float = 5.0,
                    income_streams=(), floor_pct: float = 0.0,
                    a: Assumptions | None = None) -> RetirementResult:
    """Steps 2–3 + 6: inflation-protected withdrawals to life expectancy funded
    by the workbook's 4-bucket ladder.

    income_streams: up to 3 (monthly amount today, annual escalation %[, start,
      stop]) — rent, pension, other (Step 2 rows 31–41). Each grows at its
      escalation % until retirement and through retirement, and is netted off
      the year's expense only in retirement years start <= k < stop (0-based;
      start None = 0, stop None = never stops).
    floor_pct: an inflation-indexed annuity bought at retirement covering this %
      of the first-year expense, rising with retirement inflation for life. Its
      price (PV at the taxable-FI rate) is added to the corpus; the bucket
      ladder funds only the remainder.
    """
    a = a or Assumptions()
    streams = []
    for s in income_streams:
        m, e, start, stop = (tuple(s) + (None, None))[:4]
        streams.append((float(m or 0), float(e or 0), int(start or 0),
                        int(stop) if stop is not None else None))
    if len(streams) > 3:
        raise ValueError("At most 3 post-retirement income streams")
    yrs = max(0.0, float(retire_age - current_age))
    avg_monthly = (12 * monthly_expense + annual_expense) / 12
    # Workbook: inflate at pre-retirement inflation (Settings!C3) up to retirement,
    # then at retirement inflation (Settings!C5) through drawdown.
    first_year_monthly = avg_monthly * (1 + a.inflation / 100) ** yrs
    n = max(0, int(a.life_expectancy - retire_age))
    infl = a.inflation_retirement / 100
    exp1 = first_year_monthly * 12
    stream_ret = [(m * 12 * (1 + e / 100) ** yrs, e / 100, start, stop)
                  for m, e, start, stop in streams]
    floor1 = exp1 * max(0.0, min(100.0, float(floor_pct))) / 100

    def active(k: int, start: int, stop: int | None) -> bool:
        return start <= k and (stop is None or k < stop)

    years = []
    for k in range(n):
        expense = exp1 * (1 + infl) ** k
        floor = floor1 * (1 + infl) ** k
        income = sum(s * (1 + g) ** k for s, g, start, stop in stream_ret if active(k, start, stop))
        income_applied = min(income, max(0.0, expense - floor))
        years.append({"age": retire_age + k, "expense": expense, "floor": floor,
                      "income": income_applied, "need": max(0.0, expense - floor - income)})
    needs = [y["need"] for y in years]
    buckets = bucket_ladder(n, a)
    drawdown = round(required_corpus(needs, buckets))
    annuity = round(sum(y["floor"] / (1 + a.fixed_return / 100) ** k for k, y in enumerate(years)))
    emergency = round(first_year_monthly * 6)
    gross = drawdown + annuity + emergency
    corpus_in_hand = (fv(current_equity, a.equity_return, yrs)
                      + fv(current_taxable_fi, a.fixed_return, yrs)
                      + fv(current_taxfree_fi, a.taxfree_return, yrs)
                      + lumpsum_at_retirement)
    net = max(0.0, gross - corpus_in_hand)
    months = int(round(yrs) * 12)
    pre_ret = _blended(0.65, a.equity_return, a.fixed_return)
    sip, schedule = solve_sip(net, pre_ret, months, step_up_pct) if months > 0 else (0.0, [])
    lumpsum = net / lumpsum_growth(pre_ret, months) if months > 0 else net

    def run(start: float) -> tuple[list[dict], int | None]:
        rows, dep = simulate_drawdown(start, needs, buckets)
        for row, y in zip(rows, years):
            row.update(y)
        return rows, (retire_age + dep if dep is not None else None)

    spare = corpus_in_hand - emergency - annuity
    cash_flow, depletion_age = run(max(drawdown, spare))
    _, current_dep = run(max(0.0, spare))
    alloc = cash_flow[0]["balances"] if cash_flow else [0.0] * len(buckets)
    total_alloc = sum(alloc)
    post_eq = round(sum(x * b.equity_pct for x, b in zip(alloc, buckets)) / total_alloc) if total_alloc else 0
    return RetirementResult(years_to_retirement=round(yrs),
                            first_year_monthly_expense=round(first_year_monthly),
                            gross_corpus=gross, corpus_in_hand=round(corpus_in_hand),
                            net_corpus=round(net), monthly_sip=sip, step_up_pct=step_up_pct,
                            post_ret_equity_pct=post_eq, emergency_corpus=emergency,
                            on_track=net <= 0, schedule=schedule,
                            drawdown_corpus=drawdown, annuity_cost=annuity,
                            floor_pct=float(floor_pct), floor_monthly=round(floor1 / 12),
                            income_monthly_at_retirement=round(sum(
                                s for s, _, start, stop in stream_ret if active(0, start, stop)) / 12),
                            income_streams=[{"monthly_at_retirement": round(s / 12),
                                             "escalation_pct": g * 100, "start": start, "stop": stop}
                                            for s, g, start, stop in stream_ret],
                            buckets=[{"name": b.name, "short": b.short, "years": b.years, "return_pct": b.return_pct,
                                      "equity_pct": b.equity_pct, "allocation": round(x)}
                                     for b, x in zip(buckets, alloc)],
                            cash_flow=cash_flow, depletion_age=depletion_age,
                            current_path_depletion_age=current_dep,
                            lumpsum_now=round(max(0.0, lumpsum)), pre_goal_return=pre_ret)


def sip_affordability(monthly_sip: float, monthly_take_home: float | None) -> dict | None:
    """Step 2 A24: the suggested SIP as a share of monthly take-home pay (incl.
    employer + employee EPF/NPS). None when take-home is unknown.
    Bands: < 25% comfortable · 25–40% stretch · > 40% aggressive."""
    if not monthly_take_home or float(monthly_take_home) <= 0:
        return None
    # Band on the displayed (rounded) percentage so the text never contradicts it.
    pct = _xround(float(monthly_sip) / float(monthly_take_home) * 100)
    if pct < 25:
        band, note = "comfortable", "comfortably affordable"
    elif pct <= 40:
        band, note = "stretch", "stretch but doable with the step-up starting small"
    else:
        band, note = "aggressive", "aggressive — consider a longer horizon or a lower expense target"
    return {"pct": pct, "band": band, "note": note}


def young_earner_advice(*, current_age: int, monthly_expense: float, annual_expense: float = 0.0,
                        years_to_retirement: float, net_corpus: float,
                        monthly_take_home: float | None = None,
                        a: Assumptions | None = None) -> dict:
    """Step 1's under-26 path, simplified: build a 6-month emergency fund first,
    then a 100% equity SIP stepped up 10% a year toward the retirement gap. No
    bucket ladder — revisit the full plan at 26."""
    a = a or Assumptions()
    emergency = round(6 * (monthly_expense + annual_expense / 12))
    months = int(round(max(0.0, years_to_retirement)) * 12)
    sip = solve_sip(net_corpus, a.equity_return, months, 10)[0] if months > 0 and net_corpus > 0 else 0.0
    return {"emergency_fund": emergency, "monthly_sip": sip, "step_up_pct": 10,
            "equity_pct": 100, "revisit_in_years": max(0, 26 - int(current_age)),
            "affordability": sip_affordability(sip, monthly_take_home)}


# ------------------------------------------------------- goal detail context

def assumption_notes(a: Assumptions, *, kind: str = "one_time",
                     goal_inflation: float | None = None) -> list[dict]:
    """Plain-language notes on the return and inflation assumptions: what
    "post-tax" means in India today, plus realism warnings when the numbers are
    more optimistic than Freefincal's defaults. Returns [{tone, text}] with
    tone "info" or "warn"."""
    notes = [
        {"tone": "info", "text": (
            f"Returns are after tax. Equity at {a.equity_return:g}% assumes long-term gains taxed "
            "at 12.5% above the ₹1.25 lakh yearly exemption. Taxable fixed income "
            f"(FDs, debt funds) at {a.fixed_return:g}% is already net of slab-rate tax: debt fund "
            "gains bought after 1 April 2023 get no indexation. "
            f"PPF/EPF at {a.taxfree_return:g}% is tax-free.")},
    ]
    if a.equity_return > 12:
        notes.append({"tone": "warn", "text": (
            f"Equity at {a.equity_return:g}% after tax is optimistic. Freefincal plans with 10%: "
            "lower it, or the SIP shown here will be too small.")})
    infl = a.inflation if kind == "retirement" else (goal_inflation or a.goal_inflation)
    if a.equity_return - infl > 5:
        notes.append({"tone": "warn", "text": (
            f"Equity beats inflation by {a.equity_return - infl:g} points a year here. "
            "Real returns above about 5% rarely hold over long periods.")})
    if a.fixed_return > a.taxfree_return:
        notes.append({"tone": "warn", "text": (
            f"Taxable fixed income ({a.fixed_return:g}%) beats PPF/EPF ({a.taxfree_return:g}%) "
            "after tax. That is unusual, so check whether it is a pre-tax rate.")})
    if kind != "retirement" and goal_inflation is not None and goal_inflation < a.inflation:
        notes.append({"tone": "warn", "text": (
            f"Goal inflation of {goal_inflation:g}% is below general inflation ({a.inflation:g}%). "
            "Education, healthcare and housing usually rise faster than general inflation, not slower.")})
    return notes


def recent_monthly_investing(buys, today=None, months: int = 6) -> float | None:
    """Average monthly purchase amount over the last `months` months, a proxy
    for "current SIP" across all tracked accounts. `buys` is an iterable of
    (date, amount). None when nothing was bought in the window."""
    import datetime as _dt
    today = today or _dt.date.today()
    since = today - _dt.timedelta(days=round(months * 30.44))
    total = sum(float(amt or 0) for d, amt in buys if since < d <= today)
    return round(total / months) if total > 0 else None


def _sip_path(first_month: float, rates: list[float], start_month: int, total_months: int,
              step_up_pct: float) -> list[float]:
    """Year-end SIP balances from today (index 0 = today) to total_months.
    The SIP starts at start_month and steps up every 12 SIP months; rates are
    indexed by SIP month (as in solve_sip)."""
    path, bal, m, k = [0.0], 0.0, first_month, 0
    for i in range(total_months):
        if i >= start_month:
            if k and k % 12 == 0:
                m *= 1 + step_up_pct / 100
            bal = bal * (1 + rates[k]) + m
            k += 1
        if (i + 1) % 12 == 0 or i == total_months - 1:
            path.append(bal)
    return path


def goal_progress(plan, result, a: Assumptions | None = None, *,
                  current_sip: float | None = None, start_year: int | None = None) -> dict:
    """Everything the goal detail page needs to answer "am I on track?".

    plan: a GoalPlan (duck-typed, any object with the same attributes).
    result: the GoalResult / RetirementResult computed for it.
    a: the assumptions used for the result (saved settings, with the retirement
       inflation override applied).
    current_sip: what the user invests per month today (None = unknown; the
       projection then uses existing investments only).

    Returns target, projected corpus at the goal date, surplus (+) or
    shortfall (-), required vs current SIP, an assumptions table, notes and a
    yearly chart series (required path, current path, target).
    """
    import datetime as _dt
    import json as _json
    a = a or Assumptions()
    kind = str(plan.kind)
    eq, tfi, tff = (float(plan.current_equity or 0), float(plan.current_taxable_fi or 0),
                    float(plan.current_taxfree_fi or 0))
    step = float(result.step_up_pct)
    infl_override = float(plan.inflation_override) if getattr(plan, "inflation_override", None) else None
    if kind == "retirement":
        years = max(0, int((plan.retire_age or 0) - (plan.current_age or 0)))
        start, derisk, target = 0.0, 0.0, float(result.gross_corpus)
        end_lumpsum = float(plan.lumpsum_expected or 0)
        ret = result.pre_goal_return
        labels_base, label_fmt = int(plan.current_age or 0), "Age {}"
        goal_label = f"retirement at {plan.retire_age}"
        infl_text = f"{a.inflation:g}% now, {a.inflation_retirement:g}% in retirement"
        post_text = (f"{min(b['return_pct'] for b in result.buckets):g}–"
                     f"{max(b['return_pct'] for b in result.buckets):g}% across the bucket ladder"
                     if result.buckets else "—")
        goal_infl = None
    else:
        years = max(0, int(round(result.years_to_goal)))
        start, derisk, target = result.years_to_start, result.derisk_years, float(result.future_cost)
        end_lumpsum = float(plan.lumpsum_expected or 0) if kind == "one_time" else 0.0
        ret = result.pre_goal_return
        labels_base = start_year or _dt.date.today().year
        label_fmt = "{}"
        goal_label = (f"the goal in {labels_base + years}" if kind == "one_time"
                      else f"the first payment in {labels_base + years}")
        goal_infl = infl_override or a.goal_inflation
        infl_text = f"{goal_infl:g}% a year on the goal cost"
        if kind == "recurring":
            post_text = f"{result.post_goal_return:g}% while the corpus is spent"
        else:
            post_text = "Not needed: a one-time goal is spent on the goal date"
    total_m = years * 12
    sip_m = max(0, int(round(years - start)) * 12) if years > start else 0
    derisk_m = min(sip_m, int(round(derisk * 12)))
    rates = _monthly_rates(ret, sip_m, derisk_m, a.fixed_return)
    start_m = total_m - sip_m

    if kind == "retirement" or not derisk:
        existing = [_fv_existing(eq, tfi, tff, t, a) for t in range(years + 1)]
    else:  # equity switches to FI only in the final `derisk` years
        d = derisk
        existing = [fv(fv(eq, a.equity_return, min(t, years - d)), a.fixed_return, max(0.0, t - (years - d)))
                    + fv(tfi, a.fixed_return, t) + fv(tff, a.taxfree_return, t)
                    for t in range(years + 1)]
    req_sip = float(result.monthly_sip or 0)
    req = _sip_path(req_sip, rates, start_m, total_m, step) if total_m else [0.0]
    cur = (_sip_path(float(current_sip), rates, start_m, total_m, step)
           if total_m and current_sip else [0.0] * (years + 1))
    required_path = [e + s for e, s in zip(existing, req)]
    current_path = [e + s for e, s in zip(existing, cur)]
    required_path[-1] += end_lumpsum
    current_path[-1] += end_lumpsum
    if req_sip == 0 and result.net_corpus > 0:
        # No SIP window: the required path is the lumpsum-today alternative.
        lump = float(result.lumpsum_now)
        g = [lumpsum_growth(ret, t * 12) for t in range(years + 1)]
        required_path = [c + lump * gg for c, gg in zip(current_path, g)]
    projected = current_path[-1]
    gap = projected - target
    on_track = result.net_corpus <= 0 or gap >= 0
    funded_pct = 100 if target <= 0 else max(0, min(100, round(projected / target * 100)))

    assumptions = [
        ("Inflation", infl_text),
        ("Equity return, after tax", f"{a.equity_return:g}%"),
        ("Fixed income return, after tax", f"{a.fixed_return:g}% taxable · {a.taxfree_return:g}% PPF/EPF"),
        ("Expected return before the goal", f"{ret:g}% a year"
         + (f", all fixed income ({a.fixed_return:g}%) for the last {derisk:g} years" if derisk else "")),
        ("Return after the goal", post_text),
        ("Years left", f"{years}" + (f" (SIP starts in {start:g})" if start else "")),
        ("SIP step-up", f"{step:g}% a year"),
    ]
    chart = {"labels": [label_fmt.format(labels_base + t) for t in range(years + 1)],
             "required": [round(v) for v in required_path],
             "current": [round(v) for v in current_path],
             "target": round(target),
             "has_current_sip": bool(current_sip)}
    return {
        "target": round(target), "projected": round(projected), "gap": round(gap),
        "gap_abs": round(abs(gap)), "on_track": on_track, "funded_pct": funded_pct,
        "required_sip": round(req_sip), "current_sip": round(current_sip) if current_sip else None,
        "sip_gap": round(float(current_sip) - req_sip) if current_sip else None,
        "lumpsum_now": round(float(result.lumpsum_now or 0)),
        "years_left": years, "goal_label": goal_label,
        "assumptions": assumptions,
        "notes": assumption_notes(a, kind=kind, goal_inflation=goal_infl),
        "chart": _json.dumps(chart),
    }
