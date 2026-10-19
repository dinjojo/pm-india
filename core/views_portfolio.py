"""Portfolio tools page: MPT optimisation, CAPM, overlap, curated baskets."""
import json

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core import portfolio_tools as pt

SAMPLE_CSV = "scheme_code,amount\n122639,50000\n118989,30000\n120197,20000"
CATEGORIES = [("equity", "Equity"), ("hybrid", "Hybrid"), ("debt", "Debt")]
MAX_UPLOAD_BYTES = 256 * 1024


def _pct(x):
    return round(float(x) * 100, 2)


def _heat(r2: float) -> str:
    """Inline background for the overlap heatmap: green (low R²) → red (high)."""
    r2 = max(0.0, min(1.0, float(r2)))
    hue = 140 * (1 - r2)  # 140 = green, 0 = red
    return f"background-color: hsl({hue:.0f} 70% {88 - 8 * r2:.0f}%)"


def _chart_payload(res: dict) -> dict:
    p = res["portfolios"]
    return {
        "frontier": [{"x": _pct(f["vol"]), "y": _pct(f["ret"])} for f in res["frontier"]],
        "funds": [{"x": _pct(f["vol"]), "y": _pct(f["exp_ret"]), "label": f["name"]} for f in res["funds"]],
        "current": [{"x": _pct(p["current"]["vol"]), "y": _pct(p["current"]["ret"])}],
        "min_var": [{"x": _pct(p["min_var"]["vol"]), "y": _pct(p["min_var"]["ret"])}],
        "max_sharpe": [{"x": _pct(p["max_sharpe"]["vol"]), "y": _pct(p["max_sharpe"]["ret"])}],
    }


def _overlap_rows(ov: dict) -> list[dict]:
    rows = []
    for i, name in enumerate(ov["names"]):
        cells = []
        for j in range(len(ov["codes"])):
            r2 = ov["r2"][i][j]
            cells.append({"r2": r2, "corr": ov["corr"][i][j], "style": "background-color: #f5f5f4" if i == j else _heat(r2),
                          "diag": i == j})
        rows.append({"name": name, "code": ov["codes"][i], "cells": cells})
    return rows


def _add_pcts(d: dict, keys) -> dict:
    """Templates can't multiply: add `<key>_pct` (×100) beside each fraction."""
    for k in keys:
        if d.get(k) is not None:
            d[f"{k}_pct"] = _pct(d[k])
    return d


def _decorate(res: dict) -> None:
    for f in res["funds"]:
        _add_pcts(f, ("cagr", "vol", "weight", "min_var_weight", "max_sharpe_weight"))
        f["ms_delta_pct"] = round(f["max_sharpe_weight_pct"] - f["weight_pct"], 2)
        if f["capm"]:
            _add_pcts(f["capm"], ("expected", "alpha"))
    for p in res["portfolios"].values():
        _add_pcts(p, ("ret", "cagr", "vol"))
    if res.get("benchmark"):
        _add_pcts(res["benchmark"], ("cagr",))


def decorate_basket(b: dict) -> dict:
    _add_pcts(b, ("ret", "cagr", "vol", "target", "max_vol"))
    if "ret" in b and "target" in b:
        b["gap_pct"] = round(abs(b["ret_pct"] - b["target_pct"]), 2)
    for f in b["funds"]:
        _add_pcts(f, ("weight", "exp_ret", "exp_vol", "cagr", "vol"))
    return b


def _run_analysis(ctx: dict, rows: list[tuple[int, float]], names: dict | None = None):
    res = pt.analyse_portfolio(rows, names=names)
    ctx["dropped"] = res["dropped"]
    if res["error"]:
        ctx["analysis_error"] = res["error"]
        return
    _decorate(res)
    ctx["res"] = res
    ctx["chart"] = _chart_payload(res)
    ctx["overlap_rows"] = _overlap_rows(res["overlap"])
    names_by_code = {f["code"]: f["name"] for f in res["funds"]}
    ctx["redundant"] = [{**p, "a_name": names_by_code[p["a"]], "b_name": names_by_code[p["b"]]}
                        for p in res["overlap"]["redundant"]]
    ctx["near_cash"] = [f["name"] for f in res["funds"] if f["vol"] < 0.02]


def _float(request, key, default):
    try:
        return float(request.POST.get(key, default))
    except (TypeError, ValueError):
        return default


@login_required
def portfolio_tools(request):
    ctx = {"sample_csv": SAMPLE_CSV, "categories": CATEGORIES, "rf_pct": _pct(pt.RF),
           "input_errors": [], "dropped": [], "basket_form": {"target_return": 12, "max_vol": 12,
                                                              "categories": ["equity", "hybrid", "debt"]}}
    if request.method == "POST":
        action = request.POST.get("action")
        ctx["action"] = action
        if action == "csv":
            upload = request.FILES.get("file")
            text = request.POST.get("csv_text", "")
            if upload is not None:
                if upload.size > MAX_UPLOAD_BYTES:
                    ctx["input_errors"] = ["File too large — keep it under 256 KB."]
                    text = None
                else:
                    text = upload.read().decode("utf-8-sig", errors="replace")
            if text is not None:
                rows, errors = pt.parse_holdings_csv(text)
                ctx["input_errors"] = errors
                ctx["csv_text"] = text if not upload else ""
                ctx["source"] = "CSV upload"
                if rows:
                    _run_analysis(ctx, rows)
        elif action == "tracked":
            rows, warnings = pt.tracked_mf_inputs(request.user)
            ctx["input_errors"] = warnings
            ctx["source"] = "your tracked mutual funds"
            if rows:
                _run_analysis(ctx, [(r["code"], r["amount"]) for r in rows],
                              names={r["code"]: r["name"] for r in rows})
            elif not warnings:
                ctx["input_errors"] = ["You don't have any mutual-fund holdings tracked yet."]
        elif action == "basket":
            target = _float(request, "target_return", 12)
            max_vol = _float(request, "max_vol", 12)
            cats = [c for c in request.POST.getlist("categories") if c in dict(CATEGORIES)] \
                or ["equity", "hybrid", "debt"]
            ctx["basket_form"] = {"target_return": target, "max_vol": max_vol, "categories": cats}
            ctx["basket"] = decorate_basket(pt.curated_basket(target / 100, max_vol / 100, cats))
    if "chart" in ctx:
        ctx["chart_json"] = json.dumps(ctx["chart"])
    return render(request, "portfolio_tools.html", ctx)
