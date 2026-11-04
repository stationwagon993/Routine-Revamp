from datetime import timedelta
from itertools import groupby
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from . import outlook, reports, services
from .forms import (
    CalibrationForm,
    FormulaForm,
    InstrumentForm,
    NCCalibrationForm,
    NCVerificationForm,
    ReportForm,
    SampleForm,
    SampleIntakeForm,
    StandardCylinderForm,
    VerificationForm,
)
from .models import (
    Blend,
    Calibration,
    CalibrationComponent,
    ChoiceOption,
    Formula,
    Instrument,
    NCCalibration,
    NCVerification,
    RefrigerantDensity,
    Report,
    Sample,
    SampleTest,
    StandardCylinder,
    TestMethod,
    VerificationComponent,
    rsd_percent,
)

GROUP_ORDER = [TestMethod.GROUP_GENERAL, TestMethod.GROUP_REFRIGERANT, TestMethod.GROUP_OIL]
# The intake form and the sample log columns list the sample-specific tests first.
TESTS_FIRST_ORDER = [TestMethod.GROUP_REFRIGERANT, TestMethod.GROUP_OIL, TestMethod.GROUP_GENERAL]
EXPIRY_WARNING_DAYS = 14


def _ordered_methods(queryset, group_order=GROUP_ORDER):
    return sorted(queryset, key=lambda m: (group_order.index(m.group), m.display_order, m.name))


def _grouped(methods):
    return [
        (dict(TestMethod.GROUP_CHOICES)[group], list(items))
        for group, items in groupby(methods, key=lambda m: m.group)
    ]


def _filter_summary(needed, date_from, date_to):
    """What the sample log filter box shows while closed, like a dropdown's selected option."""
    parts = []
    if len(needed) == 1:
        parts.append(f"Needs {needed[0].name}")
    elif needed:
        parts.append(f"Needs {len(needed)} tests")
    if date_from and date_to:
        parts.append(f"{date_from:%m/%d/%Y} – {date_to:%m/%d/%Y}")
    elif date_from:
        parts.append(f"From {date_from:%m/%d/%Y}")
    elif date_to:
        parts.append(f"Up to {date_to:%m/%d/%Y}")
    return " · ".join(parts)


def _date_param(request, name):
    try:
        return parse_date(request.GET.get(name, ""))
    except ValueError:  # Well-formed but impossible, e.g. 2026-02-30.
        return None


def _open_samples():
    return Sample.objects.filter(tests__status__in=SampleTest.OPEN_STATUSES).distinct()


# --- Sample log -------------------------------------------------------------


@login_required
def sample_log(request):
    methods = _ordered_methods(TestMethod.objects.filter(active=True))
    # Log columns: Results Reported first (as in the Excel log), then refrigerant, oil, other general tests.
    columns = sorted(_ordered_methods(methods, TESTS_FIRST_ORDER), key=lambda m: not m.on_every_sample)
    samples = Sample.objects.prefetch_related(
        Prefetch("tests", queryset=SampleTest.objects.select_related("completed_by__lab_profile"))
    )

    needed = [m for m in methods if str(m.pk) in request.GET.getlist("tests")]
    if needed:
        # Samples that still need any of the chosen tests.
        samples = samples.filter(
            pk__in=SampleTest.objects.filter(
                test_method__in=needed, status__in=SampleTest.OPEN_STATUSES
            ).values("sample")
        )
    date_from = _date_param(request, "from")
    date_to = _date_param(request, "to")
    if date_from:
        samples = samples.filter(received_on__gte=date_from)
    if date_to:
        samples = samples.filter(received_on__lte=date_to)
    query = request.GET.get("q", "").strip()
    if query:
        samples = samples.filter(
            Q(sample_number__icontains=query)
            | Q(identification__icontains=query)
            | Q(lto__icontains=query)
            | Q(source__icontains=query)
            | Q(unit_id__icontains=query)
            | Q(lot_number__icontains=query)
            | Q(origin__icontains=query)
            | Q(origin_area__icontains=query)
            | Q(refrigerant_type__icontains=query)
            | Q(requested_by_name__icontains=query)
        )

    page = Paginator(samples, 100).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    oldest = _open_samples().order_by("julian", "sequence").first()

    rows = []
    for sample in page:
        by_method = {t.test_method_id: t for t in sample.tests.all()}
        rows.append((sample, [by_method.get(m.pk) for m in columns]))

    return render(
        request,
        "lab/sample_log.html",
        {
            "groups": _grouped(methods),
            "column_groups": _grouped(columns),
            "rows": rows,
            "page": page,
            "oldest": oldest,
            "query": query,
            "needed_ids": {m.pk for m in needed},
            "date_from": date_from,
            "date_to": date_to,
            "filter_summary": _filter_summary(needed, date_from, date_to),
            "params": params.urlencode(),
            "calibration_warnings": _calibration_warnings(),
        },
    )


