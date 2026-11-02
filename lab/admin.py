from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import (
    Blend,
    BlendComponent,
    Calibration,
    CalibrationComponent,
    ChoiceOption,
    Formula,
    NCCalibration,
    NCVerification,
    Profile,
    RefrigerantDensity,
    Report,
    Sample,
    SampleTest,
    StandardComponent,
    StandardCylinder,
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
    list_display = ["name", "code", "group", "calc_type", "applies_to", "display_order", "on_every_sample", "active"]
    list_editable = ["code", "applies_to", "display_order", "active"]
    list_filter = ["group", "calc_type", "active"]


@admin.register(Formula)
class FormulaAdmin(admin.ModelAdmin):
    """Formulas are versioned: create new versions from the Formulas page, not here."""

    list_display = ["test_method", "pressure", "version", "expression", "result_unit", "is_placeholder", "active"]
    list_filter = ["test_method", "pressure", "is_placeholder", "active"]
    readonly_fields = [
        "test_method",
        "pressure",
        "version",
        "expression",
        "inputs",
        "result_unit",
        "decimals",
        "report_limit",
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
    list_display = ["name", "verification_rule", "verification_limit", "verification_valid_days", "active"]
    list_editable = ["verification_rule", "verification_limit", "verification_valid_days"]
    inlines = [BlendComponentInline]


class StandardComponentInline(admin.TabularInline):
    model = StandardComponent
    extra = 0


@admin.register(StandardCylinder)
class StandardCylinderAdmin(admin.ModelAdmin):
    list_display = ["code", "blend", "prepared_on", "status"]
    list_filter = ["blend", "status"]
    search_fields = ["code"]
    inlines = [StandardComponentInline]


@admin.register(RefrigerantDensity)
class RefrigerantDensityAdmin(admin.ModelAdmin):
    list_display = ["refrigerant", "density_g_cc"]
    list_editable = ["density_g_cc"]
    search_fields = ["refrigerant"]


class NCVerificationInline(admin.TabularInline):
    model = NCVerification
    extra = 0


@admin.register(NCCalibration)
class NCCalibrationAdmin(admin.ModelAdmin):
    list_display = ["performed_on", "performed_by", "n2_percent", "expires_on"]
    inlines = [NCVerificationInline]


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


@admin.register(ChoiceOption)
class ChoiceOptionAdmin(admin.ModelAdmin):
    """Dropdown choices on the sample submission form."""

    list_display = ["label", "list_name", "short_label", "order", "single_component", "active"]
    list_editable = ["short_label", "order", "single_component", "active"]
    list_filter = ["list_name", "active"]
    search_fields = ["label"]
