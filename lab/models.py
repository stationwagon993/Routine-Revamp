import calendar
import datetime
import re
import statistics

from django.conf import settings
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

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
    CALC_NC = "nc"
    CALC_CHOICES = [
        (CALC_TRACKING, "Status only (no result)"),
        (CALC_MANUAL, "Result typed in directly"),
        (CALC_FORMULA, "Calculated from a formula"),
        (CALC_BLEND, "Blend analysis from the active calibration"),
        (CALC_NC, "Non-condensables from the active NC calibration"),
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

    def current_formula(self, pressure="high"):
        """The latest formula for samples of that refrigerant pressure (see Sample.pressure)."""
        return self.formulas.filter(active=True, pressure=pressure).order_by("-version").first()

    def pressure_formulas(self):
        """[(pressure, label, current formula)] for the Formulas page."""
        return [(p, label, self.current_formula(p)) for p, label in Formula.PRESSURE_CHOICES]


class Formula(models.Model):
    """
    One version of a test's calculation. Formulas are never edited in place:
    changing a calculation creates a new version, so old results always point
    at the exact formula that produced them.
    """

    PRESSURE_LOW = "low"
    PRESSURE_HIGH = "high"
    # Every calculated test has a formula for each; they may well be the same.
    PRESSURE_CHOICES = [
        (PRESSURE_HIGH, "High/medium-pressure refrigerants"),
        (PRESSURE_LOW, "Low-pressure refrigerants"),
    ]

    test_method = models.ForeignKey(TestMethod, on_delete=models.PROTECT, related_name="formulas")
    pressure = models.CharField(max_length=10, choices=PRESSURE_CHOICES, default=PRESSURE_HIGH)
    version = models.PositiveIntegerField()
    expression = models.TextField()
    # [{"name": "mass_g", "label": "Sample mass", "unit": "g"}, ...]
    inputs = models.JSONField(default=list)
    result_unit = models.CharField(max_length=30, blank=True)
    decimals = models.PositiveSmallIntegerField(default=2)
    report_limit = models.CharField(
        "reporting limit",
        max_length=200,
        blank=True,
        help_text="Results at or below this are reported as “&lt;limit”. A number, or a formula "
        "using the inputs, e.g. <code>0.05 if mass_g &lt; 5 else 0.01</code>.",
    )
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
            models.UniqueConstraint(
                fields=["test_method", "pressure", "version"], name="unique_formula_version"
            )
        ]

    def __str__(self):
        return f"{self.test_method.name} ({self.get_pressure_display()}) v{self.version}"

    def same_calculation(self, other):
        fields = ("expression", "inputs", "result_unit", "decimals", "report_limit")
        return other is not None and all(getattr(self, f) == getattr(other, f) for f in fields)

    @property
    def sample_variables(self):
        """Values the formula takes from the sample rather than from typed inputs."""
        return sorted(formulas.names_used(self.expression) & set(formulas.SAMPLE_VARIABLES))

    @property
    def input_names(self):
        return [item["name"] for item in self.inputs]

    def clean(self):
        from django.core.exceptions import ValidationError

        allowed = [*self.input_names, *formulas.SAMPLE_VARIABLES]
        try:
            formulas.validate(self.expression, allowed)
        except formulas.FormulaError as exc:
            raise ValidationError({"expression": str(exc)}) from exc
        if self.report_limit.strip():
            try:
                formulas.validate(self.report_limit, allowed)
            except formulas.FormulaError as exc:
                raise ValidationError({"report_limit": str(exc)}) from exc

    def _values(self, values, sample_values):
        merged = {n: values.get(n) for n in self.input_names}
        needed = formulas.names_used(self.expression) | formulas.names_used(self.report_limit)
        for name in needed & set(formulas.SAMPLE_VARIABLES):
            merged[name] = (sample_values or {}).get(name)
        return merged

    def calculate(self, values, sample_values=None):
        return formulas.evaluate(self.expression, self._values(values, sample_values))

    def reporting_limit(self, values, sample_values=None):
        """The reporting limit for these inputs, or None."""
        if not self.report_limit.strip():
            return None
        return formulas.evaluate(self.report_limit, self._values(values, sample_values))


