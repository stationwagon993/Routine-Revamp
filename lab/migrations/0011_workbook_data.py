"""
Values from the lab's workbooks (GC7890 Cal and Ver Spreadsheet, Master
Routine Spreadsheet v.2).

- Verification rules per blend: the GC workbook's "Ref Error" (± percentage
  points) unless a blend already had a % error set in the app. R-514A
  verifications are good for 1 day, the others for 7.
- R-452A and R-454C, which the GC workbook calibrates.
- Refrigerant liquid densities for HBR (R-22 is left out: the workbook's
  3.66 g/cc is about 3x too high, so it has to be entered in Admin).
- Non-Condensables uses its own calibration.
- The workbook's formulas become the current formulas for both pressures.
"""

import datetime

from django.db import migrations

POINTS_LIMITS = {
    "R-407C": 0.3049,
    "R-410A": 0.3049,
    "R-513A": 0.3049,
    "R-514A": 0.3049,
    "R-515B": 0.3049,
    "R-452A": 0.5049,
    "R-454A": 0.5049,
    "R-454B": 0.5049,
    "R-454C": 0.5049,
}
ONE_DAY_VERIFICATION = ["R-514A"]

NEW_BLENDS = {
    "R-452A": [("R-32", 11.0), ("R-125", 59.0), ("R-1234yf", 30.0)],
    "R-454C": [("R-32", 21.5), ("R-1234yf", 78.5)],
}

DENSITIES = {
    "ER-20": 1.25, "R-12": 1.29, "R-1224yd(Z)": 1.36, "R-123": 1.46, "R-1233zd(E)": 1.28,
    "R-1234yf": 1.09, "R-1234ze(E)": 1.17, "R-1336mzz(Z)": 1.38, "R-134a": 1.21,
    "R-245fa": 1.32, "R-32": 0.96, "R-404A": 1.05, "R-410A": 1.03, "R-452A": 1.24,
    "R-454A": 1.13, "R-454B": 1.0, "R-454C": 1.0, "R-466A": 1.35, "R-513A": 1.19,
    "R-514A": 1.31, "R-515B": 1.18, "R-516A": 1.07,
}


def _inputs(*items):
    return [{"name": n, "label": label, "unit": unit} for n, label, unit in items]


NOTE = "From the Master Routine Spreadsheet v.2."
FORMULAS = {
    "Moisture": dict(
        expression="(rep1_ppm + rep2_ppm + rep3_ppm) / 3",
        inputs=_inputs(("rep1_ppm", "Replicate 1", "ppm"), ("rep2_ppm", "Replicate 2", "ppm"), ("rep3_ppm", "Replicate 3", "ppm")),
        result_unit="ppm",
    ),
    "Acidity": dict(
        expression="(finish_ml - start_ml) * 0.01 * 36500 / mass_g",
        inputs=_inputs(("mass_g", "Grams", "g"), ("start_ml", "Starting mL", "mL"), ("finish_ml", "Finishing mL", "mL")),
        result_unit="ppm",
    ),
    "High Boiling Residue": dict(
        expression="((beaker_final_g - beaker_tare_g) / 0.9) / ((cyl_gross_g - cyl_tare_g) / refrigerant_density) * 100",
        inputs=_inputs(
            ("cyl_tare_g", "Cylinder tare", "g"), ("cyl_gross_g", "Cylinder gross", "g"),
            ("beaker_tare_g", "Beaker tare", "g"), ("beaker_final_g", "Beaker final", "g"),
        ),
        result_unit="% vol",
        notes=NOTE + " Oil residue density 0.9 g/cc; refrigerant density from Admin → Refrigerant densities.",
    ),
    "Oil Circulation Rates": dict(
        expression="(beaker_final_g - beaker_tare_g) / (cyl_gross_g - cyl_tare_g) * 100",
        inputs=_inputs(
            ("cyl_tare_g", "Cylinder tare", "g"), ("cyl_gross_g", "Cylinder gross", "g"),
            ("beaker_tare_g", "Beaker tare", "g"), ("beaker_final_g", "Beaker final", "g"),
        ),
        result_unit="% wt",
        notes=NOTE + " Multiplied by 100: the spreadsheet's OCR cell gives a fraction under a % wt label.",
    ),
    "TAN": dict(
        expression="((finish_ml - start_ml) - (blank_finish_ml - blank_start_ml)) * 0.1 * 56.1 / mass_g",
        inputs=_inputs(
            ("mass_g", "Grams of oil", "g"), ("start_ml", "Starting mL", "mL"), ("finish_ml", "Finishing mL", "mL"),
            ("blank_start_ml", "Blank starting mL", "mL"), ("blank_finish_ml", "Blank finishing mL", "mL"),
        ),
        result_unit="mg KOH/g",
        report_limit="0.05 if mass_g < 5 else 0.01",
        notes=NOTE + " 0.1 N KOH. Reported as <0.01 (5–10 g method) or <0.05 (1–2 g method).",
    ),
    "Refractive Index": dict(
        expression="(ri_sample - ri_contaminant) / (ri_base - ri_contaminant) * 100",
        inputs=_inputs(
            ("ri_contaminant", "RI of contaminating oil", ""), ("ri_base", "RI of base oil", ""),
            ("ri_sample", "RI of sample", ""),
        ),
        result_unit="% base oil",
        decimals=0,
        notes=NOTE + " Calculation of RI: oil mixture concentration, reported to a whole percentage.",
    ),
}


