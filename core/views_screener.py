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

    def _min(param: str, default: float | None) -> float | None:
        v = request.GET.get(param)
        if v is None or v == "":
            return default
        try:
            return max(0.0, min(1.0, float(v)))
        except ValueError:
            return default

    outperf_min = _min("outperf_min", 0.6)
    upside_min = _min("upside_min", None)
    downside_min = _min("downside_min", None)

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
        "outperf_min": "" if outperf_min is None else outperf_min,
        "upside_min": "" if upside_min is None else upside_min,
        "downside_min": "" if downside_min is None else downside_min,
        "rows": ctx_rows,
        "match_count": len(ctx_rows) if ctx_rows is not None else 0,
        "submitted": submitted,
    })
