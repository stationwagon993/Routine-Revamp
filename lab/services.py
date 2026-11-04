"""Business rules shared by views: calculating and saving results, finishing reports."""

from django.db import transaction
from django.utils import timezone

from . import formulas
from .models import (
    Calibration,
    Formula,
    NCCalibration,
    RefrigerantDensity,
    Report,
    SampleTest,
    TestMethod,
    TestResult,
    normalized_percents,
)

# Tracking test that is completed automatically when a report is sent.
RESULTS_REPORTED = "Results Reported"


class ResultError(ValueError):
    pass


def blend_results(calibration, areas):
    """
    Composition of a sample from its peak areas and a calibration: peak area ×
    response factor for each component, normalized so the components add up to 100%.

    ``areas`` maps CalibrationComponent id -> peak area. Returns a list of
    (calibration_component, area, percent) in component order.
    """
    components, peak_areas = [], []
    for component in calibration.components.select_related("component"):
        raw = areas.get(component.pk)
        try:
            area = float(raw)
        except (TypeError, ValueError) as exc:
            raise ResultError(f"Peak area for {component.component.name} must be a number.") from exc
        if area < 0:
            raise ResultError(f"Peak area for {component.component.name} can't be negative.")
        components.append(component)
        peak_areas.append(area)
    try:
        percents = normalized_percents([a * c.response_factor for c, a in zip(components, peak_areas)])
    except ValueError as exc:
        raise ResultError(str(exc)) from exc
    return list(zip(components, peak_areas, percents))


def sample_values(sample):
    """Values formulas take from the sample (see formulas.SAMPLE_VARIABLES)."""
    return {"refrigerant_density": RefrigerantDensity.lookup(sample.refrigerant_type)}


def calculate_formula(formula, values, sample_values=None):
    """(value, text reported instead or "") – e.g. "<0.01" at or below the reporting limit."""
    try:
        value = formula.calculate(values, sample_values)
        limit = formula.reporting_limit(values, sample_values)
    except formulas.FormulaError as exc:
        raise ResultError(str(exc)) from exc
    reported = f"<{limit:g}" if limit is not None and value <= limit else ""
    return value, reported


def _chosen_calibration(model, pk, **match):
    """The calibration picked on the sample page (the GC switcher), if it's still usable."""
    if not pk:
        return None
    try:
        calibration = model.objects.select_related("instrument").get(pk=pk, **match)
    except (model.DoesNotExist, ValueError):
        raise ResultError("That calibration isn't available.") from None
    if not calibration.instrument.active or not calibration.is_usable():
        raise ResultError(
            f"The {calibration.instrument} calibration can't be used: it has expired or needs verifying."
        )
    return calibration


def compute_results(sample_test, data):
    """
    Work out the results for one test from submitted form ``data`` without
    saving anything. Returns (unsaved TestResults, calibration used or None).
    """
    method = sample_test.test_method

    if method.calc_type == TestMethod.CALC_FORMULA:
        try:
            formula = Formula.objects.get(pk=data.get("formula_id"), test_method=method)
        except (Formula.DoesNotExist, ValueError) as exc:
            raise ResultError("No formula is set up for this test.") from exc
        values = {name: data.get(f"input_{name}") for name in formula.input_names}
        sample = sample_test.sample
        from_sample = sample_values(sample)
        if "refrigerant_density" in formula.sample_variables and from_sample["refrigerant_density"] is None:
            raise ResultError(
                f"No liquid density is set up for {sample.refrigerant_type or 'this sample (no refrigerant type)'}. "
                "Add it under Admin → Refrigerant densities."
            )
        value, reported = calculate_formula(formula, values, from_sample)
        inputs = {k: float(v) for k, v in values.items()}
        inputs.update({k: v for k, v in from_sample.items() if k in formula.sample_variables})
        result = TestResult(
            value=value,
            text_value=reported,
            unit=formula.result_unit,
            decimals=formula.decimals,
            inputs=inputs,
            formula=formula,
        )
        return [result], None

    if method.calc_type == TestMethod.CALC_BLEND:
        blend = sample_test.sample.blend
        if blend is None:
            raise ResultError(
                f"{sample_test.sample.refrigerant_type or 'This refrigerant'} isn't set up as a blend yet. "
                "Add it with its components under Admin → Blends."
            )
        calibration = _chosen_calibration(Calibration, data.get("calibration_id"), blend=blend)
        if calibration is None:
            calibration = blend.active_calibration()
        if calibration is None:
            raise ResultError(
                f"There is no current, verified {blend} calibration on any instrument. "
                "Calibrate and verify before reporting blend results."
            )
        areas = {}
        for key, value in data.items():
            if key.startswith("area_"):
                try:
                    areas[int(key.removeprefix("area_"))] = value
                except ValueError:
                    continue
        results = [
            TestResult(
                label=component.component.name,
                order=order,
                value=percent,
                unit="%",
                decimals=2,
                inputs={"peak_area": area, "response_factor": component.response_factor},
            )
            for order, (component, area, percent) in enumerate(blend_results(calibration, areas))
        ]
        return results, calibration

    if method.calc_type == TestMethod.CALC_NC:
        calibration = _chosen_calibration(NCCalibration, data.get("nc_calibration_id"))
        if calibration is None:
            calibration = NCCalibration.active()
        if calibration is None:
            raise ResultError(
                "There is no current, verified non-condensables calibration. "
                "Calibrate and verify before reporting non-condensables."
            )
        areas = []
        for key in sorted(k for k in data if k.startswith("nc_area_")):
            raw = (data.get(key) or "").strip()
            if not raw:
                continue
            try:
                areas.append(float(raw))
            except ValueError as exc:
                raise ResultError("Peak areas must be numbers.") from exc
        if not areas:
            raise ResultError("Enter the replicate peak areas.")
        result = TestResult(
            value=calibration.result_percent(areas),
            unit="%",
            decimals=4,
            inputs={"areas": areas, "area_per_percent": calibration.area_per_percent},
        )
        return [result], calibration

    if method.calc_type == TestMethod.CALC_MANUAL:
        raw = (data.get("manual_value") or "").strip()
        if not raw:
            raise ResultError("Enter a result.")
        unit = (data.get("manual_unit") or method.unit).strip()
        try:
            value, text_value = float(raw), ""
        except ValueError:
            value, text_value = None, raw
        decimals = len(raw.split(".")[1]) if value is not None and "." in raw else 0
        result = TestResult(value=value, text_value=text_value, unit=unit, decimals=decimals)
        return [result], None

    raise ResultError(f"{method.name} does not take results.")


@transaction.atomic
def save_results(sample_test, user, data):
    """Store results for one test, replacing earlier ones, and mark it completed."""
    results, calibration = compute_results(sample_test, data)
    sample_test.results.all().delete()
    for result in results:
        result.sample_test = sample_test
        result.entered_by = user
        result.save()
    if isinstance(calibration, NCCalibration):
        sample_test.nc_calibration, sample_test.calibration = calibration, None
    else:
        sample_test.calibration, sample_test.nc_calibration = calibration, None
    sample_test.set_status(SampleTest.STATUS_COMPLETED, user)
    return results


@transaction.atomic
def mark_report_sent(report, user):
    report.status = Report.STATUS_SENT
    report.sent_by = user
    report.sent_at = timezone.now()
    report.save()
    tests = SampleTest.objects.filter(
        sample__in=report.samples.all(),
        test_method__name=RESULTS_REPORTED,
    )
    for test in tests:
        test.set_status(SampleTest.STATUS_COMPLETED, user)

