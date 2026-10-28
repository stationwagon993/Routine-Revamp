from django.conf import settings
from django.utils import timezone

from .models import julian_code


def lab_settings(request):
    return {"LAB_NAME": settings.LAB_NAME, "TODAY_JULIAN": julian_code(timezone.localdate())}
