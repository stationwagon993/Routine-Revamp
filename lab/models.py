import calendar
import datetime

from django.conf import settings
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from . import formulas


def add_months(day, months):
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(day.day, last_day))


def julian_code(day):
    """Lab Julian date: two-digit year + day of year, e.g. 2026-07-07 -> '26188'."""
    return f"{day:%y}{day.timetuple().tm_yday:03d}"


def user_initials(user):
    if user is None:
        return ""
    profile = getattr(user, "lab_profile", None)
    if profile and profile.initials:
        return profile.initials
    parts = [user.first_name, user.last_name]
    initials = "".join(p[0] for p in parts if p)
    return (initials or user.username[:2]).upper()


class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lab_profile"
    )
    initials = models.CharField(
        max_length=5, blank=True, help_text="Shown in the sample log when a test is completed."
    )

    def __str__(self):
        return f"{self.user} ({self.initials})"


# --- Tests and calculations -------------------------------------------------


class TestMethod(models.Model):
    GROUP_GENERAL = "general"
    GROUP_REFRIGERANT = "refrigerant"
    GROUP_OIL = "oil"
    GROUP_CHOICES = [
        (GROUP_GENERAL, "General"),
        (GROUP_REFRIGERANT, "Refrigerant"),
        (GROUP_OIL, "Oil"),
    ]

    CALC_TRACKING = "tracking"
    CALC_MANUAL = "manual"
    CALC_FORMULA = "formula"
    CALC_BLEND = "blend"
    CALC_CHOICES = [
        (CALC_TRACKING, "Status only (no result)"),
        (CALC_MANUAL, "Result typed in directly"),
        (CALC_FORMULA, "Calculated from a formula"),
        (CALC_BLEND, "Blend analysis from the active calibration"),
    ]

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField("method code", max_length=30, blank=True, help_text="e.g. ESIS1102")
    group = models.CharField(max_length=20, choices=GROUP_CHOICES)
    calc_type = models.CharField("result type", max_length=20, choices=CALC_CHOICES)
    unit = models.CharField(
        max_length=30, blank=True, help_text="Unit for typed-in results (formulas set their own)."
    )
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    on_every_sample = models.BooleanField(
        default=False, help_text="Added automatically to every new sample (e.g. Results Reported)."
    )
    applies_to = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Sample kinds this test is offered for, comma separated: liquid, vapor, expanded, oil. "
            "Blank = offered for every sample."
        ),
    )

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return f"{self.name} ({self.code})" if self.code else self.name

    def applies(self, kind):
        """Whether the intake form offers this test for a sample kind (see Sample.kind)."""
        if not self.applies_to or kind == Sample.TYPE_OTHER:
            return True
        return kind in {k.strip() for k in self.applies_to.split(",")}

    @property
    def has_results(self):
        return self.calc_type != self.CALC_TRACKING

    def current_formula(self):
        return self.formulas.filter(active=True).order_by("-version").first()


class Formula(models.Model):
    """
    One version of a test's calculation. Formulas are never edited in place:
    changing a calculation creates a new version, so old results always point
    at the exact formula that produced them.
    """

    test_method = models.ForeignKey(TestMethod, on_delete=models.PROTECT, related_name="formulas")
    version = models.PositiveIntegerField()
    expression = models.TextField()
    # [{"name": "mass_g", "label": "Sample mass", "unit": "g"}, ...]
    inputs = models.JSONField(default=list)
    result_unit = models.CharField(max_length=30, blank=True)
    decimals = models.PositiveSmallIntegerField(default=2)
    notes = models.TextField(blank=True)
    is_placeholder = models.BooleanField(
        default=False, help_text="Example formula that has not been confirmed by the lab."
    )
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["test_method", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["test_method", "version"], name="unique_formula_version")
        ]

    def __str__(self):
        return f"{self.test_method.name} v{self.version}"

    @property
    def input_names(self):
        return [item["name"] for item in self.inputs]

    def clean(self):
        from django.core.exceptions import ValidationError

        try:
            formulas.validate(self.expression, self.input_names)
        except formulas.FormulaError as exc:
            raise ValidationError({"expression": str(exc)}) from exc

    def calculate(self, values):
        return formulas.evaluate(self.expression, {n: values.get(n) for n in self.input_names})


# --- Blends, calibrations, verifications ------------------------------------


class Blend(models.Model):
    name = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def active_calibration(self, on=None):
        """Latest unexpired calibration of this blend with a passing verification."""
        on = on or timezone.localdate()
        candidates = self.calibrations.filter(performed_on__lte=on, expires_on__gte=on).order_by(
            "-performed_on", "-id"
        )
        for calibration in candidates:
            if calibration.is_verified:
                return calibration
        return None


