from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import (
    Blend,
    BlendComponent,
    Calibration,
    CalibrationComponent,
    Formula,
    Profile,
    Report,
    Sample,
    SampleTest,
    TestMethod,
    TestResult,
    Verification,
    VerificationComponent,
)


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False


class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(TestMethod)
class TestMethodAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "group", "calc_type", "display_order", "on_every_sample", "active"]
    list_editable = ["code", "display_order", "active"]
    list_filter = ["group", "calc_type", "active"]


@admin.register(Formula)
class FormulaAdmin(admin.ModelAdmin):
    """Formulas are versioned: create new versions from the Formulas page, not here."""

    list_display = ["test_method", "version", "expression", "result_unit", "is_placeholder", "active"]
    list_filter = ["test_method", "is_placeholder", "active"]
    readonly_fields = [
        "test_method",
        "version",
        "expression",
        "inputs",
        "result_unit",
        "decimals",
        "created_by",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False


class BlendComponentInline(admin.TabularInline):
    model = BlendComponent
    extra = 3


@admin.register(Blend)
class BlendAdmin(admin.ModelAdmin):
    list_display = ["name", "description", "active"]
    inlines = [BlendComponentInline]


class CalibrationComponentInline(admin.TabularInline):
    model = CalibrationComponent
    extra = 0


@admin.register(Calibration)
class CalibrationAdmin(admin.ModelAdmin):
    list_display = ["blend", "performed_on", "expires_on", "performed_by", "standard_cylinder"]
    list_filter = ["blend"]
    inlines = [CalibrationComponentInline]


class VerificationComponentInline(admin.TabularInline):
    model = VerificationComponent
    extra = 0


@admin.register(Verification)
class VerificationAdmin(admin.ModelAdmin):
    list_display = ["calibration", "performed_on", "performed_by"]
    inlines = [VerificationComponentInline]


class SampleTestInline(admin.TabularInline):
    model = SampleTest
    extra = 0
    fields = ["test_method", "status", "completed_by", "completed_at"]


@admin.register(Sample)
class SampleAdmin(admin.ModelAdmin):
    list_display = ["sample_number", "identification", "lto", "received_on"]
    search_fields = ["sample_number", "identification", "lto"]
    inlines = [SampleTestInline]


@admin.register(TestResult)
class TestResultAdmin(admin.ModelAdmin):
    list_display = ["sample_test", "label", "value", "text_value", "unit", "entered_by", "entered_at"]


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["report_number", "status", "created_by", "created_at", "sent_at"]
    filter_horizontal = ["samples"]
