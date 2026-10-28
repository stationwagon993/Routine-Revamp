import re

from django import forms

from . import formulas
from .models import (
    Blend,
    Calibration,
    ChoiceOption,
    Formula,
    Report,
    Sample,
    TestMethod,
    Verification,
)


OTHER = "__other__"


def _blend_for(refrigerant_type):
    """The blend whose name matches a refrigerant type, ignoring case, spaces and dashes."""
    key = re.sub(r"[^a-z0-9]", "", refrigerant_type.lower())
    if not key:
        return None
    for blend in Blend.objects.filter(active=True):
        if re.sub(r"[^a-z0-9]", "", blend.name.lower()) == key:
            return blend
    return None


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
            "requested_by_email",
            "cc_emails",
            "lto",
            "sample_type",
            "sample_type_other",
            "phase",
            "reason",
            "comments",
            "identification",
            "blend",
        ]
        widgets = {
            "received_on": forms.DateInput(attrs={"type": "date"}),
            "sampled_on": forms.DateInput(attrs={"type": "date"}),
            "sample_type": forms.RadioSelect,
            "phase": forms.RadioSelect,
            "reason": forms.Textarea(attrs={"rows": 2}),
            "comments": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "requested_by_name": "Engineer requesting testing",
            "requested_by_email": "Send report to (email)",
            "lto": "Associated eLTO",
            "phase": "Sample type (refrigerant)",
            "comments": "Any additional pertinent information",
            "blend": "Blend for blend analysis",
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
        self.fields["sampled_on"].required = True
        self.fields["blend"].queryset = Blend.objects.filter(active=True)
        self.fields["blend"].required = False
        self.fields["blend"].help_text = "Picked automatically when the refrigerant type matches a blend."
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
            if area != Sample.COMPRESSOR_GROUP:
                self._require("unit_id", "Enter the unit ID.")
            lubricant = self._pick("lubricant", True, "Choose the lubricant type.")
            self._require("requested_by_name", "Enter the engineer requesting testing.")
            self._require("lto", "Enter the associated eLTO.")
            self._require("reason", "Enter the reason for testing.")
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

        if refrigerant and not cleaned.get("blend"):
            cleaned["blend"] = _blend_for(refrigerant)

        sample.origin = origin
        sample.origin_area = area
        sample.gcs_stand = gcs
        sample.refrigerant_type = refrigerant
        sample.lubricant_type = lubricant
        return cleaned


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
        if any(t.calc_type == TestMethod.CALC_BLEND for t in tests) and not cleaned.get("blend"):
            self.add_error(
                "blend",
                "Choose the blend. If it isn't listed, add it with its components under Admin → Blends.",
            )
        return cleaned


def _kind(cleaned):
    if cleaned.get("sample_type") == Sample.TYPE_REFRIGERANT:
        return cleaned.get("phase") or ""
    return cleaned.get("sample_type") or ""


class CalibrationForm(forms.ModelForm):
    """Calibration header plus standard % and three run areas per blend component."""

    class Meta:
        model = Calibration
        fields = ["standard_cylinder", "performed_on", "notes"]
        widgets = {
            "performed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, blend, **kwargs):
        super().__init__(*args, **kwargs)
        self.blend = blend
        self.component_rows = []
        for component in blend.components.all():
            keys = {
                "percent": f"percent_{component.pk}",
                "run1": f"run1_{component.pk}",
                "run2": f"run2_{component.pk}",
                "run3": f"run3_{component.pk}",
            }
            self.fields[keys["percent"]] = forms.FloatField(
                min_value=0, max_value=100, initial=component.nominal_percent
            )
            for run in ("run1", "run2", "run3"):
                self.fields[keys[run]] = forms.FloatField(min_value=0)
            self.component_rows.append(
                (component, [self[keys[k]] for k in ("percent", "run1", "run2", "run3")])
            )

    def clean(self):
        cleaned = super().clean()
        for component, _ in self.component_rows:
            runs = [cleaned.get(f"run{i}_{component.pk}") for i in (1, 2, 3)]
            if all(r is not None for r in runs) and sum(runs) == 0:
                self.add_error(f"run1_{component.pk}", "Peak areas cannot all be zero.")
        return cleaned

    def component_values(self):
        for component, _ in self.component_rows:
            pk = component.pk
            yield component, {
                "standard_percent": self.cleaned_data[f"percent_{pk}"],
                "area_run1": self.cleaned_data[f"run1_{pk}"],
                "area_run2": self.cleaned_data[f"run2_{pk}"],
                "area_run3": self.cleaned_data[f"run3_{pk}"],
            }


class VerificationForm(forms.ModelForm):
    class Meta:
        model = Verification
        fields = ["standard_cylinder", "performed_on", "notes"]
        widgets = {
            "performed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, calibration, **kwargs):
        super().__init__(*args, **kwargs)
        self.calibration = calibration
        self.fields["standard_cylinder"].initial = calibration.standard_cylinder
        self.component_rows = []
        for cc in calibration.components.select_related("component"):
            self.fields[f"theoretical_{cc.pk}"] = forms.FloatField(
                min_value=0, max_value=100, initial=cc.standard_percent
            )
            self.fields[f"area_{cc.pk}"] = forms.FloatField(min_value=0)
            self.component_rows.append(
                (cc, self[f"theoretical_{cc.pk}"], self[f"area_{cc.pk}"])
            )

    def component_values(self):
        for cc, _, _ in self.component_rows:
            yield cc, {
                "theoretical_percent": self.cleaned_data[f"theoretical_{cc.pk}"],
                "peak_area": self.cleaned_data[f"area_{cc.pk}"],
            }


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

    class Meta:
        model = Formula
        fields = ["expression", "result_unit", "decimals", "notes"]
        widgets = {
            "expression": forms.Textarea(attrs={"rows": 2, "class": "mono"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "expression": (
                "Use the input names with + - * / ** ( ). Functions: "
                + ", ".join(sorted(formulas.FUNCTIONS))
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
        fields = ["recipient_email", "cc_emails", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"recipient_email": "To"}
        help_texts = {"recipient_email": "Separate addresses with ;"}