@login_required
def sample_create(request):
    today = timezone.localdate()
    form = SampleIntakeForm(
        request.POST or None,
        initial={
            "received_on": today,
            "sampled_on": today,
        },
    )
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

    methods = _ordered_methods(form.fields["tests"].queryset, TESTS_FIRST_ORDER)
    selected = {str(v) for v in (form["tests"].value() or [])}
    return render(
        request,
        "lab/sample_form.html",
        {
            "form": form,
            "test_groups": _grouped(methods),
            "selected": selected,
            "single_component": sorted(ChoiceOption.single_component_keys()),
        },
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


def _picked(choices, wanted):
    """The calibration for the instrument chosen in a GC switcher, else the first usable one."""
    for instrument, calibration in choices:
        if str(instrument.pk) == wanted and calibration:
            return calibration
    return next((calibration for _, calibration in choices if calibration), None)


@login_required
def sample_detail(request, pk):
    sample = get_object_or_404(Sample, pk=pk)
    if sample.blend is None and sample.refrigerant_type:
        # The blend may have been added in Admin since the sample was logged.
        sample.blend = Blend.matching(sample.refrigerant_type)
        if sample.blend:
            sample.save(update_fields=["blend"])
    tests = list(
        sample.tests.select_related("test_method", "completed_by", "calibration", "nc_calibration").prefetch_related(
            "results__formula"
        )
    )
    instruments = list(Instrument.objects.filter(active=True))
    # GC switchers: each instrument with its usable calibration (or None), and the one picked.
    gc_choices, calibration = [], None
    if sample.blend and any(t.test_method.calc_type == TestMethod.CALC_BLEND for t in tests):
        gc_choices = [(i, sample.blend.active_calibration(instrument=i)) for i in instruments]
        calibration = _picked(gc_choices, request.GET.get("gc"))
    nc_choices, nc_calibration = [], None
    if any(t.test_method.calc_type == TestMethod.CALC_NC for t in tests):
        nc_choices = [(i, NCCalibration.active(instrument=i)) for i in instruments]
        nc_calibration = _picked(nc_choices, request.GET.get("nc_gc"))
    entries = []
    for test in tests:
        formula = None
        if test.test_method.calc_type == TestMethod.CALC_FORMULA:
            formula = test.test_method.current_formula(sample.pressure)
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
            "gc_choices": gc_choices,
            "nc_calibration": nc_calibration,
            "nc_choices": nc_choices,
            "nc_replicates": range(1, 5),
            "refrigerant_density": RefrigerantDensity.lookup(sample.refrigerant_type),
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
        recipient_email=sample.requested_by_name,
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


def _calibration_warnings():
    """Sample log banners: calibrations about to expire and verifications that need redoing."""
    today = timezone.localdate()
    warnings = []
    for instrument in Instrument.objects.filter(active=True):
        for blend in Blend.objects.filter(active=True):
            latest = blend.calibrations.filter(instrument=instrument, expires_on__gte=today).first()
            if latest is None:
                continue
            if latest.expires_on <= today + timedelta(days=EXPIRY_WARNING_DAYS):
                warnings.append(
                    {
                        "text": f"The {blend} calibration on {instrument} expires on {latest.expires_on:%m/%d/%Y}.",
                        "url": reverse("lab:calibration_create") + f"?gc={instrument.pk}&blend={blend.pk}",
                        "link": "Calibrate now",
                    }
                )
            if latest.status == Calibration.STATUS_REVERIFY:
                warnings.append(
                    {
                        "text": f"The {blend} verification on {instrument} has expired; "
                        "blend results can't use it until it's verified again.",
                        "url": reverse("lab:verification_create", args=[latest.pk]),
                        "link": "Verify now",
                    }
                )
    return warnings


def _instrument_from_request(request, required=False):
    """The active instrument named by ?gc=, or None (404 if it's required and missing)."""
    value = request.GET.get("gc") or request.POST.get("gc")
    if value:
        return get_object_or_404(Instrument, pk=value, active=True)
    if required:
        raise Http404("Choose an instrument.")
    return None


@login_required
def calibration_list(request):
    today = timezone.localdate()
    instruments = list(Instrument.objects.filter(active=True))
    instrument = _instrument_from_request(request) or (instruments[0] if instruments else None)
    archive = request.GET.get("view") == "archive"
    blend_id = request.GET.get("blend")

    calibrations = Calibration.objects.none()
    blend_status, nc, nc_verification = [], None, None
    if instrument:
        calibrations = Calibration.objects.filter(instrument=instrument).select_related(
            "blend", "performed_by", "standard"
        )
        calibrations = calibrations.filter(expires_on__lt=today) if archive else calibrations.filter(expires_on__gte=today)
        if blend_id:
            calibrations = calibrations.filter(blend_id=blend_id)
        for blend in Blend.objects.filter(active=True):
            latest = blend.calibrations.filter(instrument=instrument, expires_on__gte=today).first()
            blend_status.append((blend, latest, latest.latest_verification if latest else None))
        nc = NCCalibration.objects.filter(instrument=instrument, expires_on__gte=today).first()
        nc_verification = nc.latest_verification if nc else None
    standards = StandardCylinder.objects.filter(status=StandardCylinder.STATUS_ON_HAND).select_related("blend")
    return render(
        request,
        "lab/calibration_list.html",
        {
            "instruments": instruments,
            "instrument": instrument,
            "calibrations": calibrations,
            "archive": archive,
            "blends": Blend.objects.all(),
            "blend_id": blend_id,
            "blend_status": blend_status,
            "nc": nc,
            "nc_verification": nc_verification,
            "standards": standards,
        },
    )


def _blend_from_request(request):
    if request.GET.get("blend"):
        return get_object_or_404(Blend, pk=request.GET["blend"], active=True)
    return None


def _choose(request, title, with_blends=True):
    return render(
        request,
        "lab/calibration_choose_blend.html",
        {
            "title": title,
            "instruments": Instrument.objects.filter(active=True),
            "selected_gc": request.GET.get("gc", ""),
            "selected_blend": request.GET.get("blend", ""),
            "blends": Blend.objects.filter(active=True) if with_blends else None,
        },
    )


@login_required
def calibration_create(request):
    instrument = _instrument_from_request(request)
    blend = _blend_from_request(request)
    if instrument is None or blend is None:
        return _choose(request, "New calibration")
    if not blend.components.exists():
        messages.error(request, f"{blend} has no components yet. Add them in Admin → Blends.")
        return redirect(reverse("lab:calibration_list") + f"?gc={instrument.pk}")
    form = CalibrationForm(request.POST or None, blend=blend)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            calibration = form.save(commit=False)
            calibration.instrument = instrument
            calibration.blend = blend
            calibration.performed_by = request.user
            calibration.save()
            for component, values in form.component_values():
                CalibrationComponent.objects.create(
                    calibration=calibration, component=component, **values
                )
        messages.success(request, "Calibration saved. Run the verification next.")
        return redirect(calibration)
    return render(request, "lab/calibration_form.html", {"form": form, "blend": blend, "instrument": instrument})


@login_required
def calibration_detail(request, pk):
    calibration = get_object_or_404(
        Calibration.objects.select_related("instrument", "blend", "performed_by", "standard"), pk=pk
    )
    components = calibration.component_rows()
    replicates = calibration.replicate_compositions()
    return render(
        request,
        "lab/calibration_detail.html",
        {
            "calibration": calibration,
            "components": components,
            "replicates": [(i + 1, run) for i, run in enumerate(replicates)],
            # The workbook's "RSD of % of Total Area": each run's area ÷ that run's total area.
            "area_fraction_rsd": [
                rsd_percent([c.areas[run] / sum(o.areas[run] for o in components) for run in range(3)])
                for c in components
            ],
            "verifications": calibration.verifications.select_related("performed_by", "standard").prefetch_related(
                "components__calibration_component__component"
            ),
            "samples": Sample.objects.filter(tests__calibration=calibration).distinct(),
        },
    )


@login_required
def verification_create(request, pk):
    calibration = get_object_or_404(Calibration.objects.select_related("blend"), pk=pk)
    if calibration.is_expired:
        messages.error(request, "This calibration has expired and is read-only.")
        return redirect(calibration)
    form = VerificationForm(request.POST or None, calibration=calibration)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            blend = calibration.blend
            blend.verification_rule = form.cleaned_data["verification_rule"]
            blend.verification_limit = form.cleaned_data["verification_limit"]
            blend.save(update_fields=["verification_rule", "verification_limit"])
            verification = form.save(commit=False)
            verification.calibration = calibration
            verification.performed_by = request.user
            verification.standard_cylinder = verification.standard.code
            verification.rule = blend.verification_rule
            verification.limit = blend.verification_limit
            verification.save()
            for cc, values in form.component_values():
                VerificationComponent.objects.create(
                    verification=verification, calibration_component=cc, **values
                )
        if verification.passed:
            messages.success(
                request,
                f"Verification passed. The calibration can be used until {verification.expires_on:%m/%d/%Y}.",
            )
        else:
            messages.error(request, "Verification failed. The calibration cannot be used.")
        return redirect(calibration)
    return render(
        request, "lab/verification_form.html", {"form": form, "calibration": calibration}
    )


# --- Standard cylinders --------------------------------------------------------


@login_required
def standard_list(request):
    standards = StandardCylinder.objects.select_related("blend", "prepared_by").prefetch_related(
        "components__component"
    )
    blend_id = request.GET.get("blend")
    if blend_id:
        standards = standards.filter(blend_id=blend_id)
    show = request.GET.get("show", "on_hand")
    if show == "on_hand":
        standards = standards.filter(status=StandardCylinder.STATUS_ON_HAND)
    return render(
        request,
        "lab/standard_list.html",
        {"standards": standards, "blends": Blend.objects.all(), "blend_id": blend_id, "show": show},
    )


@login_required
def standard_create(request):
    blend = _blend_from_request(request)
    if blend is None:
        return render(
            request,
            "lab/calibration_choose_blend.html",
            {"blends": Blend.objects.filter(active=True), "action": "Add standard", "title": "New standard cylinder"},
        )
    form = StandardCylinderForm(request.POST or None, blend=blend)
    if request.method == "POST" and form.is_valid():
        standard = form.save(user=request.user)
        messages.success(request, f"Standard {standard} added.")
        return redirect("lab:standard_list")
    return render(request, "lab/standard_form.html", {"form": form, "blend": blend})


@login_required
def standard_edit(request, pk):
    standard = get_object_or_404(StandardCylinder.objects.select_related("blend"), pk=pk)
    form = StandardCylinderForm(request.POST or None, instance=standard, blend=standard.blend)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Standard {standard} saved.")
        return redirect("lab:standard_list")
    return render(request, "lab/standard_form.html", {"form": form, "blend": standard.blend, "standard": standard})


# --- Non-condensables -----------------------------------------------------------


@login_required
def nc_calibration_create(request):
    instrument = _instrument_from_request(request)
    if instrument is None:
        return _choose(request, "Non-condensables calibration", with_blends=False)
    form = NCCalibrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        calibration = form.save(commit=False)
        calibration.instrument = instrument
        calibration.performed_by = request.user
        calibration.save()
        messages.success(request, "Non-condensables calibration saved. Run the verification next.")
        return redirect(calibration)
    return render(
        request, "lab/nc_form.html", {"form": form, "title": f"Non-condensables calibration on {instrument}"}
    )


@login_required
def nc_calibration_detail(request, pk):
    calibration = get_object_or_404(NCCalibration, pk=pk)
    return render(
        request,
        "lab/nc_detail.html",
        {
            "calibration": calibration,
            "verifications": calibration.verifications.select_related("performed_by"),
            "samples": Sample.objects.filter(tests__nc_calibration=calibration).distinct(),
        },
    )


@login_required
def nc_verification_create(request, pk):
    calibration = get_object_or_404(NCCalibration, pk=pk)
    if calibration.is_expired:
        messages.error(request, "This calibration has expired and is read-only.")
        return redirect(calibration)
    previous = NCVerification.objects.filter(calibration__instrument=calibration.instrument).order_by(
        "-performed_on", "-id"
    ).first() or NCVerification.objects.order_by("-performed_on", "-id").first()
    initial = {"max_error_percent": previous.max_error_percent} if previous else {}
    form = NCVerificationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        verification = form.save(commit=False)
        verification.calibration = calibration
        verification.performed_by = request.user
        verification.save()
        if verification.passed:
            messages.success(
                request,
                f"Verification passed. The calibration can be used until {verification.expires_on:%m/%d/%Y}.",
            )
        else:
            messages.error(request, "Verification failed. The calibration cannot be used.")
        return redirect(calibration)
    return render(
        request,
        "lab/nc_form.html",
        {"form": form, "title": f"Verify {calibration}", "calibration": calibration},
    )


# --- Instruments ------------------------------------------------------------------


def staff_required(view):
    """Only users with Admin access (staff) can manage instruments."""

    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied("Only administrators can manage instruments.")
        return view(request, *args, **kwargs)

    return wrapped


@staff_required
def instrument_list(request):
    add_form = InstrumentForm(request.POST if request.POST.get("action") == "add" else None, prefix="new")
    if request.method == "POST" and request.POST.get("action") == "add" and add_form.is_valid():
        instrument = add_form.save()
        messages.success(request, f"{instrument} added.")
        return redirect("lab:instrument_list")
    instruments = Instrument.objects.annotate(
        calibration_count=Count("calibrations", distinct=True) + Count("nc_calibrations", distinct=True)
    )
    rows = [(i, InstrumentForm(instance=i, prefix=f"i{i.pk}", form_id=f"instrument-{i.pk}")) for i in instruments]
    return render(request, "lab/instrument_list.html", {"rows": rows, "add_form": add_form})


@staff_required
@require_POST
def instrument_edit(request, pk):
    instrument = get_object_or_404(Instrument, pk=pk)
    if request.POST.get("action") == "delete":
        if instrument.calibrations.exists() or instrument.nc_calibrations.exists():
            messages.error(request, f"{instrument} has calibrations, so it can only be retired (untick Active).")
        else:
            instrument.delete()
            messages.success(request, f"{instrument} removed.")
        return redirect("lab:instrument_list")
    form = InstrumentForm(request.POST, instance=instrument, prefix=f"i{pk}")
    if form.is_valid():
        form.save()
        messages.success(request, f"{instrument} saved.")
    else:
        messages.error(request, "; ".join(e for errors in form.errors.values() for e in errors))
    return redirect("lab:instrument_list")


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
    pressure = request.GET.get("pressure") or Formula.PRESSURE_HIGH
    if pressure not in dict(Formula.PRESSURE_CHOICES):
        raise Http404("Unknown pressure.")
    other = Formula.PRESSURE_LOW if pressure == Formula.PRESSURE_HIGH else Formula.PRESSURE_HIGH
    current = method.current_formula(pressure)
    other_current = method.current_formula(other)
    # Always a new, unsaved version, pre-filled from the current one.
    draft = Formula(test_method=method, pressure=pressure)
    if current:
        draft.expression = current.expression
        draft.inputs = current.inputs
        draft.result_unit = current.result_unit
        draft.decimals = current.decimals
        draft.report_limit = current.report_limit
    # Offer to save for both pressures; ticked when they share a formula today.
    if current is None:
        both = other_current is None
    else:
        both = current.same_calculation(other_current)
    form = FormulaForm(request.POST or None, instance=draft, initial={"both_pressures": both})
    if request.method == "POST" and form.is_valid():
        formula = form.save(commit=False)
        pressures = [pressure, other] if form.cleaned_data["both_pressures"] else [pressure]
        saved = []
        with transaction.atomic():
            for p in pressures:
                formula.pk = None
                formula.pressure = p
                formula.version = (method.formulas.filter(pressure=p).aggregate(v=Max("version"))["v"] or 0) + 1
                formula.created_by = request.user
                formula.save()
                saved.append(str(formula))
        messages.success(request, " and ".join(saved) + " saved.")
        return redirect("lab:formula_list")
    return render(
        request,
        "lab/formula_form.html",
        {
            "form": form,
            "method": method,
            "current": current,
            "pressure_label": draft.get_pressure_display(),
            "other_label": dict(Formula.PRESSURE_CHOICES)[other],
        },
    )
