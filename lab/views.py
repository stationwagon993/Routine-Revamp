from datetime import timedelta
from itertools import groupby
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import outlook, reports, services
from .forms import (
    CalibrationForm,
    FormulaForm,
    ReportForm,
    SampleForm,
    SampleIntakeForm,
    VerificationForm,
)
from .models import (
    Blend,
    Calibration,
    CalibrationComponent,
    Formula,
    Report,
    Sample,
    SampleTest,
    TestMethod,
    VerificationComponent,
)

GROUP_ORDER = [TestMethod.GROUP_GENERAL, TestMethod.GROUP_REFRIGERANT, TestMethod.GROUP_OIL]
EXPIRY_WARNING_DAYS = 14


def _ordered_methods(queryset):
    return sorted(queryset, key=lambda m: (GROUP_ORDER.index(m.group), m.display_order, m.name))


def _grouped(methods):
    return [
        (dict(TestMethod.GROUP_CHOICES)[group], list(items))
        for group, items in groupby(methods, key=lambda m: m.group)
    ]


def _open_samples():
    return Sample.objects.filter(tests__status__in=SampleTest.OPEN_STATUSES).distinct()


# --- Sample log -------------------------------------------------------------


@login_required
def sample_log(request):
    methods = _ordered_methods(TestMethod.objects.filter(active=True))
    samples = Sample.objects.prefetch_related(
        Prefetch("tests", queryset=SampleTest.objects.select_related("completed_by__lab_profile"))
    )

    show = request.GET.get("show", "all")
    if show == "open":
        samples = samples.filter(pk__in=_open_samples())
    query = request.GET.get("q", "").strip()
    if query:
        samples = samples.filter(
            Q(sample_number__icontains=query)
            | Q(identification__icontains=query)
            | Q(lto__icontains=query)
            | Q(source__icontains=query)
            | Q(requested_by_name__icontains=query)
        )

    page = Paginator(samples, 100).get_page(request.GET.get("page"))
    oldest = _open_samples().order_by("julian", "sequence").first()

    rows = []
    for sample in page:
        by_method = {t.test_method_id: t for t in sample.tests.all()}
        rows.append((sample, [by_method.get(m.pk) for m in methods]))

    return render(
        request,
        "lab/sample_log.html",
        {
            "groups": _grouped(methods),
            "rows": rows,
            "page": page,
            "oldest": oldest,
            "show": show,
            "query": query,
            "expiring": _expiring_calibrations(),
        },
    )


@login_required
def sample_create(request):
    form = SampleIntakeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            sample = form.save(commit=False)
            sample.received_by = request.user
            sample.save()
            methods = set(form.cleaned_data["tests"])
            methods.update(TestMethod.objects.filter(active=True, on_every_sample=True))
            SampleTest.objects.bulk_create(SampleTest(sample=sample, test_method=m) for m in methods)
        messages.success(request, f"Sample {sample.sample_number} logged.")
        if "add_another" in request.POST:
            return redirect("lab:sample_create")
        return redirect(sample)

    methods = _ordered_methods(form.fields["tests"].queryset)
    selected = {str(v) for v in (form["tests"].value() or [])}
    return render(
        request,
        "lab/sample_form.html",
        {"form": form, "test_groups": _grouped(methods), "selected": selected},
    )


@login_required
def sample_edit(request, pk):
    sample = get_object_or_404(Sample, pk=pk)
    form = SampleForm(request.POST or None, instance=sample)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Sample updated.")
        return redirect(sample)
    return render(request, "lab/sample_form.html", {"form": form, "sample": sample})


@login_required
def sample_detail(request, pk):
    sample = get_object_or_404(Sample, pk=pk)
    tests = list(
        sample.tests.select_related("test_method", "completed_by", "calibration").prefetch_related(
            "results__formula"
        )
    )
    calibration = sample.blend.active_calibration() if sample.blend else None
    entries = []
    for test in tests:
        formula = None
        if test.test_method.calc_type == TestMethod.CALC_FORMULA:
            formula = test.test_method.current_formula()
        previous = {}
        for result in test.results.all():
            previous.update({k: v for k, v in result.inputs.items()})
        entries.append({"test": test, "formula": formula, "previous": previous})

    used = {t.test_method_id for t in tests}
    addable = _ordered_methods(TestMethod.objects.filter(active=True).exclude(pk__in=used))

    related = Sample.objects.none()
    if sample.lto:
        related = Sample.objects.filter(lto=sample.lto).exclude(pk=sample.pk)

    return render(
        request,
        "lab/sample_detail.html",
        {
            "sample": sample,
            "entries": entries,
            "calibration": calibration,
            "calibration_components": calibration.components.select_related("component")
            if calibration
            else [],
            "status_choices": SampleTest.STATUS_CHOICES,
            "addable": addable,
            "related": related,
            "reports": sample.reports.all(),
        },
    )


