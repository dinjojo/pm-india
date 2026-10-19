"""{% browse_catalog request as b %} — catalog/filter/pagination context for
templates/funds.html. The /funds/ route is served by core.views.funds (another
worker's file), so the browse state is built here from request.GET."""
from django import template

from core.funds import browse_context

register = template.Library()


@register.simple_tag
def browse_catalog(request):
    return browse_context(request.GET)