def forwards(apps, schema_editor):
    Blend = apps.get_model("lab", "Blend")
    BlendComponent = apps.get_model("lab", "BlendComponent")
    Verification = apps.get_model("lab", "Verification")
    RefrigerantDensity = apps.get_model("lab", "RefrigerantDensity")
    TestMethod = apps.get_model("lab", "TestMethod")
    Formula = apps.get_model("lab", "Formula")

    for name, components in NEW_BLENDS.items():
        blend, created = Blend.objects.get_or_create(name=name)
        if created:
            for order, (component, percent) in enumerate(components):
                BlendComponent.objects.create(blend=blend, name=component, nominal_percent=percent, order=order)

    for blend in Blend.objects.all():
        if blend.max_error_percent is not None:
            blend.verification_rule = "percent"
            blend.verification_limit = blend.max_error_percent
        elif blend.name in POINTS_LIMITS:
            blend.verification_rule = "points"
            blend.verification_limit = POINTS_LIMITS[blend.name]
        if blend.name in ONE_DAY_VERIFICATION:
            blend.verification_valid_days = 1
        blend.save()

    for v in Verification.objects.select_related("calibration__blend"):
        if v.max_error_percent is not None:
            v.rule = "percent"
            v.limit = v.max_error_percent
        days = v.calibration.blend.verification_valid_days
        v.expires_on = v.performed_on + datetime.timedelta(days=days)
        v.save()

    for name, density in DENSITIES.items():
        RefrigerantDensity.objects.get_or_create(refrigerant=name, defaults={"density_g_cc": density})

    TestMethod.objects.filter(name="Non-Condensables").update(calc_type="nc", unit="%")

    for method_name, spec in FORMULAS.items():
        method = TestMethod.objects.filter(name=method_name).first()
        if method is None:
            continue
        if method.calc_type != "formula":
            method.calc_type = "formula"
            method.save()
        for pressure in ("high", "low"):
            current = (
                Formula.objects.filter(test_method=method, pressure=pressure, active=True)
                .order_by("-version")
                .first()
            )
            last = Formula.objects.filter(test_method=method, pressure=pressure).order_by("-version").first()
            Formula.objects.create(
                test_method=method,
                pressure=pressure,
                version=(last.version if last else 0) + 1,
                expression=spec["expression"],
                inputs=spec["inputs"],
                result_unit=spec["result_unit"],
                decimals=spec.get("decimals", current.decimals if current else 2),
                report_limit=spec.get("report_limit", ""),
                notes=spec.get("notes", NOTE),
                is_placeholder=False,
            )


class Migration(migrations.Migration):
    dependencies = [("lab", "0010_standards_nc_and_verification_rules")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
