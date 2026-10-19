import math

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core import screener as screener_engine


def _clean(v):
    """None -> dash, floats rounded and finite-checked for the template."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v) * 100, 1)


@login_required
def screener(request):
    ctx_rows = None
    categories = list(screener_engine.BENCHMARK_MAP.keys())
    benchmarks = [{"key": k, "label": v} for k, v in screener_engine.BENCHMARK_LABELS.items()]
    windows = list(screener_engine.WINDOWS.values())

    category = request.GET.get("category") or None
    benchmark = request.GET.get("benchmark") or None
    window = request.GET.get("window") if request.GET.get("window") in screener_engine.WINDOWS.values() else "3Y"
    mode = "excess" if request.GET.get("mode") == "excess" else "consistency"
    try:
        outperf_min = max(0.0, min(1.0, float(request.GET.get("outperf_min", "0.7"))))
        upside_min = max(0.0, min(1.0, float(request.GET.get("upside_min", "0.7"))))
        downside_min = max(0.0, min(1.0, float(request.GET.get("downside_min", "0.7"))))
    except ValueError:
        outperf_min = upside_min = downside_min = 0.7

    submitted = request.GET.get("go") is not None
    if submitted:
        df = screener_engine.query(category, benchmark, window,
                            outperf_min, upside_min, downside_min,
                            "excess" if mode == "excess" else None)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "code": int(r["scheme_code"]),
                "name": r["scheme_name"],
                "category": r["category"],
                "benchmark": screener_engine.BENCHMARK_LABELS.get(r["benchmark"], r["benchmark"]),
                "n": int(r.get(f"n{window}") or 0),
                "ro": _clean(r.get(f"ro{window}")),
                "up": _clean(r.get(f"up{window}")),
                "dc": _clean(r.get(f"dc{window}")),
                "tr": _clean(r.get(f"tr{window}")),
                "tb": _clean(r.get(f"tb{window}")),
                "ex": _clean(r.get(f"ex{window}")),
            })
        ctx_rows = rows

    return render(request, "screener.html", {
        "categories": categories,
        "benchmarks": benchmarks,
        "windows": windows,
        "sel_category": category or "",
        "sel_benchmark": benchmark or "",
        "sel_window": window,
        "sel_mode": mode,
        "outperf_min": outperf_min,
        "upside_min": upside_min,
        "downside_min": downside_min,
        "rows": ctx_rows,
        "submitted": submitted,
    })
