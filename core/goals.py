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


def suggest_equity_weight(flexible: bool, years: float) -> float:
    """Workbook spirit: rigid goals stay conservative; flexible goals take more
    equity the further away they are."""
    if not flexible:
        return 0.25 if years <= 3 else 0.40
    return 0.60 if years <= 7 else 0.75


def solve_sip(net_corpus: float, annual_return_pct: float, months: int,
              step_up_pct: float) -> tuple[float, list]:
    """Monthly SIP that grows step_up% each year and reaches net_corpus.
    Binary-searched on a month-by-month simulation (mirrors the workbook's
    investment schedule)."""
    if months <= 0:
        return 0.0, []
    r = annual_return_pct / 100 / 12

    def end_value(first_month: float) -> float:
        bal, m = 0.0, first_month
        for i in range(months):
            if i and i % 12 == 0:
                m *= 1 + step_up_pct / 100
            bal = bal * (1 + r) + m
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
        bal = bal * (1 + r) + m
        if (i + 1) % 12 == 0 or i == months - 1:
            schedule.append(((i + 1) // 12 if (i + 1) % 12 == 0 else None, m, round(bal)))
    return round(sip, 0), schedule


def one_time_goal(*, cost_today: float, years_to_goal: float, years_to_start: float = 0.0,
                  flexible: bool = False, inflation_override: float | None = None,
                  current_equity: float = 0.0, current_taxable_fi: float = 0.0,
                  current_taxfree_fi: float = 0.0, lumpsum_expected: float = 0.0,
                  step_up_pct: float = 5.0, a: Assumptions | None = None) -> GoalResult:
    """Step 4A block, verbatim math."""
    a = a or Assumptions()
    infl = inflation_override if inflation_override else a.goal_inflation
    future_cost = max(0.0, fv(cost_today, infl, years_to_goal))
    y = max(0.0, years_to_goal - years_to_start)
    fv_current = (fv(current_equity, a.equity_return, y)
                  + fv(current_taxable_fi, a.fixed_return, y)
                  + fv(current_taxfree_fi, a.taxfree_return, y))
    net = max(0.0, future_cost - fv_current - lumpsum_expected)
    eq_w = suggest_equity_weight(flexible, years_to_goal)
    ret = _blended(eq_w, a.equity_return, a.fixed_return)
    months = int(round(years_to_goal - years_to_start) * 12) if years_to_goal > years_to_start else 0
    # A goal already funded: no SIP needed.
    if months <= 0:
        sip = net if net > 0 else 0.0  # due immediately — show as lumpsum
    else:
        sip, schedule = solve_sip(net, ret, months, step_up_pct)
    lumpsum = max(0.0, net) if months <= 0 else max(0.0, net * 0.25)  # 25% lumpsum option, rest SIP
    return GoalResult(future_cost=round(future_cost), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(lumpsum),
                      monthly_sip=sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=round(eq_w * 100), schedule=schedule)


def recurring_goal(*, annual_cost_today: float, years_until_start: float, years_duration: float,
                   flexible: bool = False, inflation_override: float | None = None,
                   current_equity: float = 0.0, current_taxable_fi: float = 0.0,
                   current_taxfree_fi: float = 0.0, step_up_pct: float = 5.0,
                   a: Assumptions | None = None) -> GoalResult:
    """Step 4B: corpus = PV (at goal start) of a growing annuity of annual costs,
    then planned like a one-time goal from today."""
    a = a or Assumptions()
    infl = (inflation_override if inflation_override else a.goal_inflation) / 100
    eq_w = suggest_equity_weight(flexible, years_until_start + years_duration)
    ret = _blended(eq_w, a.equity_return, a.fixed_return) / 100
    # PV of growing annuity: c1 * [1 - ((1+g)/(1+r))^n] / (r - g), c1 at start year
    c1 = annual_cost_today * (1 + infl) ** years_until_start
    n, g, r = int(round(years_duration)), infl, ret
    pv = c1 * (1 - ((1 + g) / (1 + r)) ** n) / (r - g) if abs(r - g) > 1e-9 else c1 * n
    pv = max(0.0, pv)
    y = max(0.0, years_until_start)
    fv_current = (fv(current_equity, a.equity_return, y)
                  + fv(current_taxable_fi, a.fixed_return, y)
                  + fv(current_taxfree_fi, a.taxfree_return, y))
    net = max(0.0, pv - fv_current)
    months = int(round(years_until_start) * 12)
    sip, schedule = solve_sip(net, ret / 1, months, step_up_pct) if months > 0 else (net if net > 0 else 0.0, [])
    return GoalResult(future_cost=round(pv), fv_current=round(fv_current),
                      net_corpus=round(net), lumpsum_now=round(max(0.0, net * 0.25)) if months > 0 else round(net),
                      monthly_sip=sip, step_up_pct=step_up_pct,
                      equity_alloc_pct=round(eq_w * 100), schedule=schedule)


def goal_scenarios(*, kind: str, a: Assumptions | None = None, **kwargs) -> list[dict]:
    """What-if table for a goal: market regimes and decision changes.

    Bear  : equity 7%, FI 5%, inflation 7% everywhere
    Base  : workbook assumptions
    Bull  : equity 12%, FI 7%, inflation 5%
    Decision variants on the base case: 10% step-up instead of the plan's,
    and (retirement only) retiring 3 years later.
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
        variants.append(("Retire at 53", a, late))

    out = []
    for label, asm, kw in variants:
        kw = {k: v for k, v in kw.items() if k not in ("step_up_pct",)}
        step_up = 10 if label == "10% step-up" else step
        if kind == "one_time":
            r = one_time_goal(a=asm, step_up_pct=step_up, **kw)
            note = f"equity {r.equity_alloc_pct}%"
        elif kind == "recurring":
            r = recurring_goal(a=asm, step_up_pct=step_up, **kw)
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
    sip, schedule = solve_sip(net, _blended(0.65, a.equity_return, a.fixed_return), months, step_up_pct) if months > 0 else (0.0, [])

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
                            current_path_depletion_age=current_dep)


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
