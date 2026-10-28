"""
Data for the sample submission form, taken from the lab's Microsoft Form:
dropdown choices, ES1511 method codes, which tests each sample type can
request, two new tests, and the blends on the refrigerant list.

Only fills in what is missing, so changes already made in Admin are kept.
"""

from django.db import migrations

CHOICES = {
    "refrigerant": [
        "R-1233zd(E)", "R-454B", "R-513A", "R-514A", "R-410A",
        "R-515B", "R-134a", "R-454A", "R-1234ze(E)", "R-22",
    ],
    "lubricant": [
        "Sontex 500 LE", "CPI 2931 AW", "Solest 120", "Solest 220", "Trane Oil 22",
        "RL-32-3MHF", "S100A", "RL32H", "Oilless", "MIN015001",
    ],
    "plant7_location": ["Bay A C2", "Bay B North", "Bay B South", "Bay C 8 Inch Loop"],
    "plant7_refrigerant": ["R-514A", "R-1233zd(E)"],
    "lab11_category": [
        ("Reclaimed Refrigerant Tank", "Reclaimed Tank"),
        ("Compressor Group", "Compressors"),
        ("Air Cooled Chiller", "Air Cooled"),
        ("Water Cooled Chiller", "Water Cooled"),
        ("Unitary", ""),
        ("Unit Testing", ""),
    ],
    "gcs_stand": [f"GCS {n}" for n in [*range(0, 13), 40, 41]],
}

LIQUID = "liquid"
# test name: (method code, sample kinds it can be requested for)
TESTS = {
    "Blend Analysis": ("ES1511078", "liquid,expanded"),
    "Moisture": ("ES1511083", LIQUID),
    "High Boiling Residue": ("ES1511082", LIQUID),
    "Acidity": ("ES1511056", LIQUID),
    "Other Organic Impurities": ("ES1511058", "liquid,expanded"),
    "Oil Circulation Rates": ("ES1511001", LIQUID),
    "Refrigerant Breakdown and Contaminants": ("ES1511067", LIQUID),
    "Non-Condensables": ("ES1511050", "vapor"),
    "TAN": ("ES1511054", "oil"),
    "Determining Moisture in Oil": ("ES1511055", "oil"),
    "Viscosity": ("", "oil"),
    "Refractive Index": ("ES1511061", "oil"),
    "Determination of Refrigerant Dilution": ("ES1511097", "oil"),
    "Wear Metals by ICP-OES": ("ES1511068", "oil"),
}

NEW_TESTS = [
    # name, code, unit, display order (after Miscellaneous in the General group)
    ("Determination of Anions in Water", "ES1511075", "ppm", 22),
    ("Unknown (Chemist Input Needed)", "", "", 24),
]

# Nominal compositions (mass %) from ASHRAE Standard 34.
BLENDS = {
    "R-454B": [("R-32", 68.9), ("R-1234yf", 31.1)],
    "R-454A": [("R-32", 35.0), ("R-1234yf", 65.0)],
    "R-513A": [("R-1234yf", 56.0), ("R-134a", 44.0)],
    "R-514A": [("R-1336mzz(Z)", 74.7), ("R-1130(E)", 25.3)],
    "R-515B": [("R-1234ze(E)", 91.1), ("R-227ea", 8.9)],
}


def seed(apps, schema_editor):
    ChoiceOption = apps.get_model("lab", "ChoiceOption")
    TestMethod = apps.get_model("lab", "TestMethod")
    Blend = apps.get_model("lab", "Blend")
    BlendComponent = apps.get_model("lab", "BlendComponent")

    for list_name, options in CHOICES.items():
        for order, option in enumerate(options):
            label, short = option if isinstance(option, tuple) else (option, "")
            ChoiceOption.objects.get_or_create(
                list_name=list_name, label=label, defaults={"short_label": short, "order": order * 10}
            )

    for name, (code, applies_to) in TESTS.items():
        method = TestMethod.objects.filter(name=name).first()
        if method is None:
            continue
        if not method.code:
            method.code = code
        if not method.applies_to:
            method.applies_to = applies_to
        method.save()

    for name, code, unit, order in NEW_TESTS:
        TestMethod.objects.get_or_create(
            name=name,
            defaults={
                "code": code,
                "group": "general",
                "calc_type": "manual",
                "unit": unit,
                "display_order": order,
                "applies_to": "liquid,oil",
            },
        )

    for blend_name, components in BLENDS.items():
        blend, created = Blend.objects.get_or_create(name=blend_name)
        if created:
            for order, (component, percent) in enumerate(components):
                BlendComponent.objects.create(
                    blend=blend, name=component, nominal_percent=percent, order=order
                )


class Migration(migrations.Migration):
    dependencies = [("lab", "0003_intake_form")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
