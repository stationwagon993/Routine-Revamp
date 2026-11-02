"""
"Miscellaneous" and "Test Plan Submitted" are no longer offered on the intake
form or shown in the sample log. Samples that already have them keep them.
"""

from django.db import migrations

RETIRED = ["Miscellaneous", "Test Plan Submitted"]


def retire(apps, schema_editor):
    apps.get_model("lab", "TestMethod").objects.filter(name__in=RETIRED).update(active=False)


def restore(apps, schema_editor):
    apps.get_model("lab", "TestMethod").objects.filter(name__in=RETIRED).update(active=True)


class Migration(migrations.Migration):
    dependencies = [("lab", "0004_intake_form_data")]

    operations = [migrations.RunPython(retire, restore)]
