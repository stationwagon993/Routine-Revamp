"""
Marks the single-component refrigerants, which can't have Blend Analysis
requested. More can be marked under Admin → Choice options.
"""

from django.db import migrations, models

SINGLE_COMPONENT = ["R-1233zd(E)", "R-134a", "R-1234ze(E)", "R-22"]


def mark(apps, schema_editor):
    apps.get_model("lab", "ChoiceOption").objects.filter(
        list_name__in=["refrigerant", "plant7_refrigerant"], label__in=SINGLE_COMPONENT
    ).update(single_component=True)


class Migration(migrations.Migration):

    dependencies = [
        ("lab", "0006_intake_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="choiceoption",
            name="single_component",
            field=models.BooleanField(
                default=False,
                help_text="Refrigerant types only: a single-component refrigerant can't have Blend Analysis.",
            ),
        ),
        migrations.RunPython(mark, migrations.RunPython.noop),
    ]
