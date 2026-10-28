import re

from django import forms

from . import formulas
from .models import Blend, Calibration, Formula, Report, Sample, TestMethod, Verification


class SampleForm(forms.ModelForm):
    class Meta:
        model = Sample
        fields = [
            "received_on",
            "sample_type",
            "phase",
            "blend",
            "identification",
            "source",
            "lto",
            "requested_by_name",
            "requested_by_email",
            "cc_emails",
            "comments",
        ]
        widgets = {
            "received_on": forms.DateInput(attrs={"type": "date"}),
            "comments": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["blend"].queryset = Blend.objects.filter(active=True)
        self.fields["blend"].help_text = "Required for blend analysis."


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
        if any(t.calc_type == TestMethod.CALC_BLEND for t in tests) and not cleaned.get("blend"):
            self.add_error("blend", "Choose the blend for blend analysis.")
        return cleaned


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
