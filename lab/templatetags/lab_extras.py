from django import template

register = template.Library()


@register.filter
def get(mapping, key):
    """{{ mydict|get:key }} – dictionary lookup with a variable key."""
    if mapping is None:
        return None
    return mapping.get(key)


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
def person(user):
    """A user's full name, falling back to their username; blank for None."""
    if user is None:
        return ""
    return user.get_full_name() or user.username
