"""Indian currency shorthand: 51514892 -> "₹5.15 cr", 523000 -> "₹5.23 L".

Big plan numbers parse slowly as full rupee strings; the shorthand is the
primary display next to Kuvera/INDmoney conventions. Exact values stay one
hover away via the title attribute in templates that use it.
"""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def inr_short(value):
    try:
        v = abs(float(Decimal(str(value))))
    except (InvalidOperation, ValueError, TypeError):
        return ""
    if v >= 1e7:
        n = v / 1e7
        s = f"{n:,.2f}" if n < 100 else f"{n:,.1f}"
        return f"₹{s} cr"
    if v >= 1e5:
        return f"₹{v / 1e5:,.2f} L"
    if v >= 1e3:
        return f"₹{v / 1e3:,.1f} K"
    return f"₹{v:,.0f}"