# --- Blends, calibrations, verifications ------------------------------------


class Instrument(models.Model):
    """A GC. Each one has its own calibrations; the standard log is shared between them."""

    name = models.CharField(max_length=50, unique=True, help_text="e.g. GC7890")
    description = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True, help_text="Retired instruments are hidden but keep their history.")
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Blend(models.Model):
    RULE_POINTS = "points"
    RULE_PERCENT = "percent"
    RULE_CHOICES = [
        (RULE_POINTS, "± percentage points (the GC workbook's “Ref Error”)"),
        (RULE_PERCENT, "% error (|calculated − theoretical| ÷ theoretical × 100)"),
    ]

    name = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    verification_rule = models.CharField(max_length=10, choices=RULE_CHOICES, default=RULE_POINTS)
    verification_limit = models.FloatField(
        "allowed verification error",
        null=True,
        blank=True,
        help_text="Each component must be within this of the standard: percentage points "
        "(e.g. 0.3049) or % error, depending on the rule.",
    )
    verification_valid_days = models.PositiveSmallIntegerField(
        default=7, help_text="How long a passing verification lets the calibration be used."
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def limit_display(self):
        if self.verification_limit is None:
            return ""
        return format_limit(self.verification_rule, self.verification_limit)

    @classmethod
    def matching(cls, refrigerant_type):
        """The active blend named like a refrigerant type (R-454B = r454b), or None."""
        key = ChoiceOption.name_key(refrigerant_type)
        if not key:
            return None
        for blend in cls.objects.filter(active=True):
            if ChoiceOption.name_key(blend.name) == key:
                return blend
        return None

    def active_calibration(self, on=None, instrument=None):
        """
        Latest unexpired calibration whose latest verification passed and is still current,
        on the given instrument (or on any active instrument).
        """
        on = on or timezone.localdate()
        candidates = self.calibrations.filter(
            performed_on__lte=on, expires_on__gte=on, instrument__active=True
        ).order_by("-performed_on", "-id")
        if instrument is not None:
            candidates = candidates.filter(instrument=instrument)
        for calibration in candidates:
            if calibration.is_usable(on):
                return calibration
        return None


def format_limit(rule, limit):
    if rule == Blend.RULE_PERCENT:
        return f"{limit:g}% error"
    return f"±{limit:g} points"


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


def rsd_percent(values):
    """Relative standard deviation (sample), % – Excel's STDEV ÷ AVERAGE × 100."""
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    return statistics.stdev(values) / mean * 100 if mean else None


# --- Standard cylinders --------------------------------------------------------


class StandardCylinder(models.Model):
    """
    A gravimetric standard (the Standard Log on each blend sheet of the GC
    workbook): composition = each component's mass ÷ the total mass.
    """

    STATUS_ON_HAND = "on_hand"
    STATUS_USED = "used"
    STATUS_DISPOSED = "disposed"
    STATUS_CHOICES = [
        (STATUS_ON_HAND, "On hand – in rack"),
        (STATUS_USED, "Disposed – used for cal/ver"),
        (STATUS_DISPOSED, "Disposed"),
    ]
    REPLACE_BELOW = 0.6  # flag for replacement below 60% remaining

    blend = models.ForeignKey(Blend, on_delete=models.PROTECT, related_name="standards")
    code = models.CharField("std. code", max_length=50, unique=True, help_text="e.g. R-454B-080626-A")
    prepared_on = models.DateField("date prepared", default=timezone.localdate)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ON_HAND)
    cylinder_gross_g = models.FloatField("cylinder gross (g)", null=True, blank=True)
    cylinder_tare_g = models.FloatField("cylinder tare (g)", null=True, blank=True)
    full_fill_g = models.FloatField(
        "full fill (g)",
        null=True,
        blank=True,
        help_text="Refrigerant mass when the cylinder is full, for the remaining-refrigerant check.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-prepared_on", "-id"]

    def __str__(self):
        return self.code

    def get_absolute_url(self):
        return reverse("lab:standard_edit", args=[self.pk])

    @property
    def expires_on(self):
        return self.prepared_on + datetime.timedelta(days=settings.STANDARD_VALID_DAYS)

    def is_usable(self, on=None):
        on = on or timezone.localdate()
        return self.status == self.STATUS_ON_HAND and self.prepared_on <= on <= self.expires_on

    def composition(self):
        """{BlendComponent id: mass %}."""
        rows = list(self.components.all())
        total = sum(r.mass_g for r in rows)
        return {r.component_id: (r.mass_g / total * 100 if total else 0.0) for r in rows}

    @property
    def remaining_fraction(self):
        if None in (self.cylinder_gross_g, self.cylinder_tare_g) or not self.full_fill_g:
            return None
        return (self.cylinder_gross_g - self.cylinder_tare_g) / self.full_fill_g

    @property
    def needs_replacing(self):
        remaining = self.remaining_fraction
        return remaining is not None and remaining < self.REPLACE_BELOW


