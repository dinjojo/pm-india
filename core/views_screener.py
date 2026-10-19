import pandas as pd

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from core import screener as screener_engine

PAGE_SIZE = 50


def _clean(v):
    """None -> dash, floats rounded and finite-checked for the template."""
    if v is None or pd.isna(v):
        return None
    return round(float(v) * 100, 1)


class _ScreenerResults:
    """Lazy sequence for Django's Paginator: count() is an exact SQL COUNT and
    slicing runs one LIMIT/OFFSET query, so only the visible page is fetched."""

    def __init__(self, args: tuple):
        self.args = args
        self._count = None

    def count(self) -> int:
        if self._count is None:
            self._count = screener_engine.query_count(*self.args)
        return self._count

    def __len__(self) -> int:
        return self.count()

    def __getitem__(self, s):
        if not isinstance(s, slice):
            raise TypeError("screener results only support slicing")
        start = s.start or 0
        stop = s.stop if s.stop is not None else self.count()
        if stop <= start:
            return []
        df = screener_engine.query(*self.args, limit=max(0, stop - start), offset=start)
        return [r for _, r in df.iterrows()]


@login_required
def screener(request):
    ctx_rows = None
    page = None
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
    match_count = 0
    scheme_count = 0
    page_query = ""
    if submitted:
        args = (category, benchmark, window, outperf_min, upside_min, downside_min,
                "excess" if mode == "excess" else None)
        results = _ScreenerResults(args)
        paginator = Paginator(results, PAGE_SIZE)
        # get_page clamps junk / out-of-range page numbers instead of 404ing
        page = paginator.get_page(request.GET.get("page"))
        match_count = paginator.count
        scheme_count = (screener_engine.query_count(*args, distinct_schemes=True)
                        if match_count else 0)
        rows = []
        for r in page.object_list:
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
        # Current filters minus the page number, for shareable pager links.
        qd = request.GET.copy()
        qd.pop("page", None)
        page_query = qd.urlencode()

    return render(request, "screener.html", {
        "data_as_of": screener_engine.data_as_of(),
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
        "page": page,
        "page_query": page_query,
        "match_count": match_count,
        "scheme_count": scheme_count,
        "submitted": submitted,
    })
