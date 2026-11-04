"""
Instruments (GCs). Each has its own calibrations; the standard log is shared.
Existing calibrations belong to the GC7890, the lab's first instrument.
"""

import django.db.models.deletion
from django.db import migrations, models


def assign_gc7890(apps, schema_editor):
    Instrument = apps.get_model("lab", "Instrument")
    gc, _ = Instrument.objects.get_or_create(name="GC7890")
    apps.get_model("lab", "Calibration").objects.filter(instrument__isnull=True).update(instrument=gc)
    apps.get_model("lab", "NCCalibration").objects.filter(instrument__isnull=True).update(instrument=gc)


class Migration(migrations.Migration):

    dependencies = [
        ("lab", "0012_remove_old_error_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="Instrument",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="e.g. GC7890", max_length=50, unique=True
                    ),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                (
                    "active",
                    models.BooleanField(
                        default=True,
                        help_text="Retired instruments are hidden but keep their history.",
                    ),
                ),
                ("order", models.PositiveSmallIntegerField(default=0)),
            ],
            options={
                "ordering": ["order", "name"],
            },
        ),
        migrations.AddField(
            model_name="calibration",
            name="instrument",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="calibrations",
                to="lab.instrument",
            ),
        ),
        migrations.AddField(
            model_name="nccalibration",
            name="instrument",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="nc_calibrations",
                to="lab.instrument",
            ),
        ),
        migrations.RunPython(assign_gc7890, migrations.RunPython.noop),
    ]
