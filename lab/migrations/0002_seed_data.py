"""
Starting data: the sample log's test columns, placeholder formulas for the
calculated tests, and a few common blends.

Method codes are left blank (the screenshot was too small to read them
reliably); fill them in under Admin → Test methods. The formulas are marked as
placeholders until the lab replaces them with its own.
"""

from django.db import migrations

GENERAL, REFRIGERANT, OIL = "general", "refrigerant", "oil"
TRACKING, MANUAL, FORMULA, BLEND = "tracking", "manual", "formula", "blend"

# (name, group, result type, unit, on every sample)
TEST_METHODS = [
    ("Test Plan Submitted", GENERAL, TRACKING, "", False),
    ("Results Reported", GENERAL, TRACKING, "", True),
    ("Miscellaneous", GENERAL, MANUAL, "", False),
    ("Blend Analysis", REFRIGERANT, BLEND, "%", False),
    ("Moisture", REFRIGERANT, FORMULA, "ppm", False),
    ("High Boiling Residue", REFRIGERANT, FORMULA, "%", False),
    ("Acidity", REFRIGERANT, FORMULA, "ppm", False),
    ("Non-Condensables", REFRIGERANT, MANUAL, "%", False),
    ("Oil Circulation Rates", REFRIGERANT, FORMULA, "%", False),
    ("Other Organic Impurities", REFRIGERANT, MANUAL, "%", False),
    ("Refrigerant Breakdown and Contaminants", REFRIGERANT, MANUAL, "", False),
    ("TAN", OIL, FORMULA, "mg KOH/g", False),
    ("Determining Moisture in Oil", OIL, FORMULA, "ppm", False),
    ("Viscosity", OIL, MANUAL, "cSt", False),
    ("Refractive Index", OIL, FORMULA, "", False),
    ("Determination of Refrigerant Dilution", OIL, MANUAL, "%", False),
    ("Wear Metals by ICP-OES", OIL, MANUAL, "ppm", False),
]


def _inputs(*items):
    return [{"name": n, "label": label, "unit": unit} for n, label, unit in items]


PLACEHOLDER_FORMULAS = {
    "Moisture": (
        "water_ug / mass_g",
        _inputs(("water_ug", "Water titrated", "µg"), ("mass_g", "Sample mass", "g")),
        "ppm",
        1,
    ),
    "High Boiling Residue": (
        "residue_g / mass_g * 100",
        _inputs(("residue_g", "Residue mass", "g"), ("mass_g", "Sample mass", "g")),
        "%",
        3,
    ),
    "Acidity": (
        "(v_sample_ml - v_blank_ml) * normality * 36.46 * 1000 / mass_g",
        _inputs(
            ("v_sample_ml", "Titrant volume, sample", "mL"),
            ("v_blank_ml", "Titrant volume, blank", "mL"),
            ("normality", "Titrant normality", "N"),
            ("mass_g", "Sample mass", "g"),
        ),
        "ppm",
        1,
    ),
    "Oil Circulation Rates": (
        "oil_g / (oil_g + refrigerant_g) * 100",
        _inputs(("oil_g", "Oil mass", "g"), ("refrigerant_g", "Refrigerant mass", "g")),
        "%",
        2,
    ),
    "TAN": (
        "(v_sample_ml - v_blank_ml) * normality * 56.1 / mass_g",
        _inputs(
            ("v_sample_ml", "KOH volume, sample", "mL"),
            ("v_blank_ml", "KOH volume, blank", "mL"),
            ("normality", "KOH normality", "N"),
            ("mass_g", "Sample mass", "g"),
        ),
        "mg KOH/g",
        3,
    ),
    "Determining Moisture in Oil": (
        "water_ug / mass_g",
        _inputs(("water_ug", "Water titrated", "µg"), ("mass_g", "Sample mass", "g")),
        "ppm",
        1,
    ),
    "Refractive Index": (
        "n_measured + 0.0004 * (temp_c - 20)",
        _inputs(("n_measured", "Measured RI", ""), ("temp_c", "Temperature", "°C")),
        "",
        4,
    ),
}

# Nominal compositions (mass %) from ASHRAE Standard 34.
BLENDS = {
    "R-404A": [("R-125", 44.0), ("R-143a", 52.0), ("R-134a", 4.0)],
    "R-407C": [("R-32", 23.0), ("R-125", 25.0), ("R-134a", 52.0)],
    "R-410A": [("R-32", 50.0), ("R-125", 50.0)],
}


def seed(apps, schema_editor):
    TestMethod = apps.get_model("lab", "TestMethod")
    Formula = apps.get_model("lab", "Formula")
    Blend = apps.get_model("lab", "Blend")
    BlendComponent = apps.get_model("lab", "BlendComponent")

    for order, (name, group, calc_type, unit, every) in enumerate(TEST_METHODS):
        method, _ = TestMethod.objects.get_or_create(
            name=name,
            defaults={
                "group": group,
                "calc_type": calc_type,
                "unit": unit,
                "display_order": order * 10,
                "on_every_sample": every,
            },
        )
        if name in PLACEHOLDER_FORMULAS and not Formula.objects.filter(test_method=method).exists():
            expression, inputs, result_unit, decimals = PLACEHOLDER_FORMULAS[name]
            Formula.objects.create(
                test_method=method,
                version=1,
                expression=expression,
                inputs=inputs,
                result_unit=result_unit,
                decimals=decimals,
                is_placeholder=True,
                notes="Example only – replace with the lab's method.",
            )

    for blend_name, components in BLENDS.items():
        blend, created = Blend.objects.get_or_create(name=blend_name)
        if created:
            for order, (component, percent) in enumerate(components):
                BlendComponent.objects.create(
                    blend=blend, name=component, nominal_percent=percent, order=order
                )


class Migration(migrations.Migration):
    dependencies = [("lab", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
