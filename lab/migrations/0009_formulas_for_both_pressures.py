"""
Every calculated test gets a high/medium-pressure and a low-pressure formula.

Formulas that applied to all samples become the high/medium-pressure ones,
and the current one is copied as the low-pressure formula, so results are
calculated exactly as before until the lab changes either one. Tests that
already have a low-pressure formula (HBR) are left as they are.
"""

from django.db import migrations, models


def split(apps, schema_editor):
    Formula = apps.get_model("lab", "Formula")
    Formula.objects.filter(pressure="").update(pressure="high")
    method_ids = Formula.objects.values_list("test_method_id", flat=True).distinct()
    for method_id in method_ids:
        formulas = Formula.objects.filter(test_method_id=method_id)
        if formulas.filter(pressure="low").exists():
            continue
        current = formulas.filter(pressure="high", active=True).order_by("-version").first()
        if current is None:
            continue
        Formula.objects.create(
            test_method_id=method_id,
            pressure="low",
            version=1,
            expression=current.expression,
            inputs=current.inputs,
            result_unit=current.result_unit,
            decimals=current.decimals,
            notes=f"Copied from the high/medium-pressure formula (v{current.version}).",
            is_placeholder=current.is_placeholder,
            created_by_id=current.created_by_id,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("lab", "0008_verification_error_and_pressure_formulas"),
    ]

    operations = [
        migrations.RunPython(split, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="testmethod",
            name="formula_by_pressure",
        ),
        migrations.AlterField(
            model_name="formula",
            name="pressure",
            field=models.CharField(
                choices=[
                    ("high", "High/medium-pressure refrigerants"),
                    ("low", "Low-pressure refrigerants"),
                ],
                default="high",
                max_length=10,
            ),
        ),
    ]
