"""Business rules shared by views: calculating and saving results, finishing reports."""

from django.db import transaction
from django.utils import timezone

from . import formulas
from .models import Formula, Report, SampleTest, TestMethod, TestResult

# Tracking test that is completed automatically when a report is sent.
RESULTS_REPORTED = "Results Reported"


class ResultError(ValueError):
    pass


def blend_results(calibration, areas):
    """
    Composition of a sample from its peak areas and a calibration.

    ``areas`` maps CalibrationComponent id -> peak area. Returns a list of
    (calibration_component, area, percent) in component order.
    """
    rows = []
    for component in calibration.components.select_related("component"):
        raw = areas.get(component.pk)
        try:
            area = float(raw)
        except (TypeError, ValueError) as exc:
            raise ResultError(f"Peak area for {component.component.name} must be a number.") from exc
        rows.append((component, area, area * component.response_factor))
    return rows


def calculate_formula(formula, values):
    try:
        return formula.calculate(values)
    except formulas.FormulaError as exc:
        raise ResultError(str(exc)) from exc


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
        value = calculate_formula(formula, values)
        result = TestResult(
            value=value,
            unit=formula.result_unit,
            decimals=formula.decimals,
            inputs={k: float(v) for k, v in values.items()},
            formula=formula,
        )
        return [result], None

    if method.calc_type == TestMethod.CALC_BLEND:
        blend = sample_test.sample.blend
        if blend is None:
            raise ResultError("Set the sample's blend before entering blend analysis data.")
        calibration = blend.active_calibration()
        if calibration is None:
            raise ResultError(
                f"There is no current, verified calibration for {blend}. "
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
    sample_test.calibration = calibration
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