class StandardComponent(models.Model):
    cylinder = models.ForeignKey(StandardCylinder, on_delete=models.CASCADE, related_name="components")
    component = models.ForeignKey(BlendComponent, on_delete=models.PROTECT)
    mass_g = models.FloatField("mass (g)")

    class Meta:
        ordering = ["component__order", "component__name"]
        constraints = [
            models.UniqueConstraint(fields=["cylinder", "component"], name="unique_standard_component")
        ]


# --- Calibrations ----------------------------------------------------------------


class Calibration(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_UNVERIFIED = "unverified"
    STATUS_FAILED = "failed"
    STATUS_REVERIFY = "reverify"
    STATUS_EXPIRED = "expired"
    STATUS_LABELS = {
        STATUS_ACTIVE: "Active",
        STATUS_UNVERIFIED: "Not verified",
        STATUS_FAILED: "Verification failed",
        STATUS_REVERIFY: "Verification expired",
        STATUS_EXPIRED: "Expired",
    }

    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="calibrations")
    blend = models.ForeignKey(Blend, on_delete=models.PROTECT, related_name="calibrations")
    standard = models.ForeignKey(
        StandardCylinder, null=True, blank=True, on_delete=models.PROTECT, related_name="calibrations"
    )
    standard_cylinder = models.CharField(
        max_length=50, help_text="Standard cylinder code (filled in from the standard)."
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
        return f"{self.blend} calibration on {self.instrument} {self.performed_on:%Y-%m-%d}"

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

    def is_usable(self, on=None):
        """Not expired, and the latest verification passed and is still current."""
        on = on or timezone.localdate()
        latest = self.latest_verification
        return (
            self.performed_on <= on <= self.expires_on
            and latest is not None
            and latest.passed
            and latest.is_current(on)
        )

    @property
    def status(self):
        if self.is_expired:
            return self.STATUS_EXPIRED
        latest = self.latest_verification
        if latest is None:
            return self.STATUS_UNVERIFIED
        if not latest.passed:
            return self.STATUS_FAILED
        if not latest.is_current():
            return self.STATUS_REVERIFY
        return self.STATUS_ACTIVE

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.status]

    @property
    def days_until_expiry(self):
        return (self.expires_on - timezone.localdate()).days

    def component_rows(self):
        components = list(self.components.select_related("component"))
        reference = components[-1].response_factor if components else None
        for c in components:
            c.relative_response_factor = c.response_factor / reference if reference else None
        return components

    def replicate_compositions(self):
        """Each run's areas worked out as a composition with this calibration: [[%, ...] per run]."""
        components = list(self.components.all())
        runs = []
        for field in ("area_run1", "area_run2", "area_run3"):
            raw = [getattr(c, field) * c.response_factor for c in components]
            try:
                runs.append(normalized_percents(raw))
            except ValueError:
                runs.append([0.0] * len(raw))
        return runs


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
    def areas(self):
        return [self.area_run1, self.area_run2, self.area_run3]

    @property
    def mean_area(self):
        return sum(self.areas) / 3

    @property
    def rsd_percent(self):
        return rsd_percent(self.areas)

    @property
    def response_factor(self):
        """Mass % per unit of peak area (the workbook's ARF, × 100)."""
        return self.standard_percent / self.mean_area