class BlendComponent(models.Model):
    blend = models.ForeignKey(Blend, on_delete=models.CASCADE, related_name="components")
    name = models.CharField(max_length=50)
    nominal_percent = models.FloatField(help_text="Nominal composition, mass %.")
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["blend", "name"], name="unique_blend_component")
        ]

    def __str__(self):
        return f"{self.blend.name}: {self.name}"


class Calibration(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_UNVERIFIED = "unverified"
    STATUS_FAILED = "failed"
    STATUS_EXPIRED = "expired"

    blend = models.ForeignKey(Blend, on_delete=models.PROTECT, related_name="calibrations")
    standard_cylinder = models.CharField(
        max_length=50, help_text="Standard cylinder ID (from the inventory system)."
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="calibrations"
    )
    performed_on = models.DateField(default=timezone.localdate)
    expires_on = models.DateField(editable=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_on", "-id"]

    def __str__(self):
        return f"{self.blend} calibration {self.performed_on:%Y-%m-%d}"

    def save(self, *args, **kwargs):
        self.expires_on = add_months(self.performed_on, settings.CALIBRATION_VALID_MONTHS)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("lab:calibration_detail", args=[self.pk])

    @property
    def is_expired(self):
        return self.expires_on < timezone.localdate()

    @property
    def latest_verification(self):
        return self.verifications.order_by("-performed_on", "-id").first()

    @property
    def is_verified(self):
        latest = self.latest_verification
        return latest is not None and latest.passed

    @property
    def status(self):
        if self.is_expired:
            return self.STATUS_EXPIRED
        latest = self.latest_verification
        if latest is None:
            return self.STATUS_UNVERIFIED
        return self.STATUS_ACTIVE if latest.passed else self.STATUS_FAILED

    @property
    def days_until_expiry(self):
        return (self.expires_on - timezone.localdate()).days


class CalibrationComponent(models.Model):
    """Peak areas for one component across the three calibration runs."""

    calibration = models.ForeignKey(Calibration, on_delete=models.CASCADE, related_name="components")
    component = models.ForeignKey(BlendComponent, on_delete=models.PROTECT)
    standard_percent = models.FloatField(help_text="Composition of the standard, mass %.")
    area_run1 = models.FloatField()
    area_run2 = models.FloatField()
    area_run3 = models.FloatField()

    class Meta:
        ordering = ["component__order", "component__name"]

    def __str__(self):
        return f"{self.calibration}: {self.component.name}"

    @property
    def mean_area(self):
        return (self.area_run1 + self.area_run2 + self.area_run3) / 3

    @property
    def response_factor(self):
        """Mass % per unit of peak area."""
        return self.standard_percent / self.mean_area


class Verification(models.Model):
    calibration = models.ForeignKey(Calibration, on_delete=models.CASCADE, related_name="verifications")
    standard_cylinder = models.CharField(max_length=50, blank=True)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    performed_on = models.DateField(default=timezone.localdate)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_on", "-id"]

    def __str__(self):
        return f"Verification of {self.calibration} on {self.performed_on:%Y-%m-%d}"

    @property
    def passed(self):
        components = list(self.components.all())
        return bool(components) and all(c.passed for c in components)


class VerificationComponent(models.Model):
    verification = models.ForeignKey(Verification, on_delete=models.CASCADE, related_name="components")
    calibration_component = models.ForeignKey(CalibrationComponent, on_delete=models.PROTECT)
    theoretical_percent = models.FloatField()
    peak_area = models.FloatField()

    class Meta:
        ordering = ["calibration_component__component__order"]

    @property
    def calculated_percent(self):
        return self.peak_area * self.calibration_component.response_factor

    @property
    def difference(self):
        return self.calculated_percent - self.theoretical_percent

    @property
    def passed(self):
        # Small epsilon so a difference of exactly the tolerance is not failed by float rounding.
        return abs(self.difference) <= settings.VERIFICATION_TOLERANCE + 1e-9


# --- Intake form dropdowns ---------------------------------------------------


class ChoiceOption(models.Model):
    """One entry in an intake-form dropdown. "Other" is always offered as well."""

    REFRIGERANT = "refrigerant"
    LUBRICANT = "lubricant"
    PLANT7_LOCATION = "plant7_location"
    PLANT7_REFRIGERANT = "plant7_refrigerant"
    LAB11_CATEGORY = "lab11_category"
    GCS_STAND = "gcs_stand"
    LIST_CHOICES = [
        (REFRIGERANT, "Refrigerant types"),
        (LUBRICANT, "Lubricant types"),
        (PLANT7_LOCATION, "Plant 7 locations"),
        (PLANT7_REFRIGERANT, "Plant 7 refrigerant types"),
        (LAB11_CATEGORY, "Lab 11 areas"),
        (GCS_STAND, "Compressor test stands (GCS)"),
    ]

    list_name = models.CharField("list", max_length=30, choices=LIST_CHOICES)
    label = models.CharField(max_length=100)
    short_label = models.CharField(
        max_length=100, blank=True, help_text="Used in the sample identification instead of the label."
    )
    order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["list_name", "order", "label"]
        constraints = [
            models.UniqueConstraint(fields=["list_name", "label"], name="unique_choice_option")
        ]

    def __str__(self):
        return self.label

    @classmethod
    def labels(cls, list_name):
        return list(
            cls.objects.filter(list_name=list_name, active=True).values_list("label", flat=True)
        )

    @classmethod
    def short(cls, list_name, label):
        option = cls.objects.filter(list_name=list_name, label=label).first()
        return (option.short_label or option.label) if option else label


# --- Samples ----------------------------------------------------------------


class Sample(models.Model):
    TYPE_REFRIGERANT = "refrigerant"
    TYPE_OIL = "oil"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_REFRIGERANT, "Refrigerant"),
        (TYPE_OIL, "Oil"),
        (TYPE_OTHER, "Other"),
    ]
    PHASE_CHOICES = [
        ("", "—"),
        ("liquid", "Liquid"),
        ("vapor", "Vapor"),
        ("expanded", "Expanded Liquid"),
    ]

    ORIGIN_PLANT4 = "Plant 4"
    ORIGIN_PLANT7 = "Plant 7"
    ORIGIN_LAB11 = "Lab 11"
    ORIGIN_NEW_TANK = "New Refrigerant Tank"
    ORIGIN_NEW_DRUM = "New Lubricant Drum"
    ORIGINS = [ORIGIN_PLANT4, ORIGIN_PLANT7, ORIGIN_LAB11, ORIGIN_NEW_TANK, ORIGIN_NEW_DRUM]
    # Lab 11 area that is asked for its compressor test stand.
    COMPRESSOR_GROUP = "Compressor Group"

    sample_number = models.CharField(max_length=20, unique=True, editable=False)
    julian = models.CharField(max_length=5, editable=False)
    sequence = models.PositiveIntegerField(editable=False)
    received_on = models.DateField(default=timezone.localdate)
    identification = models.CharField(
        max_length=200,
        blank=True,
        help_text="Leave blank to build it from the answers, e.g. Compressors, GCS 4, Refrigerant, Liquid.",
    )
    submitter = models.CharField(max_length=100, blank=True)
    origin = models.CharField("where the sample originates", max_length=100, blank=True)
    origin_area = models.CharField(
        "area", max_length=100, blank=True, help_text="Plant 7 location or Lab 11 area."
    )
    gcs_stand = models.CharField("compressor test stand", max_length=50, blank=True)
    unit_id = models.CharField("unit ID", max_length=100, blank=True)
    lot_number = models.CharField("lot #", max_length=50, blank=True)
    refrigerant_type = models.CharField(max_length=50, blank=True)
    lubricant_type = models.CharField(max_length=50, blank=True)
    sample_type_other = models.CharField("other sample type", max_length=100, blank=True)
    reason = models.TextField("reason for testing", blank=True)
    sampled_on = models.DateField("sampled date", null=True, blank=True)
    lto = models.CharField("LTO #", max_length=30, blank=True)
    sample_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    phase = models.CharField(max_length=10, choices=PHASE_CHOICES, blank=True)
    blend = models.ForeignKey(Blend, null=True, blank=True, on_delete=models.PROTECT)
    source = models.CharField(max_length=200, blank=True, help_text="Where the sample came from.")
    requested_by_name = models.CharField("requested by", max_length=100, blank=True)
    requested_by_email = models.EmailField("requester email", blank=True)
    cc_emails = models.CharField(
        "CC emails", max_length=500, blank=True, help_text="Separate addresses with ;"
    )
    comments = models.TextField(blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-julian", "-sequence"]

    def __str__(self):
        return self.sample_number

    def get_absolute_url(self):
        return reverse("lab:sample_detail", args=[self.pk])

    @property
    def kind(self):
        """liquid / vapor / expanded / oil / other: decides which tests can be requested."""
        if self.sample_type == self.TYPE_REFRIGERANT:
            return self.phase
        return self.sample_type

    def build_identification(self):
        """Sample log text in the lab's usual style, built from the intake answers."""
        type_label = self.sample_type_other or self.get_sample_type_display()
        if self.origin == self.ORIGIN_NEW_TANK:
            parts = [self.origin, f"Lot {self.lot_number}", self.refrigerant_type, self.get_phase_display()]
        elif self.origin == self.ORIGIN_NEW_DRUM:
            parts = [self.origin, f"Lot {self.lot_number}", self.lubricant_type, "Oil"]
        else:
            if self.origin == self.ORIGIN_LAB11:
                parts = [ChoiceOption.short(ChoiceOption.LAB11_CATEGORY, self.origin_area)]
            elif self.origin == self.ORIGIN_PLANT7:
                parts = [self.origin, ChoiceOption.short(ChoiceOption.PLANT7_LOCATION, self.origin_area)]
            else:
                parts = [self.origin]
            parts += [self.gcs_stand, self.unit_id, type_label]
            if self.sample_type == self.TYPE_REFRIGERANT:
                parts.append(self.get_phase_display())
        return ", ".join(p for p in parts if p and p != "—")

    def save(self, *args, **kwargs):
        if not self.identification:
            self.identification = self.build_identification()[:200]
        if not self.sample_number:
            with transaction.atomic():
                self.julian = julian_code(self.received_on)
                last = (
                    Sample.objects.filter(julian=self.julian)
                    .order_by("-sequence")
                    .values_list("sequence", flat=True)
                    .first()
                )
                self.sequence = (last or 0) + 1
                self.sample_number = f"{self.julian}-{self.sequence}"
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return any(t.is_open for t in self.tests.all())


class SampleTest(models.Model):
    STATUS_REQUESTED = "requested"
    STATUS_NEEDS_COMPILING = "needs_compiling"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Analysis requested"),
        (STATUS_NEEDS_COMPILING, "Test run, data needs compiling"),
        (STATUS_COMPLETED, "Testing completed"),
        (STATUS_CANCELLED, "Testing cancelled"),
    ]
    OPEN_STATUSES = {STATUS_REQUESTED, STATUS_NEEDS_COMPILING}

    sample = models.ForeignKey(Sample, on_delete=models.CASCADE, related_name="tests")
    test_method = models.ForeignKey(TestMethod, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    # Calibration used for blend analysis results.
    calibration = models.ForeignKey(Calibration, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["test_method__display_order"]
        constraints = [
            models.UniqueConstraint(fields=["sample", "test_method"], name="unique_sample_test")
        ]

    def __str__(self):
        return f"{self.sample} – {self.test_method.name}"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def cell_text(self):
        """What the sample log cell shows, matching the old spreadsheet."""
        if self.status == self.STATUS_COMPLETED:
            return user_initials(self.completed_by)
        if self.status == self.STATUS_REQUESTED:
            return "-"
        return ""

    def set_status(self, status, user):
        self.status = status
        if status == self.STATUS_COMPLETED:
            self.completed_by = user
            self.completed_at = timezone.now()
        else:
            self.completed_by = None
            self.completed_at = None
        self.save()


class TestResult(models.Model):
    sample_test = models.ForeignKey(SampleTest, on_delete=models.CASCADE, related_name="results")
    label = models.CharField(max_length=100, blank=True, help_text="Component name for blend results.")
    order = models.PositiveSmallIntegerField(default=0)
    value = models.FloatField(null=True, blank=True)
    text_value = models.CharField(max_length=200, blank=True, help_text="For non-numeric results.")
    unit = models.CharField(max_length=30, blank=True)
    decimals = models.PositiveSmallIntegerField(default=2)
    inputs = models.JSONField(default=dict, blank=True)
    formula = models.ForeignKey(Formula, null=True, blank=True, on_delete=models.PROTECT)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    entered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.sample_test}: {self.display_value}"

    @property
    def display_value(self):
        if self.value is None:
            return self.text_value
        return f"{self.value:.{self.decimals}f}"


# --- Reports ----------------------------------------------------------------


class Report(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_GENERATED = "generated"
    STATUS_SENT = "sent"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_GENERATED, "PDF generated"),
        (STATUS_SENT, "Sent"),
    ]

    report_number = models.CharField(max_length=30, unique=True, editable=False)
    samples = models.ManyToManyField(Sample, related_name="reports")
    recipient_email = models.CharField(max_length=500, blank=True)
    cc_emails = models.CharField("CC emails", max_length=500, blank=True)
    notes = models.TextField(blank=True, help_text="Printed on the report.")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    pdf_path = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    generated_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.report_number

    def get_absolute_url(self):
        return reverse("lab:report_detail", args=[self.pk])

    @classmethod
    def next_number(cls, first_sample):
        base = f"R{first_sample.sample_number}"
        number, n = base, 1
        while cls.objects.filter(report_number=number).exists():
            n += 1
            number = f"{base}-{n}"
        return number

    def ordered_samples(self):
        return self.samples.order_by("julian", "sequence")

    def uses_placeholder_formula(self):
        return TestResult.objects.filter(
            sample_test__sample__in=self.samples.all(), formula__is_placeholder=True
        ).exists()