@login_required
@require_POST
def sample_add_test(request, pk):
    sample = get_object_or_404(Sample, pk=pk)
    method = get_object_or_404(TestMethod, pk=request.POST.get("test_method"), active=True)
    SampleTest.objects.get_or_create(sample=sample, test_method=method)
    messages.success(request, f"{method.name} added.")
    return redirect(sample)


@login_required
@require_POST
def test_results(request, pk):
    test = get_object_or_404(SampleTest.objects.select_related("sample", "test_method"), pk=pk)
    try:
        services.save_results(test, request.user, request.POST)
    except services.ResultError as exc:
        messages.error(request, f"{test.test_method.name}: {exc}")
    else:
        messages.success(request, f"{test.test_method.name} results saved.")
    return redirect(f"{test.sample.get_absolute_url()}#test-{test.pk}")


@login_required
@require_POST
def test_calculate(request, pk):
    """Live preview of a result while the user types; nothing is saved."""
    test = get_object_or_404(SampleTest.objects.select_related("sample", "test_method"), pk=pk)
    try:
        results, _ = services.compute_results(test, request.POST)
    except services.ResultError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})
    return JsonResponse(
        {
            "ok": True,
            "results": [
                {"label": r.label, "value": r.display_value, "unit": r.unit} for r in results
            ],
        }
    )


@login_required
@require_POST
def test_status(request, pk):
    test = get_object_or_404(SampleTest, pk=pk)
    status = request.POST.get("status")
    if status not in dict(SampleTest.STATUS_CHOICES):
        raise Http404
    test.set_status(status, request.user)
    return redirect(f"{test.sample.get_absolute_url()}#test-{test.pk}")


# --- Reports ----------------------------------------------------------------


@login_required
@require_POST
def report_create(request, pk):
    sample = get_object_or_404(Sample, pk=pk)
    extra_ids = request.POST.getlist("samples")
    samples = [sample, *Sample.objects.filter(pk__in=extra_ids).exclude(pk=sample.pk)]
    samples.sort(key=lambda s: (s.julian, s.sequence))
    report = Report.objects.create(
        report_number=Report.next_number(samples[0]),
        recipient_email=sample.requested_by_email,
        cc_emails=sample.cc_emails,
        created_by=request.user,
    )
    report.samples.set(samples)
    return redirect(report)


@login_required
def report_detail(request, pk):
    report = get_object_or_404(Report, pk=pk)
    form = ReportForm(request.POST or None, instance=report)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Report details saved.")
        return redirect(report)
    samples = report.ordered_samples().prefetch_related(
        Prefetch(
            "tests",
            queryset=SampleTest.objects.select_related("test_method", "completed_by").prefetch_related(
                "results"
            ),
        )
    )
    return render(
        request,
        "lab/report_detail.html",
        {
            "report": report,
            "form": form,
            "samples": samples,
            "open_tests": SampleTest.objects.filter(
                sample__in=report.samples.all(),
                status__in=SampleTest.OPEN_STATUSES,
                test_method__calc_type__in=[
                    TestMethod.CALC_FORMULA,
                    TestMethod.CALC_BLEND,
                    TestMethod.CALC_MANUAL,
                ],
            ).select_related("sample", "test_method"),
            "placeholder": report.uses_placeholder_formula(),
            "pdf_exists": bool(report.pdf_path) and Path(report.pdf_path).exists(),
        },
    )


@login_required
@require_POST
def report_generate(request, pk):
    report = get_object_or_404(Report, pk=pk)
    try:
        path = reports.generate(report)
    except OSError as exc:
        messages.error(request, f"Could not save the PDF: {exc}")
    else:
        messages.success(request, f"PDF saved to {path}")
    return redirect(report)


@login_required
def report_pdf(request, pk):
    report = get_object_or_404(Report, pk=pk)
    if not report.pdf_path or not Path(report.pdf_path).exists():
        raise Http404("Generate the PDF first.")
    return FileResponse(open(report.pdf_path, "rb"), content_type="application/pdf")


@login_required
@require_POST
def report_email(request, pk):
    report = get_object_or_404(Report, pk=pk)
    if not report.pdf_path or not Path(report.pdf_path).exists():
        messages.error(request, "Generate the PDF first.")
        return redirect(report)
    if outlook.open_in_outlook(report):
        messages.success(request, "Email draft opened in Outlook. Check it, then click Send.")
        return redirect(report)
    response = HttpResponse(outlook.eml_bytes(report), content_type="message/rfc822")
    response["Content-Disposition"] = f'attachment; filename="{report.report_number}.eml"'
    return response


@login_required
@require_POST
def report_mark_sent(request, pk):
    report = get_object_or_404(Report, pk=pk)
    services.mark_report_sent(report, request.user)
    messages.success(request, "Report marked as sent and the sample log updated.")
    return redirect(report)


@login_required
def report_list(request):
    return render(
        request,
        "lab/report_list.html",
        {"reports": Report.objects.prefetch_related("samples").select_related("created_by")[:200]},
    )


