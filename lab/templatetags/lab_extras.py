from pathlib import Path

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def asset(path):
    """
    Static file URL that changes whenever the file does, so browsers load the
    new version after an update instead of a stale cached copy.
    """
    url = static(path)
    found = finders.find(path)
    if found:
        url += f"?v={int(Path(found).stat().st_mtime)}"
    return url


@register.filter
def get(mapping, key):
    """{{ mydict|get:key }} – dictionary lookup with a variable key."""
    if mapping is None:
        return None
    return mapping.get(key)


@register.filter
def getfield(form, name):
    """{{ form|getfield:name }} – a form's bound field by a variable name."""
    return form[name]


@register.filter
def num(value, decimals=4):
    """Format a float without trailing noise; blank for None."""
    if value is None:
        return ""
    return f"{value:.{int(decimals)}f}"


@register.filter
def signed(value, decimals=4):
    if value is None:
        return ""
    return f"{value:+.{int(decimals)}f}"


@register.filter
def percent(fraction, decimals=0):
    """0.642 -> "64%"; blank for None."""
    if fraction is None:
        return ""
    return f"{fraction * 100:.{int(decimals)}f}%"


@register.filter
def person(user):
    """A user's full name, falling back to their username; blank for None."""
    if user is None:
        return ""
    return user.get_full_name() or user.username
