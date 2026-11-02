"""
Verification % error per blend, and pressure-based formulas.

- Blends get an allowed % error; each verification keeps the value it was
  checked against. Older verifications keep the old ±0.03-point rule.
- High Boiling Residue gets separate low- and high/medium-pressure
  formulas. Its existing formulas become the high/medium-pressure ones; the
  low-pressure formula is added on the Formulas page.
"""

from django.conf import settings
from django.db import migrations, models


LOW_PRESSURE = ["R-123", "R-1233zd(E)", "R-1336mzz(Z)", "R-514A", "R-1130(E)", "R-227ea"]


def seed(apps, schema_editor):
    ChoiceOption = apps.get_model("lab", "ChoiceOption")
    for order, label in enumerate(LOW_PRESSURE):
        ChoiceOption.objects.get_or_create(
            list_name="low_pressure", label=label, defaults={"order": order * 10}
        )
    TestMethod = apps.get_model("lab", "TestMethod")
    Formula = apps.get_model("lab", "Formula")
    for method in TestMethod.objects.filter(name="High Boiling Residue"):
        method.formula_by_pressure = True
        method.save()
        Formula.objects.filter(test_method=method, pressure="").update(pressure="high")


class Migration(migrations.Migration):

    dependencies = [
        ("lab", "0007_single_component_refrigerants"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="formula",
            name="unique_formula_version",
        ),
        migrations.AddField(
            model_name="blend",
            name="max_error_percent",
            field=models.FloatField(
                blank=True,
                help_text="A verification passes when every component's % error (|calculated − theoretical| ÷ theoretical × 100) is at or below this.",
                null=True,
                verbose_name="allowed verification error (%)",
            ),
        ),
        migrations.AddField(
            model_name="formula",
            name="pressure",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "All samples"),
                    ("low", "Low-pressure refrigerants"),
                    ("high", "High/medium-pressure refrigerants"),
                ],
                default="",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="testmethod",
            name="formula_by_pressure",
            field=models.BooleanField(
                default=False,
                help_text="One formula for low-pressure refrigerants and one for high/medium-pressure (e.g. HBR).",
                verbose_name="separate formulas by refrigerant pressure",
            ),
        ),
        migrations.AddField(
            model_name="verification",
            name="max_error_percent",
            field=models.FloatField(
                blank=True, editable=False, null=True, verbose_name="allowed error (%)"
            ),
        ),
        migrations.AlterField(
            model_name="choiceoption",
            name="list_name",
            field=models.CharField(
                choices=[
                    ("refrigerant", "Refrigerant types"),
                    ("lubricant", "Lubricant types"),
                    ("plant7_location", "Plant 7 locations"),
                    ("plant7_refrigerant", "Plant 7 refrigerant types"),
                    ("lab11_category", "Lab 11 areas"),
                    ("gcs_stand", "Compressor test stands (GCS)"),
                    (
                        "low_pressure",
                        "Low-pressure refrigerants (pressure-based formulas)",
                    ),
                ],
                max_length=30,
                verbose_name="list",
            ),
        ),
        migrations.AddConstraint(
            model_name="formula",
            constraint=models.UniqueConstraint(
                fields=("test_method", "pressure", "version"),
                name="unique_formula_version",
            ),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
