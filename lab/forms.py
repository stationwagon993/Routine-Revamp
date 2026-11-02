import re

from django import forms
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import formulas
from .models import (
    Blend,
    Calibration,
    ChoiceOption,
    Formula,
    NCCalibration,
    NCVerification,
    Report,
    Sample,
    StandardComponent,
    StandardCylinder,
    TestMethod,
    Verification,
)


OTHER = "__other__"


class SampleForm(forms.ModelForm):
    """
    The lab's sample submission form. Which questions apply depends on where the
    sample comes from and what kind of sample it is; the page shows and hides
    them as answers change, and clean() enforces the same rules.
    """

    # Dropdowns that also offer "Other" with a box to type in: (field, choice list, label).
    DROPDOWNS = [
        ("origin", None, "Where does this sample originate?"),
        ("plant7_location", ChoiceOption.PLANT7_LOCATION, "Location (Plant 7)"),
        ("lab11_area", ChoiceOption.LAB11_CATEGORY, "Area (Lab 11)"),
        ("gcs_stand", ChoiceOption.GCS_STAND, "Compressor test stand"),
        ("plant7_refrigerant", ChoiceOption.PLANT7_REFRIGERANT, "Refrigerant type"),
        ("refrigerant", ChoiceOption.REFRIGERANT, "Refrigerant type"),
        ("lubricant", ChoiceOption.LUBRICANT, "Lubricant type"),
    ]

    class Meta:
        model = Sample
        fields = [
            "submitter",
            "received_on",
            "sampled_on",
            "lot_number",
            "unit_id",
            "requested_by_name",
            "lto",
            "sample_type",
            "sample_type_other",
            "phase",
            "comments",
        ]
        widgets = {
            "received_on": forms.DateInput(attrs={"type": "date"}),
            "sampled_on": forms.DateInput(attrs={"type": "date"}),
            "sample_type": forms.RadioSelect,
            "phase": forms.RadioSelect,
            "comments": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "submitter": "Submitted by",
            "requested_by_name": "Engineer requesting testing",
            "lto": "Associated eLTO",
            "phase": "Sample type (refrigerant)",
            "comments": "Any additional pertinent information",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, list_name, label in self.DROPDOWNS:
            options = Sample.ORIGINS if list_name is None else ChoiceOption.labels(list_name)
            self.fields[name] = forms.ChoiceField(
                label=label,
                required=False,
                choices=[("", "—"), *[(o, o) for o in options], (OTHER, "Other")],
            )
            self.fields[name + "_other"] = forms.CharField(
                label="Other (please specify)", required=False, max_length=100
            )
        self.fields["sample_type"].required = False
        self.fields["sample_type"].choices = Sample.TYPE_CHOICES
        self.fields["phase"].choices = Sample.PHASE_CHOICES[1:]
        self.fields["submitter"].required = True
        self.fields["submitter"].help_text = ""
        self.fields["sampled_on"].required = True
        self.fields["lto"].required = True
        self.fields["requested_by_name"].help_text = ""
        if self.instance.pk:
            self._load_dropdowns(self.instance)

    def _load_dropdowns(self, sample):
        """Show a saved sample's answers in the dropdowns (or in their Other boxes)."""
        origin = sample.origin
        values = {
            "origin": origin,
            "plant7_location": sample.origin_area if origin == Sample.ORIGIN_PLANT7 else "",
            "lab11_area": sample.origin_area if origin == Sample.ORIGIN_LAB11 else "",
            "gcs_stand": sample.gcs_stand,
            "plant7_refrigerant": sample.refrigerant_type if origin == Sample.ORIGIN_PLANT7 else "",
            "refrigerant": sample.refrigerant_type if origin != Sample.ORIGIN_PLANT7 else "",
            "lubricant": sample.lubricant_type,
        }
        for name, value in values.items():
            if not value:
                continue
            if value in dict(self.fields[name].choices):
                self.initial[name] = value
            else:
                self.initial[name] = OTHER
                self.initial[name + "_other"] = value

    def _pick(self, name, required, message):
        """The answer to a dropdown, using the typed-in text when Other is chosen."""
        value = self.cleaned_data.get(name) or ""
        if value == OTHER:
            value = (self.cleaned_data.get(name + "_other") or "").strip()
            if not value:
                self.add_error(name + "_other", "Please specify.")
                return ""
        if required and not value:
            self.add_error(name, message)
        return value

    def _require(self, field, message):
        value = self.cleaned_data.get(field)
        if not value:
            self.add_error(field, message)
        return value

    def clean(self):
        cleaned = super().clean()
        sample = self.instance
        origin = self._pick("origin", True, "Choose where the sample originates.")
        area = gcs = refrigerant = lubricant = ""

        if origin == Sample.ORIGIN_NEW_TANK:
            self._require("lot_number", "Enter the lot #.")
            refrigerant = self._pick("refrigerant", True, "Choose the refrigerant type.")
            cleaned["sample_type"] = Sample.TYPE_REFRIGERANT
            cleaned["unit_id"] = ""
        elif origin == Sample.ORIGIN_NEW_DRUM:
            self._require("lot_number", "Enter the lot #.")
            lubricant = self._pick("lubricant", True, "Choose the lubricant type.")
            cleaned["sample_type"] = Sample.TYPE_OIL
            cleaned["unit_id"] = ""
        elif origin:
            cleaned["lot_number"] = ""
            if origin == Sample.ORIGIN_PLANT7:
                area = self._pick("plant7_location", True, "Choose the Plant 7 location.")
                refrigerant = self._pick("plant7_refrigerant", True, "Choose the refrigerant type.")
            else:
                refrigerant = self._pick("refrigerant", True, "Choose the refrigerant type.")
            if origin == Sample.ORIGIN_LAB11:
                area = self._pick("lab11_area", True, "Choose the Lab 11 area.")
                if area == Sample.COMPRESSOR_GROUP:
                    gcs = self._pick("gcs_stand", True, "Choose the compressor test stand.")
            if area == Sample.COMPRESSOR_GROUP:
                cleaned["unit_id"] = ""  # Compressor samples are identified by their test stand.
            else:
                self._require("unit_id", "Enter the unit ID.")
            lubricant = self._pick("lubricant", True, "Choose the lubricant type.")
            self._require("requested_by_name", "Enter the engineer requesting testing.")
            self._require("sample_type", "Choose the sample type.")

        sample_type = cleaned.get("sample_type")
        if sample_type == Sample.TYPE_REFRIGERANT:
            self._require("phase", "Choose liquid, vapor or expanded liquid.")
        else:
            cleaned["phase"] = ""
        if sample_type == Sample.TYPE_OTHER:
            self._require("sample_type_other", "Describe the sample type.")
        else:
            cleaned["sample_type_other"] = ""

        sample.origin = origin
        sample.origin_area = area
        sample.gcs_stand = gcs
        sample.refrigerant_type = refrigerant
        sample.lubricant_type = lubricant
        # Blend analysis is always of the refrigerant entered above. If that blend isn't set up
        # yet the sample is still logged; it picks the blend up once it's added in Admin.
        sample.blend = Blend.matching(refrigerant)
        return cleaned

    def save(self, commit=True):
        sample = super().save(commit=False)
        sample.identification = sample.build_identification()[:200]
        if commit:
            sample.save()
        return sample


class SampleIntakeForm(SampleForm):
    tests = forms.ModelMultipleChoiceField(
        queryset=TestMethod.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tests"].queryset = TestMethod.objects.filter(active=True, on_every_sample=False)

    def clean(self):
        cleaned = super().clean()
        tests = cleaned.get("tests") or []
        kind = _kind(cleaned)
        if not tests:
            self.add_error("tests", "Choose at least one test.")
        elif kind:
            wrong = [t.name for t in tests if not t.applies(kind)]
            if wrong:
                self.add_error("tests", "Not available for this sample type: " + ", ".join(wrong) + ".")
        blend_requested = any(t.calc_type == TestMethod.CALC_BLEND for t in tests)
        refrigerant = self.instance.refrigerant_type
        if blend_requested and ChoiceOption.name_key(refrigerant) in ChoiceOption.single_component_keys():
            self.add_error("tests", f"{refrigerant} is a single-component refrigerant: Blend Analysis can't be requested.")
        return cleaned


def _kind(cleaned):
    if cleaned.get("sample_type") == Sample.TYPE_REFRIGERANT:
        return cleaned.get("phase") or ""
    return cleaned.get("sample_type") or ""


class StandardCylinderForm(forms.ModelForm):
    """A gravimetric standard: the mass of each component, as in the GC workbook's Standard Log."""

    class Meta:
        model = StandardCylinder
        fields = ["code", "prepared_on", "status", "cylinder_gross_g", "cylinder_tare_g", "full_fill_g", "notes"]
        widgets = {
            "prepared_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, blend, **kwargs):
        super().__init__(*args, **kwargs)
        self.blend = blend
        masses = {}
        if self.instance.pk:
            masses = {c.component_id: c.mass_g for c in self.instance.components.all()}
        self.mass_rows = []
        for component in blend.components.all():
            name = f"mass_{component.pk}"
            self.fields[name] = forms.FloatField(min_value=0, initial=masses.get(component.pk))
            self.mass_rows.append((component, self[name]))

    def clean(self):
        cleaned = super().clean()
        masses = [cleaned.get(f"mass_{c.pk}") for c, _ in self.mass_rows]
        if all(m is not None for m in masses) and sum(masses) <= 0:
            raise forms.ValidationError("Enter the mass of each component.")
        return cleaned

    @transaction.atomic
    def save(self, user=None):
        standard = super().save(commit=False)
        standard.blend = self.blend
        if user is not None and standard.prepared_by_id is None:
            standard.prepared_by = user
        standard.save()
        for component, _ in self.mass_rows:
            StandardComponent.objects.update_or_create(
                cylinder=standard,
                component=component,
                defaults={"mass_g": self.cleaned_data[f"mass_{component.pk}"]},
            )
        return standard


def _standard_choices(blend, on=None):
    """Standards of the blend that are on hand and not past their 7 days."""
    on = on or timezone.localdate()
    ids = [s.pk for s in blend.standards.filter(status=StandardCylinder.STATUS_ON_HAND) if s.is_usable(on)]
    return StandardCylinder.objects.filter(pk__in=ids)


def _check_standard(form, field, standard, performed_on):
    if standard and performed_on and not standard.is_usable(performed_on):
        form.add_error(
            field,
            f"{standard} can't be used on {performed_on:%m/%d/%Y}: standards are good for "
            f"{settings.STANDARD_VALID_DAYS} days after they're prepared and must be on hand.",
        )


class CalibrationForm(forms.ModelForm):
    """Three runs of a gravimetric standard; its composition comes from the standard log."""

    standard = forms.ModelChoiceField(queryset=StandardCylinder.objects.none(), label="Calibration standard")

    class Meta:
        model = Calibration
        fields = ["standard", "performed_on", "notes"]
        widgets = {
            "performed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, blend, **kwargs):
        super().__init__(*args, **kwargs)
        self.blend = blend
        self.fields["standard"].queryset = blend.standards.all()
        self.fields["standard"].help_text = "From the standard log. Must be on hand and within 7 days of preparation."
        self.usable_standards = _standard_choices(blend)
        self.component_rows = []
        for component in blend.components.all():
            names = [f"run{i}_{component.pk}" for i in (1, 2, 3)]
            for name in names:
                self.fields[name] = forms.FloatField(min_value=0)
            self.component_rows.append((component, [self[n] for n in names]))

    def clean(self):
        cleaned = super().clean()
        standard = cleaned.get("standard")
        _check_standard(self, "standard", standard, cleaned.get("performed_on"))
        if standard:
            composition = standard.composition()
            missing = [c.name for c, _ in self.component_rows if c.pk not in composition]
            if missing:
                self.add_error("standard", f"{standard} has no mass for {', '.join(missing)}.")
        for component, _ in self.component_rows:
            runs = [cleaned.get(f"run{i}_{component.pk}") for i in (1, 2, 3)]
            if all(r is not None for r in runs) and sum(runs) == 0:
                self.add_error(f"run1_{component.pk}", "Peak areas cannot all be zero.")
        return cleaned

    def save(self, commit=True):
        calibration = super().save(commit=False)
        calibration.standard_cylinder = self.cleaned_data["standard"].code
        if commit:
            calibration.save()
        return calibration

    def component_values(self):
        composition = self.cleaned_data["standard"].composition()
        for component, _ in self.component_rows:
            pk = component.pk
            yield component, {
                "standard_percent": composition[pk],
                "area_run1": self.cleaned_data[f"run1_{pk}"],
                "area_run2": self.cleaned_data[f"run2_{pk}"],
                "area_run3": self.cleaned_data[f"run3_{pk}"],
            }


class VerificationForm(forms.ModelForm):
    """One run of a second standard; it passes when every component is within the blend's limit."""

    standard = forms.ModelChoiceField(queryset=StandardCylinder.objects.none(), label="Verification standard")
    verification_rule = forms.ChoiceField(
        choices=[(Blend.RULE_POINTS, "± percentage points"), (Blend.RULE_PERCENT, "% error (relative)")],
        label="Pass rule",
    )
    verification_limit = forms.FloatField(
        min_value=0, label="Allowed error", help_text="Saved for the blend and used for its future verifications."
    )

    class Meta:
        model = Verification
        fields = ["standard", "performed_on", "purity", "air_area", "air_rt", "co2_area", "co2_rt", "notes"]
        widgets = {
            "performed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, calibration, **kwargs):
        super().__init__(*args, **kwargs)
        self.calibration = calibration
        blend = calibration.blend
        self.fields["standard"].queryset = blend.standards.all()
        self.fields["standard"].help_text = "Normally a different standard from the calibration's."
        self.usable_standards = _standard_choices(blend)
        self.fields["verification_rule"].initial = blend.verification_rule
        self.fields["verification_limit"].initial = blend.verification_limit
        self.component_rows = []
        for cc in calibration.components.select_related("component"):
            self.fields[f"area_{cc.pk}"] = forms.FloatField(min_value=0)
            self.fields[f"rt_{cc.pk}"] = forms.FloatField(min_value=0, required=False)
            self.component_rows.append((cc, self[f"area_{cc.pk}"], self[f"rt_{cc.pk}"]))

    def clean(self):
        cleaned = super().clean()
        standard = cleaned.get("standard")
        _check_standard(self, "standard", standard, cleaned.get("performed_on"))
        if standard:
            composition = standard.composition()
            missing = [cc.component.name for cc, _, _ in self.component_rows if cc.component_id not in composition]
            if missing:
                self.add_error("standard", f"{standard} has no mass for {', '.join(missing)}.")
        return cleaned

    def component_values(self):
        composition = self.cleaned_data["standard"].composition()
        for cc, _, _ in self.component_rows:
            yield cc, {
                "theoretical_percent": composition[cc.component_id],
                "peak_area": self.cleaned_data[f"area_{cc.pk}"],
                "retention_time": self.cleaned_data.get(f"rt_{cc.pk}"),
            }


def _area_fields(form, prefix, count, required):
    names = []
    for i in range(1, count + 1):
        name = f"{prefix}{i}"
        form.fields[name] = forms.FloatField(min_value=0, required=False, label=f"Replicate {i}")
        names.append(name)
    form.area_names = names
    form.area_required = required


def _clean_areas(form, cleaned):
    areas = [cleaned.get(n) for n in form.area_names if cleaned.get(n) is not None]
    if len(areas) < form.area_required:
        raise forms.ValidationError(f"Enter at least {form.area_required} replicate peak areas.")
    if sum(areas) <= 0:
        raise forms.ValidationError("The peak areas add up to zero.")
    return areas


class NCCalibrationForm(forms.ModelForm):
    class Meta:
        model = NCCalibration
        fields = ["performed_on", "standard_name", "lot_number", "n2_percent", "baseline_air_peak", "notes"]
        widgets = {
            "performed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {"n2_percent": "e.g. 1.48 for a 1.48% N₂ standard."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["n2_percent"].min_value = 0.000001
        _area_fields(self, "area_", 10, 3)

    def clean(self):
        cleaned = super().clean()
        self.instance.areas = _clean_areas(self, cleaned)
        return cleaned


class NCVerificationForm(forms.ModelForm):
    class Meta:
        model = NCVerification
        fields = ["performed_on", "standard_name", "lot_number", "n2_percent", "max_error_percent", "notes"]
        widgets = {
            "performed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "max_error_percent": "Passes when |result − standard| ÷ standard × 100 is at or below this.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["n2_percent"].min_value = 0.000001
        _area_fields(self, "area_", 4, 2)

    def clean(self):
        cleaned = super().clean()
        self.instance.areas = _clean_areas(self, cleaned)
        return cleaned


_INPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FormulaForm(forms.ModelForm):
    inputs_text = forms.CharField(
        label="Inputs",
        widget=forms.Textarea(attrs={"rows": 5, "class": "mono"}),
        help_text=(
            "One input per line: <code>name | label | unit</code>. "
            "Names may contain letters, numbers and underscores, e.g. "
            "<code>mass_g | Sample mass | g</code>."
        ),
    )
    both_pressures = forms.BooleanField(
        label="Use this formula for both pressures",
        required=False,
        help_text="Untick to change only this pressure's formula.",
    )

    class Meta:
        model = Formula
        fields = ["expression", "result_unit", "decimals", "report_limit", "notes"]
        widgets = {
            "expression": forms.Textarea(attrs={"rows": 2, "class": "mono"}),
            "report_limit": forms.TextInput(attrs={"class": "mono"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "expression": (
                "Use the input names with + - * / ** ( ). Functions: "
                + ", ".join(sorted(formulas.FUNCTIONS))
                + ". From the sample (no input needed): "
                + ", ".join(f"<code>{n}</code> ({d})" for n, d in formulas.SAMPLE_VARIABLES.items())
                + "."
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.inputs:
            self.fields["inputs_text"].initial = "\n".join(
                " | ".join([i["name"], i.get("label", ""), i.get("unit", "")])
                for i in self.instance.inputs
            )

    def clean_inputs_text(self):
        inputs = []
        for line_no, line in enumerate(self.cleaned_data["inputs_text"].splitlines(), start=1):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("|")]
            name = parts[0]
            if not _INPUT_NAME.match(name):
                raise forms.ValidationError(f"Line {line_no}: '{name}' is not a valid input name.")
            if name in {i["name"] for i in inputs}:
                raise forms.ValidationError(f"Line {line_no}: '{name}' is listed twice.")
            inputs.append(
                {
                    "name": name,
                    "label": parts[1] if len(parts) > 1 and parts[1] else name,
                    "unit": parts[2] if len(parts) > 2 else "",
                }
            )
        if not inputs:
            raise forms.ValidationError("Add at least one input.")
        return inputs

    def clean(self):
        cleaned = super().clean()
        # The model's own validation checks the expression against these inputs.
        self.instance.inputs = cleaned.get("inputs_text") or []
        return cleaned


class ReportForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ["recipient_email", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"recipient_email": "To"}
        help_texts = {
            "recipient_email": "The engineer's name (Outlook looks up the address). Add anyone to CC in Outlook."
        }
