"""PDF generation for sample reports."""

from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Report, SampleTest, user_initials

_styles = getSampleStyleSheet()
_small = ParagraphStyle("small", parent=_styles["Normal"], fontSize=8, leading=10)
_cell = ParagraphStyle("cell", parent=_styles["Normal"], fontSize=9, leading=11)

_GRID = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef4")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
)


def report_filename(report):
    return f"{report.report_number}.pdf"


def report_path(report):
    year = (report.generated_at or timezone.now()).year
    return Path(settings.REPORTS_DIR) / str(year) / report_filename(report)


def _sample_block(sample):
    story = []
    info = [
        ["Sample #", sample.sample_number, "Received", f"{sample.received_on:%m/%d/%Y}"],
        [
            "Identification",
            Paragraph(escape(sample.identification), _cell),
            "LTO #",
            sample.lto or "—",
        ],
    ]
    if sample.blend:
        info.append(["Blend", sample.blend.name, "Phase", sample.get_phase_display()])
    table = Table(info, colWidths=[1.1 * inch, 3.2 * inch, 0.8 * inch, 1.4 * inch], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story += [table, Spacer(1, 6)]

    rows = [["Test", "Method", "Result", "Unit", "Analyst"]]
    footnotes = []
    tests = sample.tests.filter(
        status=SampleTest.STATUS_COMPLETED, results__isnull=False
    ).distinct().select_related("test_method", "completed_by", "calibration")
    for test in tests:
        analyst = user_initials(test.completed_by)
        results = list(test.results.all())
        for i, result in enumerate(results):
            name = test.test_method.name
            if result.label:
                name = f"{name} – {result.label}" if i == 0 else f"    {result.label}"
            rows.append(
                [
                    Paragraph(escape(name), _cell),
                    test.test_method.code if i == 0 else "",
                    result.display_value,
                    result.unit,
                    analyst if i == 0 else "",
                ]
            )
        if test.calibration:
            cal = test.calibration
            footnotes.append(
                f"{test.test_method.name}: calibration of {cal.blend} on "
                f"{cal.performed_on:%m/%d/%Y} (standard {cal.standard_cylinder})."
            )
    if len(rows) == 1:
        rows.append(["No completed results.", "", "", "", ""])
    table = Table(rows, colWidths=[2.6 * inch, 1.0 * inch, 1.2 * inch, 0.9 * inch, 0.8 * inch], hAlign="LEFT")
    table.setStyle(_GRID)
    story.append(table)
    for note in footnotes:
        story.append(Paragraph(escape(note), _small))
    if sample.comments:
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"<b>Comments:</b> {escape(sample.comments)}", _small))
    story.append(Spacer(1, 18))
    return story


def build_pdf(report: Report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"Report {report.report_number}",
    )
    story = [
        Paragraph(settings.LAB_NAME, _styles["Title"]),
        Paragraph("Analysis Report", _styles["Heading2"]),
        Paragraph(
            f"Report #: {report.report_number} &nbsp;&nbsp; "
            f"Date: {timezone.localdate():%m/%d/%Y}",
            _styles["Normal"],
        ),
        Spacer(1, 12),
    ]
    if report.uses_placeholder_formula():
        story.append(
            Paragraph(
                "<font color='red'><b>DRAFT – contains results from placeholder formulas "
                "that have not been confirmed by the lab.</b></font>",
                _styles["Normal"],
            )
        )
        story.append(Spacer(1, 8))
    for sample in report.ordered_samples():
        story += _sample_block(sample)
    if report.notes:
        story.append(Paragraph(f"<b>Notes:</b> {escape(report.notes)}", _styles["Normal"]))
    doc.build(story)
    return path


def generate(report: Report):
    """Build the PDF in the reports folder and record where it was saved."""
    report.generated_at = timezone.now()
    path = build_pdf(report, report_path(report))
    report.pdf_path = str(path)
    if report.status == Report.STATUS_DRAFT:
        report.status = Report.STATUS_GENERATED
    report.save()
    return path
