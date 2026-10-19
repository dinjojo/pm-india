"""Fund-analytics views: per-scheme analytics page and the returns calculator
(owned by the fund-analytics worker). The /funds/ browse page is rendered by
core.views.funds; its catalog context comes from core.funds.browse_context via
the {% browse_catalog %} tag in core/templatetags/fund_browse.py."""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render

from core import fund_analytics as fa

MC_DEFAULT_AMOUNT = 100_000


def _num(raw, default, lo, hi, cast=float):
    try:
        v = cast(float(raw))
    except (TypeError, ValueError):
        return default, raw not in (None, "")
    if not (lo <= v <= hi):
        return default, True
    return v, False


def _fmt(v) -> str:
    """Form-friendly number: 12.0 → "12", 14.5 → "14.5"."""
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


@login_required
def scheme_analytics(request, code: int):
    data = fa.scheme_analytics(code)
    if data is None:
        raise Http404("Unknown scheme")
    amount, _ = _num(request.GET.get("amount"), MC_DEFAULT_AMOUNT, 1_000, 1e10)
    ctx = {"a": data, "code": code, "amount": amount, "amount_str": _fmt(float(amount))}
    if data.get("enough"):
        ctx["mc_inr"] = fa.scale_var(data["mc"], amount)
        rate = data["returns"].get("5Y") or data["returns"].get("Since inception") or 12
        ctx["calc_rate"] = _fmt(round(float(rate), 1))
        ctx["calc_url"] = "/funds/calculator/?" + urlencode({"rate": ctx["calc_rate"], "mode": "sip"})
        ctx["calc_url_lumpsum"] = "/funds/calculator/?" + urlencode(
            {"rate": ctx["calc_rate"], "mode": "lumpsum", "amount": _fmt(float(amount))})
        ctx["charts"] = {
            "chart": data["chart"], "rolling_vol": data["rolling_vol"],
            "hist_labels": data["mc"]["hist_labels"], "hist_counts": data["mc"]["hist_counts"],
        }
    return render(request, "fund_analytics.html", ctx)


@login_required
def calculator(request):
    mode = "sip" if request.GET.get("mode") == "sip" else "lumpsum"
    default_amount = 10_000 if mode == "sip" else 100_000
    amount, e1 = _num(request.GET.get("amount"), default_amount, 1, 1e11)
    rate, e2 = _num(request.GET.get("rate"), 12.0, -50, 100)
    years, e3 = _num(request.GET.get("years"), 10, 1, 60, cast=int)
    inflation, e4 = _num(request.GET.get("inflation"), 6.0, -20, 50)
    calc = (fa.sip_fv if mode == "sip" else fa.lumpsum_fv)(amount, rate, years, inflation=inflation)
    keep = {"rate": _fmt(rate), "years": years, "inflation": _fmt(inflation)}
    return render(request, "calculator.html", {
        "mode": mode,
        "amount": _fmt(float(amount)), "rate": _fmt(rate), "years": years, "inflation": _fmt(inflation),
        "invalid": any((e1, e2, e3, e4)),
        "calc": calc,
        "tab_urls": {m: "?" + urlencode({"mode": m, **keep}) for m in ("lumpsum", "sip")},
        "chart": {
            "labels": [f"Y{p['year']}" for p in calc["series"]],
            "nominal": [round(p["nominal"]) for p in calc["series"]],
            "real": [round(p["real"]) for p in calc["series"]],
            "invested": [round(p["invested"]) for p in calc["series"]],
        },
    })