def normalized_percents(raw):
    """Scale raw component percents so they add up to 100."""
    total = sum(raw)
    if total <= 0:
        raise ValueError("The peak areas add up to zero.")
    return [r / total * 100 for r in raw]


class Verification(models.Model):
    calibration = models.ForeignKey(Calibration, on_delete=models.CASCADE, related_name="verifications")
    standard = models.ForeignKey(
        StandardCylinder, null=True, blank=True, on_delete=models.PROTECT, related_name="verifications"
    )
    # The blend's rule and limit when this verification was run, so changing them later doesn't
    # change past results. Empty on verifications from before rules were stored: those keep the
    # original rule (unnormalized, within ±VERIFICATION_TOLERANCE points).
    rule = models.CharField(max_length=10, choices=Blend.RULE_CHOICES, blank=True, editable=False)
    limit = models.FloatField(null=True, blank=True, editable=False)
    expires_on = models.DateField(null=True, blank=True, editable=False)
    standard_cylinder = models.CharField(max_length=50, blank=True)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    performed_on = models.DateField(default=timezone.localdate)
    purity = models.FloatField("purity (%)", null=True, blank=True, help_text="Recorded only.")
    air_area = models.FloatField("air area (µV)", null=True, blank=True)
    air_rt = models.FloatField("air RT (min)", null=True, blank=True)
    co2_area = models.FloatField("CO₂ area (µV)", null=True, blank=True)
    co2_rt = models.FloatField("CO₂ RT (min)", null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_on", "-id"]

    def __str__(self):
        return f"Verification of {self.calibration} on {self.performed_on:%Y-%m-%d}"

    def save(self, *args, **kwargs):
        if self.expires_on is None:
            days = self.calibration.blend.verification_valid_days
            self.expires_on = self.performed_on + datetime.timedelta(days=days)
        super().save(*args, **kwargs)

    def is_current(self, on=None):
        on = on or timezone.localdate()
        return self.expires_on is None or on <= self.expires_on

    @property
    def is_legacy(self):
        return not self.rule

    @property
    def limit_display(self):
        if self.is_legacy:
            return f"±{settings.VERIFICATION_TOLERANCE:g} points, not normalized (older verification)"
        return format_limit(self.rule, self.limit)

    @cached_property
    def calculated_percents(self):
        """Calculated % per VerificationComponent id."""
        components = list(self.components.select_related("calibration_component"))
        raw = [c.raw_percent for c in components]
        if not self.is_legacy:
            try:
                raw = normalized_percents(raw)
            except ValueError:
                raw = [0.0] * len(raw)
        return {c.pk: value for c, value in zip(components, raw)}

    def component_rows(self):
        """Components linked back to this object, so their % are worked out once."""
        components = list(self.components.all())
        for c in components:
            c.verification = self
        return components

    @property
    def passed(self):
        components = self.component_rows()
        return bool(components) and all(c.passed for c in components)


class VerificationComponent(models.Model):
    verification = models.ForeignKey(Verification, on_delete=models.CASCADE, related_name="components")
    calibration_component = models.ForeignKey(CalibrationComponent, on_delete=models.PROTECT)
    theoretical_percent = models.FloatField()
    peak_area = models.FloatField()
    retention_time = models.FloatField("RT (min)", null=True, blank=True)

    class Meta:
        ordering = ["calibration_component__component__order"]

    @property
    def raw_percent(self):
        return self.peak_area * self.calibration_component.response_factor

    @property
    def calculated_percent(self):
        """Normalized so the components add up to 100% (old verifications: unnormalized)."""
        return self.verification.calculated_percents[self.pk]

    @property
    def difference(self):
        return self.calculated_percent - self.theoretical_percent

    @property
    def percent_error(self):
        if self.theoretical_percent == 0:
            return 0.0 if self.calculated_percent == 0 else float("inf")
        return abs(self.difference) / self.theoretical_percent * 100

    @property
    def passed(self):
        # Small epsilon so an error of exactly the limit is not failed by float rounding.
        v = self.verification
        if v.is_legacy:
            return abs(self.difference) <= settings.VERIFICATION_TOLERANCE + 1e-9
        if v.rule == Blend.RULE_PERCENT:
            return self.percent_error <= v.limit + 1e-9
        return abs(self.difference) <= v.limit + 1e-9


# --- Non-condensables ------------------------------------------------------------


class NCCalibration(models.Model):
    """
    Non-condensables (the GC workbook's NC sheet): replicate injections of a
    nitrogen-in-helium standard give the peak area for 1.00% N2.
    """

    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="nc_calibrations")
    performed_on = models.DateField(default=timezone.localdate)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    standard_name = models.CharField("standard used", max_length=100, default="Nitrogen in Helium")
    lot_number = models.CharField("lot #", max_length=50, blank=True)
    n2_percent = models.FloatField("% nitrogen in standard")
    areas = models.JSONField(default=list, help_text="Replicate peak areas (µV).")
    baseline_air_peak = models.FloatField("baseline air peak (µV)", null=True, blank=True)
    expires_on = models.DateField(editable=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_on", "-id"]

    def __str__(self):
        return f"Non-condensables calibration on {self.instrument} {self.performed_on:%Y-%m-%d}"

    def save(self, *args, **kwargs):
        self.expires_on = add_months(self.performed_on, settings.CALIBRATION_VALID_MONTHS)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("lab:nc_calibration_detail", args=[self.pk])

    @property
    def mean_area(self):
        return statistics.mean(self.areas)

    @property
    def rsd_percent(self):
        return rsd_percent(self.areas)

    @property
    def area_per_percent(self):
        """Peak area for 1.00% N2."""
        return self.mean_area / self.n2_percent

    def result_percent(self, areas):
        return statistics.mean(areas) / self.area_per_percent

    @property
    def latest_verification(self):
        return self.verifications.order_by("-performed_on", "-id").first()

    @property
    def is_expired(self):
        return self.expires_on < timezone.localdate()

    def is_usable(self, on=None):
        on = on or timezone.localdate()
        latest = self.latest_verification
        return (
            self.performed_on <= on <= self.expires_on
            and latest is not None
            and latest.passed
            and latest.is_current(on)
        )

    @property
    def status(self):
        if self.is_expired:
            return Calibration.STATUS_EXPIRED
        latest = self.latest_verification
        if latest is None:
            return Calibration.STATUS_UNVERIFIED
        if not latest.passed:
            return Calibration.STATUS_FAILED
        if not latest.is_current():
            return Calibration.STATUS_REVERIFY
        return Calibration.STATUS_ACTIVE

    @property
    def status_label(self):
        return Calibration.STATUS_LABELS[self.status]

    @classmethod
    def active(cls, on=None, instrument=None):
        """The usable calibration on the given instrument (or on any active instrument)."""
        on = on or timezone.localdate()
        candidates = cls.objects.filter(performed_on__lte=on, expires_on__gte=on, instrument__active=True)
        if instrument is not None:
            candidates = candidates.filter(instrument=instrument)
        for calibration in candidates:
            if calibration.is_usable(on):
                return calibration
        return None


class NCVerification(models.Model):
    calibration = models.ForeignKey(NCCalibration, on_delete=models.CASCADE, related_name="verifications")
    performed_on = models.DateField(default=timezone.localdate)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    standard_name = models.CharField("standard used", max_length=100, default="Nitrogen in Helium")
    lot_number = models.CharField("lot #", max_length=50, blank=True)
    n2_percent = models.FloatField("% nitrogen in standard")
    areas = models.JSONField(default=list, help_text="Replicate peak areas (µV).")
    max_error_percent = models.FloatField("allowed % error")
    expires_on = models.DateField(editable=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_on", "-id"]

    def save(self, *args, **kwargs):
        self.expires_on = self.performed_on + datetime.timedelta(days=settings.NC_VERIFICATION_VALID_DAYS)
        super().save(*args, **kwargs)

    def is_current(self, on=None):
        return (on or timezone.localdate()) <= self.expires_on

    @property
    def result_percent(self):
        return self.calibration.result_percent(self.areas)

    @property
    def percent_error(self):
        """(result − standard) ÷ standard × 100, as in the workbook (signed)."""
        return (self.result_percent - self.n2_percent) / self.n2_percent * 100

    @property
    def rsd_percent(self):
        return rsd_percent(self.areas)

    @property
    def passed(self):
        return abs(self.percent_error) <= self.max_error_percent + 1e-9


# --- Refrigerant properties ------------------------------------------------------


class RefrigerantDensity(models.Model):
    """Liquid densities used by formulas as ``refrigerant_density`` (the Master sheet's HBR Data)."""

    refrigerant = models.CharField(max_length=50, unique=True)
    density_g_cc = models.FloatField("density (g/cc)")

    class Meta:
        ordering = ["refrigerant"]
        verbose_name_plural = "refrigerant densities"

    def __str__(self):
        return f"{self.refrigerant}: {self.density_g_cc} g/cc"

    @classmethod
    def lookup(cls, refrigerant_type):
        key = ChoiceOption.name_key(refrigerant_type)
        if not key:
            return None
        for row in cls.objects.all():
            if ChoiceOption.name_key(row.refrigerant) == key:
                return row.density_g_cc
        return None


# --- Intake form dropdowns ---------------------------------------------------


class ChoiceOption(models.Model):
    """One entry in an intake-form dropdown. "Other" is always offered as well."""

    REFRIGERANT = "refrigerant"
    LUBRICANT = "lubricant"
    PLANT7_LOCATION = "plant7_location"
    PLANT7_REFRIGERANT = "plant7_refrigerant"
    LAB11_CATEGORY = "lab11_category"
    GCS_STAND = "gcs_stand"
    # Not a dropdown: the refrigerants that use the low-pressure formula (e.g. HBR).
    LOW_PRESSURE = "low_pressure"
    LIST_CHOICES = [
        (REFRIGERANT, "Refrigerant types"),
        (LUBRICANT, "Lubricant types"),
        (PLANT7_LOCATION, "Plant 7 locations"),
        (PLANT7_REFRIGERANT, "Plant 7 refrigerant types"),
        (LAB11_CATEGORY, "Lab 11 areas"),
        (GCS_STAND, "Compressor test stands (GCS)"),
        (LOW_PRESSURE, "Low-pressure refrigerants (pressure-based formulas)"),
    ]

    list_name = models.CharField("list", max_length=30, choices=LIST_CHOICES)
    label = models.CharField(max_length=100)
    short_label = models.CharField(
        max_length=100, blank=True, help_text="Used in the sample identification instead of the label."
    )
    order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    single_component = models.BooleanField(
        default=False,
        help_text="Refrigerant types only: a single-component refrigerant can't have Blend Analysis.",
    )

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

    @staticmethod
    def name_key(name):
        """Refrigerant names compared ignoring case, spaces, dashes and brackets: R-1233zd(E) = r1233zde."""
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())

    @classmethod
    def is_low_pressure(cls, refrigerant_type):
        key = cls.name_key(refrigerant_type)
        labels = cls.objects.filter(list_name=cls.LOW_PRESSURE, active=True).values_list("label", flat=True)
        return bool(key) and key in {cls.name_key(label) for label in labels}

    @classmethod
    def single_component_keys(cls):
        names = cls.objects.filter(
            list_name__in=[cls.REFRIGERANT, cls.PLANT7_REFRIGERANT], single_component=True
        ).values_list("label", flat=True)
        return {cls.name_key(n) for n in names}

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
    RECLAIMED_TANK = "Reclaimed Refrigerant Tank"

    sample_number = models.CharField(max_length=20, unique=True, editable=False)
    julian = models.CharField(max_length=5, editable=False)
    sequence = models.PositiveIntegerField(editable=False)
    received_on = models.DateField(default=timezone.localdate)
    identification = models.CharField(
        max_length=200,
        blank=True,
        help_text="Built from the intake answers, e.g. Compressors, GCS 4, Refrigerant, Liquid.",
    )
    submitter = models.CharField(
        max_length=100, blank=True, help_text="The tech from the testing facility who brought the sample."
    )
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
    def pressure(self):
        """Which formula a pressure-dependent test uses: low or high (high/medium) pressure."""
        if ChoiceOption.is_low_pressure(self.refrigerant_type):
            return Formula.PRESSURE_LOW
        return Formula.PRESSURE_HIGH

    @property
    def kind(self):
        """liquid / vapor / expanded / oil / other: decides which tests can be requested."""
        if self.sample_type == self.TYPE_REFRIGERANT:
            return self.phase
        return self.sample_type

    def build_identification(self):
        """Sample log text in the lab's usual style, built from the intake answers."""
        # Refrigerant and lubricant have their own sample log columns, so they are left out here.
        type_label = self.sample_type_other or self.get_sample_type_display()
        phase = self.get_phase_display() if self.sample_type == self.TYPE_REFRIGERANT else ""
        if self.origin == self.ORIGIN_NEW_TANK:
            parts = [self.origin, f"Lot {self.lot_number}", phase]
        elif self.origin == self.ORIGIN_NEW_DRUM:
            parts = [self.origin, f"Lot {self.lot_number}", "Oil"]
        elif self.origin == self.ORIGIN_PLANT7:
            # The Excel log lists Plant 7 samples by location only, e.g. "Plant 7, Bay B South".
            parts = [self.origin, ChoiceOption.short(ChoiceOption.PLANT7_LOCATION, self.origin_area)]
        elif self.origin == self.ORIGIN_LAB11 and self.origin_area == self.RECLAIMED_TANK:
            # "Reclaimed Tank, Tank 014323, Liquid"
            tank = self.unit_id
            if tank and not tank.lower().startswith("tank"):
                tank = f"Tank {tank}"
            parts = [ChoiceOption.short(ChoiceOption.LAB11_CATEGORY, self.origin_area), tank]
            parts += [phase] if phase else [type_label]
        else:
            if self.origin == self.ORIGIN_LAB11:
                parts = [ChoiceOption.short(ChoiceOption.LAB11_CATEGORY, self.origin_area)]
            else:
                parts = [self.origin]
            # "Compressors, GCS 12, Refrigerant, Liquid" / "Unitary, E137, Refrigerant, Liquid" / "Compressors, GCS 3, Oil"
            parts += [self.gcs_stand, self.unit_id, type_label, phase]
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
    nc_calibration = models.ForeignKey(NCCalibration, null=True, blank=True, on_delete=models.PROTECT)

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
    text_value = models.CharField(
        max_length=200, blank=True, help_text="Non-numeric results, or what is reported, e.g. <0.01."
    )
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
        if self.text_value or self.value is None:
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