# --- Calibrations -----------------------------------------------------------


def _expiring_calibrations():
    """Current calibrations that expire soon, one per blend."""
    today = timezone.localdate()
    soon = []
    for blend in Blend.objects.filter(active=True):
        calibration = blend.active_calibration()
        if calibration and calibration.expires_on <= today + timedelta(days=EXPIRY_WARNING_DAYS):
            soon.append(calibration)
    return soon


@login_required
def calibration_list(request):
    today = timezone.localdate()
    archive = request.GET.get("view") == "archive"
    calibrations = Calibration.objects.select_related("blend", "performed_by").prefetch_related(
        "verifications__components__calibration_component"
    )
    if archive:
        calibrations = calibrations.filter(expires_on__lt=today)
    else:
        calibrations = calibrations.filter(expires_on__gte=today)
    blend_id = request.GET.get("blend")
    if blend_id:
        calibrations = calibrations.filter(blend_id=blend_id)

    blends = Blend.objects.filter(active=True)
    blend_status = [(blend, blend.active_calibration()) for blend in blends]
    return render(
        request,
        "lab/calibration_list.html",
        {
            "calibrations": calibrations,
            "archive": archive,
            "blends": Blend.objects.all(),
            "blend_id": blend_id,
            "blend_status": blend_status,
        },
    )


@login_required
def calibration_create(request):
    blend = None
    if request.GET.get("blend"):
        blend = get_object_or_404(Blend, pk=request.GET["blend"], active=True)
    if blend is None:
        return render(
            request, "lab/calibration_choose_blend.html", {"blends": Blend.objects.filter(active=True)}
        )
    if not blend.components.exists():
        messages.error(request, f"{blend} has no components yet. Add them in Admin → Blends.")
        return redirect("lab:calibration_list")

    form = CalibrationForm(request.POST or None, blend=blend)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            calibration = form.save(commit=False)
            calibration.blend = blend
            calibration.performed_by = request.user
            calibration.save()
            for component, values in form.component_values():
                CalibrationComponent.objects.create(
                    calibration=calibration, component=component, **values
                )
        messages.success(request, "Calibration saved. Run the verification next.")
        return redirect(calibration)
    return render(request, "lab/calibration_form.html", {"form": form, "blend": blend})


@login_required
def calibration_detail(request, pk):
    calibration = get_object_or_404(
        Calibration.objects.select_related("blend", "performed_by"), pk=pk
    )
    return render(
        request,
        "lab/calibration_detail.html",
        {
            "calibration": calibration,
            "components": calibration.components.select_related("component"),
            "verifications": calibration.verifications.select_related("performed_by").prefetch_related(
                "components__calibration_component__component"
            ),
            "samples": Sample.objects.filter(tests__calibration=calibration).distinct(),
        },
    )


@login_required
def verification_create(request, pk):
    calibration = get_object_or_404(Calibration, pk=pk)
    if calibration.is_expired:
        messages.error(request, "This calibration has expired and is read-only.")
        return redirect(calibration)
    form = VerificationForm(request.POST or None, calibration=calibration)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            verification = form.save(commit=False)
            verification.calibration = calibration
            verification.performed_by = request.user
            verification.save()
            for cc, values in form.component_values():
                VerificationComponent.objects.create(
                    verification=verification, calibration_component=cc, **values
                )
        if verification.passed:
            messages.success(request, "Verification passed. The calibration is now active.")
        else:
            messages.error(request, "Verification failed. The calibration cannot be used.")
        return redirect(calibration)
    return render(
        request, "lab/verification_form.html", {"form": form, "calibration": calibration}
    )


# --- Formulas ---------------------------------------------------------------


@login_required
def formula_list(request):
    methods = _ordered_methods(
        TestMethod.objects.filter(calc_type=TestMethod.CALC_FORMULA).prefetch_related("formulas")
    )
    return render(request, "lab/formula_list.html", {"methods": methods})


@login_required
def formula_new_version(request, method_pk):
    method = get_object_or_404(TestMethod, pk=method_pk, calc_type=TestMethod.CALC_FORMULA)
    current = method.current_formula()
    # Always a new, unsaved version, pre-filled from the current one.
    draft = Formula(test_method=method)
    if current:
        draft.expression = current.expression
        draft.inputs = current.inputs
        draft.result_unit = current.result_unit
        draft.decimals = current.decimals
    form = FormulaForm(request.POST or None, instance=draft)
    if request.method == "POST" and form.is_valid():
        formula = form.save(commit=False)
        formula.version = (method.formulas.aggregate(v=Max("version"))["v"] or 0) + 1
        formula.created_by = request.user
        formula.save()
        messages.success(request, f"{method.name} formula version {formula.version} saved.")
        return redirect("lab:formula_list")
    return render(
        request, "lab/formula_form.html", {"form": form, "method": method, "current": current}
    )
