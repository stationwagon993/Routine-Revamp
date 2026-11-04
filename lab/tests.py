import datetime
import email
import tempfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from . import formulas, outlook, reports, services
from .models import (
    Blend,
    Instrument,
    NCCalibration,
    NCVerification,
    StandardComponent,
    StandardCylinder,
    Calibration,
    CalibrationComponent,
    Formula,
    Report,
    Sample,
    SampleTest,
    TestMethod,
    Verification,
    VerificationComponent,
    add_months,
    julian_code,
)


class FormulaEvaluationTests(TestCase):
    def test_arithmetic_and_functions(self):
        value = formulas.evaluate("(a - b) * 56.1 / m + sqrt(4)", {"a": 1.5, "b": 0.5, "m": 2})
        self.assertAlmostEqual(value, 30.05)

    def test_rejects_code(self):
        for bad in ["__import__('os')", "a.__class__", "[1, 2]", "open('x')", "a < 1 and a", "lambda: 1"]:
            with self.subTest(bad=bad), self.assertRaises(formulas.FormulaError):
                formulas.validate(bad, ["a"])

    def test_conditionals_for_reporting_limits(self):
        limit = "0.05 if mass_g < 5 else 0.01"
        self.assertEqual(formulas.evaluate(limit, {"mass_g": 1.5}), 0.05)
        self.assertEqual(formulas.evaluate(limit, {"mass_g": 7.5}), 0.01)
        with self.assertRaises(formulas.FormulaError):
            formulas.evaluate("mass_g < 5", {"mass_g": 1})  # a comparison alone isn't a number

    def test_rejects_undeclared_names(self):
        with self.assertRaisesMessage(formulas.FormulaError, "mass"):
            formulas.validate("volume / mass", ["volume"])

    def test_division_by_zero(self):
        with self.assertRaisesMessage(formulas.FormulaError, "Division by zero"):
            formulas.evaluate("a / b", {"a": 1, "b": 0})

    def test_non_numeric_input(self):
        with self.assertRaises(formulas.FormulaError):
            formulas.evaluate("a * 2", {"a": "abc"})

    def test_huge_exponent_blocked(self):
        with self.assertRaises(formulas.FormulaError):
            formulas.evaluate("a ** 1000", {"a": 10})


class DateHelperTests(TestCase):
    def test_julian_code(self):
        self.assertEqual(julian_code(datetime.date(2026, 7, 7)), "26188")
        self.assertEqual(julian_code(datetime.date(2026, 1, 1)), "26001")

    def test_add_months_clamps_day(self):
        self.assertEqual(add_months(datetime.date(2026, 8, 31), 6), datetime.date(2027, 2, 28))
        self.assertEqual(add_months(datetime.date(2026, 1, 15), 6), datetime.date(2026, 7, 15))


class LabTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("cs", password="pw", first_name="Carson", last_name="Smith")
        self.blend = Blend.objects.get(name="R-410A")
        self.gc = Instrument.objects.get(name="GC7890")
        self.r32, self.r125 = self.blend.components.order_by("order")

    def make_sample(self, **kwargs):
        defaults = {
            "identification": "Compressors, GCS 4, Refrigerant, Liquid",
            "sample_type": Sample.TYPE_REFRIGERANT,
            "received_on": datetime.date(2026, 7, 7),
            "blend": self.blend,
            "requested_by_email": "tech@example.com",
        }
        defaults.update(kwargs)
        return Sample.objects.create(**defaults)

    def make_calibration(self, on=None, verify_area_r32=10.0, verify=True):
        """Calibration with response factor 5.0 %/area for both components."""
        cal = Calibration.objects.create(
            instrument=self.gc,
            blend=self.blend,
            standard_cylinder="STD-001",
            performed_by=self.user,
            performed_on=on or datetime.date.today(),
        )
        cc32 = CalibrationComponent.objects.create(
            calibration=cal, component=self.r32, standard_percent=50.0,
            area_run1=9.9, area_run2=10.0, area_run3=10.1,
        )
        cc125 = CalibrationComponent.objects.create(
            calibration=cal, component=self.r125, standard_percent=50.0,
            area_run1=10.0, area_run2=10.0, area_run3=10.0,
        )
        if verify:
            v = Verification.objects.create(calibration=cal, performed_by=self.user)
            VerificationComponent.objects.create(
                verification=v, calibration_component=cc32, theoretical_percent=50.0,
                peak_area=verify_area_r32,
            )
            VerificationComponent.objects.create(
                verification=v, calibration_component=cc125, theoretical_percent=50.0, peak_area=10.0
            )
        return cal, cc32, cc125

    def add_test(self, sample, name):
        return SampleTest.objects.create(sample=sample, test_method=TestMethod.objects.get(name=name))


class SampleNumberTests(LabTestCase):
    def test_numbers_follow_julian_date_and_sequence(self):
        a = self.make_sample()
        b = self.make_sample()
        c = self.make_sample(received_on=datetime.date(2026, 7, 8))
        self.assertEqual([a.sample_number, b.sample_number, c.sample_number], ["26188-1", "26188-2", "26189-1"])


class CalibrationTests(LabTestCase):
    def test_expires_after_six_months(self):
        cal, _, _ = self.make_calibration(on=datetime.date(2026, 3, 10))
        self.assertEqual(cal.expires_on, datetime.date(2026, 9, 10))

    def test_verification_within_tolerance_passes(self):
        # 10.006 * 5.0 = 50.03 -> difference exactly 0.03
        cal, _, _ = self.make_calibration(verify_area_r32=10.006)
        self.assertTrue(cal.is_verified)
        self.assertEqual(cal.status, Calibration.STATUS_ACTIVE)
        self.assertEqual(self.blend.active_calibration(), cal)

    def test_verification_outside_tolerance_fails(self):
        cal, _, _ = self.make_calibration(verify_area_r32=10.01)  # 50.05%
        self.assertFalse(cal.is_verified)
        self.assertEqual(cal.status, Calibration.STATUS_FAILED)
        self.assertIsNone(self.blend.active_calibration())

    def verify(self, cal, cc32, cc125, areas, theoretical=(50.0, 50.0), max_error=1.0, rule=Blend.RULE_PERCENT, on=None):
        v = Verification.objects.create(
            calibration=cal, performed_by=self.user, rule=rule, limit=max_error,
            performed_on=on or datetime.date.today(),
        )
        for cc, area, theo in zip((cc32, cc125), areas, theoretical):
            VerificationComponent.objects.create(
                verification=v, calibration_component=cc, theoretical_percent=theo, peak_area=area
            )
        return v

    def test_verification_percent_error_is_relative_and_normalized(self):
        cal, cc32, cc125 = self.make_calibration(verify=False)  # response factor 5.0 for both
        # Raw 51% + 51% = 102% -> normalized 50/50: no error even though the raw values are off.
        v = self.verify(cal, cc32, cc125, areas=(10.2, 10.2))
        r32, r125 = v.component_rows()
        self.assertAlmostEqual(r32.calculated_percent, 50.0)
        self.assertAlmostEqual(r32.percent_error, 0.0)
        self.assertTrue(v.passed)
        # 30/70 standard measured as 30.3/69.7: R-32 is off by 1.0% of 30, R-125 by 0.43%.
        v = self.verify(cal, cc32, cc125, areas=(6.06, 13.94), theoretical=(30.0, 70.0), max_error=1.0)
        r32, r125 = v.component_rows()
        self.assertAlmostEqual(r32.calculated_percent, 30.3)
        self.assertAlmostEqual(r32.percent_error, 1.0)
        self.assertTrue(v.passed)  # exactly at the limit passes
        v = self.verify(cal, cc32, cc125, areas=(6.06, 13.94), theoretical=(30.0, 70.0), max_error=0.9)
        self.assertFalse(v.passed)
        self.assertEqual(cal.status, Calibration.STATUS_FAILED)  # latest verification decides

    def test_verification_within_points_band(self):
        cal, cc32, cc125 = self.make_calibration(verify=False)
        # 30/70 standard measured as 30.3/69.7: 0.3 points off.
        v = self.verify(cal, cc32, cc125, areas=(6.06, 13.94), theoretical=(30.0, 70.0), max_error=0.3049, rule=Blend.RULE_POINTS)
        self.assertTrue(v.passed)
        v = self.verify(cal, cc32, cc125, areas=(6.06, 13.94), theoretical=(30.0, 70.0), max_error=0.25, rule=Blend.RULE_POINTS)
        self.assertFalse(v.passed)

    def test_verification_expires(self):
        cal, cc32, cc125 = self.make_calibration(verify=False)
        today = datetime.date.today()
        v = self.verify(cal, cc32, cc125, areas=(10, 10), on=today - datetime.timedelta(days=8))
        self.assertEqual(v.expires_on, today - datetime.timedelta(days=1))  # 7 days by default
        self.assertEqual(cal.status, Calibration.STATUS_REVERIFY)
        self.assertIsNone(self.blend.active_calibration())
        self.verify(cal, cc32, cc125, areas=(10, 10))
        self.assertEqual(self.blend.active_calibration(), cal)
        # R-514A verifications are good for 1 day (from the GC workbook).
        self.assertEqual(Blend.objects.get(name="R-514A").verification_valid_days, 1)

    def test_workbook_rules_loaded(self):
        rules = {b.name: (b.verification_rule, b.verification_limit) for b in Blend.objects.all()}
        self.assertEqual(rules["R-454B"], (Blend.RULE_POINTS, 0.5049))
        self.assertEqual(rules["R-410A"], (Blend.RULE_POINTS, 0.3049))
        self.assertIn("R-452A", rules)

    def test_standard_composition_expiry_and_remaining(self):
        standard = StandardCylinder.objects.create(
            blend=self.blend, code="R-410A-110226-A", prepared_on=datetime.date(2026, 11, 2),
            cylinder_gross_g=1454.42, cylinder_tare_g=986.16, full_fill_g=1000,
        )
        StandardComponent.objects.create(cylinder=standard, component=self.r32, mass_g=265.86)
        StandardComponent.objects.create(cylinder=standard, component=self.r125, mass_g=119.86)
        composition = standard.composition()
        self.assertAlmostEqual(composition[self.r32.pk], 265.86 / 385.72 * 100)
        self.assertEqual(standard.expires_on, datetime.date(2026, 11, 9))
        self.assertTrue(standard.is_usable(datetime.date(2026, 11, 9)))
        self.assertFalse(standard.is_usable(datetime.date(2026, 11, 10)))
        self.assertAlmostEqual(standard.remaining_fraction, 0.46826)
        self.assertTrue(standard.needs_replacing)

    def test_unverified_and_expired_are_not_active(self):
        self.make_calibration(verify=False)
        self.make_calibration(on=datetime.date.today() - datetime.timedelta(days=200))
        self.assertIsNone(self.blend.active_calibration())


class ResultTests(LabTestCase):
    def test_formula_result_saved_and_completed(self):
        sample = self.make_sample()
        test = self.add_test(sample, "TAN")
        formula = TestMethod.objects.get(name="TAN").current_formula()
        services.save_results(
            test,
            self.user,
            {
                "formula_id": formula.pk,
                "input_mass_g": "5.61",
                "input_start_ml": "0",
                "input_finish_ml": "1.5",
                "input_blank_start_ml": "0",
                "input_blank_finish_ml": "0.5",
            },
        )
        test.refresh_from_db()
        self.assertEqual(test.status, SampleTest.STATUS_COMPLETED)
        self.assertEqual(test.cell_text, "CS")
        result = test.results.get()
        self.assertAlmostEqual(result.value, 1.0)
        self.assertEqual(result.formula, formula)

    def test_blend_requires_verified_calibration(self):
        sample = self.make_sample()
        test = self.add_test(sample, "Blend Analysis")
        with self.assertRaisesMessage(services.ResultError, "no current, verified R-410A calibration on any instrument"):
            services.save_results(test, self.user, {})

    def test_blend_results_use_response_factors(self):
        cal, cc32, cc125 = self.make_calibration()
        sample = self.make_sample()
        test = self.add_test(sample, "Blend Analysis")
        services.save_results(test, self.user, {f"area_{cc32.pk}": "10.2", f"area_{cc125.pk}": "9.8"})
        values = {r.label: r.value for r in test.results.all()}
        self.assertAlmostEqual(values["R-32"], 51.0)
        self.assertAlmostEqual(values["R-125"], 49.0)
        test.refresh_from_db()
        self.assertEqual(test.calibration, cal)

    def test_blend_results_are_normalized_to_100(self):
        cal, cc32, cc125 = self.make_calibration()
        test = self.add_test(self.make_sample(), "Blend Analysis")
        services.save_results(test, self.user, {f"area_{cc32.pk}": "12", f"area_{cc125.pk}": "8.4"})  # raw 60 + 42
        values = {r.label: r.value for r in test.results.all()}
        self.assertAlmostEqual(values["R-32"], 60 / 102 * 100)
        self.assertAlmostEqual(values["R-125"], 42 / 102 * 100)
        self.assertAlmostEqual(sum(values.values()), 100.0)

    def test_every_formula_test_has_both_pressures(self):
        for method in TestMethod.objects.filter(calc_type=TestMethod.CALC_FORMULA):
            with self.subTest(method=method.name):
                high = method.current_formula(Formula.PRESSURE_HIGH)
                low = method.current_formula(Formula.PRESSURE_LOW)
                self.assertNotEqual(high, low)
                self.assertTrue(high.same_calculation(low))  # copied, so results don't change

    def test_formula_depends_on_refrigerant_pressure(self):
        method = TestMethod.objects.get(name="High Boiling Residue")
        high = method.current_formula(Formula.PRESSURE_HIGH)
        low = Formula.objects.create(
            test_method=method, pressure=Formula.PRESSURE_LOW, version=99,
            expression="residue_g / mass_g * 50", inputs=high.inputs, result_unit="%",
        )
        for refrigerant, expected in [("R-1233zd(E)", low), ("R-1336MZZ(Z)", low), ("R-514A", low),
                                      ("R-454B", high), ("R-999X", high), ("", high)]:
            with self.subTest(refrigerant=refrigerant):
                sample = self.make_sample(refrigerant_type=refrigerant)
                self.assertEqual(method.current_formula(sample.pressure), expected)

    def test_manual_result(self):
        sample = self.make_sample()
        test = self.add_test(sample, "Viscosity")
        services.save_results(test, self.user, {"manual_value": "32.15"})
        result = test.results.get()
        self.assertEqual(result.display_value, "32.15")
        self.assertEqual(result.unit, "cSt")

    def test_new_formula_version_keeps_old_results(self):
        sample = self.make_sample()
        test = self.add_test(sample, "Moisture")
        v1 = TestMethod.objects.get(name="Moisture").current_formula()
        services.save_results(test, self.user, {"formula_id": v1.pk, "input_rep1_ppm": "4", "input_rep2_ppm": "5", "input_rep3_ppm": "6"})
        v2 = Formula.objects.create(
            test_method=v1.test_method, version=v1.version + 1, expression="rep1_ppm * 2",
            inputs=v1.inputs, result_unit="ppm",
        )
        self.assertEqual(v1.test_method.current_formula(), v2)
        self.assertEqual(test.results.get().formula, v1)


@override_settings(EMAIL_SIGNATURE="Lab team")
class ReportTests(LabTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_generate_email_and_mark_sent(self):
        sample = self.make_sample(identification="Reclaimed tank <lot 8> & drum")
        SampleTest.objects.create(sample=sample, test_method=TestMethod.objects.get(name="Results Reported"))
        test = self.add_test(sample, "Viscosity")
        services.save_results(test, self.user, {"manual_value": "32.1"})
        report = Report.objects.create(
            report_number=Report.next_number(sample), recipient_email="tech@example.com"
        )
        report.samples.add(sample)

        with self.settings(REPORTS_DIR=self.tmp.name):
            path = reports.generate(report)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes()[:4], b"%PDF")
        self.assertEqual(report.status, Report.STATUS_GENERATED)

        message = email.message_from_bytes(outlook.eml_bytes(report))
        self.assertEqual(message["X-Unsent"], "1")
        self.assertEqual(message["To"], "tech@example.com")
        self.assertIn("26188-1", message["Subject"])
        self.assertEqual(message.get_payload()[1].get_filename(), f"{report.report_number}.pdf")

        services.mark_report_sent(report, self.user)
        reported = sample.tests.get(test_method__name="Results Reported")
        self.assertEqual(reported.status, SampleTest.STATUS_COMPLETED)

    def test_report_numbers_are_unique(self):
        sample = self.make_sample()
        first = Report.objects.create(report_number=Report.next_number(sample))
        self.assertEqual(first.report_number, "R26188-1")
        self.assertEqual(Report.next_number(sample), "R26188-1-2")


class ViewTests(LabTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("lab:sample_log"))
        self.assertEqual(response.status_code, 302)

    def intake(self, **answers):
        data = {
            "submitter": "Carson Steichen",
            "received_on": "2026-07-07",
            "sampled_on": "2026-07-06",
            "requested_by_name": "Jordan Lee",
            "lto": "108836",
            "comments": "Routine check",
            "refrigerant": "R-410A",
            "lubricant": "Solest 120",
        }
        data.update(answers)
        return self.client.post(reverse("lab:sample_create"), data)

    def test_compressor_sample(self):
        moisture = TestMethod.objects.get(name="Moisture")
        blend = TestMethod.objects.get(name="Blend Analysis")
        response = self.intake(
            origin="Lab 11", lab11_area="Compressor Group", gcs_stand="GCS 4",
            sample_type="refrigerant", phase="liquid", tests=[moisture.pk, blend.pk],
        )
        sample = Sample.objects.get()
        self.assertRedirects(response, sample.get_absolute_url())
        self.assertEqual(sample.identification, "Compressors, GCS 4, Refrigerant, Liquid")
        self.assertEqual(sample.blend, self.blend)  # matched from the refrigerant type
        self.assertEqual(sample.origin_area, "Compressor Group")
        names = set(sample.tests.values_list("test_method__name", flat=True))
        self.assertEqual(names, {"Moisture", "Blend Analysis", "Results Reported"})

    def test_unit_sample_with_other_answers(self):
        tan = TestMethod.objects.get(name="TAN")
        self.intake(
            origin="Lab 11", lab11_area="Water Cooled Chiller", unit_id="7457",
            refrigerant="__other__", refrigerant_other="R-1336mzz(Z)",
            sample_type="oil", tests=[tan.pk],
        )
        sample = Sample.objects.get()
        self.assertEqual(sample.identification, "Water Cooled, 7457, Oil")
        self.assertEqual(sample.refrigerant_type, "R-1336mzz(Z)")
        self.assertEqual(sample.phase, "")

    def test_new_tank_and_drum(self):
        blend = TestMethod.objects.get(name="Blend Analysis")
        tan = TestMethod.objects.get(name="TAN")
        self.intake(origin="New Refrigerant Tank", lot_number="8", phase="expanded", tests=[blend.pk])
        self.intake(origin="New Lubricant Drum", lot_number="22", tests=[tan.pk])
        tank, drum = Sample.objects.order_by("sequence")
        self.assertEqual(tank.identification, "New Refrigerant Tank, Lot 8, Expanded Liquid")
        self.assertEqual(tank.sample_type, Sample.TYPE_REFRIGERANT)
        self.assertEqual(drum.identification, "New Lubricant Drum, Lot 22, Oil")
        self.assertEqual(drum.sample_type, Sample.TYPE_OIL)
        self.assertEqual(drum.refrigerant_type, "")

    def test_branch_questions_are_required(self):
        moisture = TestMethod.objects.get(name="Moisture")
        response = self.intake(origin="Lab 11", lab11_area="Compressor Group", sample_type="refrigerant", tests=[moisture.pk])
        self.assertFalse(Sample.objects.exists())
        errors = response.context["form"].errors
        self.assertIn("gcs_stand", errors)
        self.assertIn("phase", errors)
        response = self.intake(origin="Plant 7", plant7_location="__other__", sample_type="refrigerant", phase="liquid", tests=[moisture.pk])
        errors = response.context["form"].errors
        self.assertIn("plant7_location_other", errors)
        self.assertIn("plant7_refrigerant", errors)
        self.assertIn("unit_id", errors)

    def test_intake_form_fields(self):
        form = self.client.get(reverse("lab:sample_create")).context["form"]
        self.assertEqual(form["submitter"].value(), None)  # typed in, not the logged-in user
        self.assertNotIn("reason", form.fields)
        self.assertNotIn("identification", form.fields)
        self.assertEqual(form["submitter"].help_text, "")
        offered = set(form.fields["tests"].queryset.values_list("name", flat=True))
        self.assertNotIn("Miscellaneous", offered)
        self.assertNotIn("Test Plan Submitted", offered)

    def test_edit_rebuilds_identification(self):
        tan = TestMethod.objects.get(name="TAN")
        self.intake(origin="Plant 4", unit_id="U1", sample_type="oil", tests=[tan.pk])
        sample = Sample.objects.get()
        self.client.post(reverse("lab:sample_edit", args=[sample.pk]), {
            "submitter": "Pat Kim", "received_on": "2026-07-07", "sampled_on": "2026-07-06",
            "requested_by_name": "Jordan Lee", "lto": "108836", "origin": "Plant 4",
            "unit_id": "U2", "refrigerant": "R-410A", "lubricant": "Solest 120", "sample_type": "oil",
        })
        sample.refresh_from_db()
        self.assertEqual(sample.identification, "Plant 4, U2, Oil")

    def test_identification_matches_excel_log(self):
        tests = [TestMethod.objects.get(name="Moisture").pk]
        self.intake(origin="Lab 11", lab11_area="Unitary", unit_id="E137", sample_type="refrigerant", phase="liquid", tests=tests)
        self.intake(origin="Lab 11", lab11_area="Reclaimed Refrigerant Tank", unit_id="014323", sample_type="refrigerant", phase="liquid", tests=tests)
        self.intake(origin="Plant 7", plant7_location="Bay B South", plant7_refrigerant="R-514A", unit_id="U9", sample_type="refrigerant", phase="liquid", tests=tests)
        self.assertEqual(
            list(Sample.objects.order_by("sequence").values_list("identification", flat=True)),
            ["Unitary, E137, Refrigerant, Liquid", "Reclaimed Tank, Tank 014323, Liquid", "Plant 7, Bay B South"],
        )

    def test_elto_required(self):
        tan = TestMethod.objects.get(name="TAN")
        response = self.intake(origin="New Lubricant Drum", lot_number="22", lto="", tests=[tan.pk])
        self.assertIn("lto", response.context["form"].errors)
        self.assertFalse(Sample.objects.exists())

    def test_no_blend_analysis_for_single_component_refrigerants(self):
        blend = TestMethod.objects.get(name="Blend Analysis")
        for answers in [
            {"refrigerant": "R-134a"},
            {"refrigerant": "__other__", "refrigerant_other": "R1234ZE(E)"},
        ]:
            with self.subTest(**answers):
                response = self.intake(origin="Plant 4", unit_id="U1", sample_type="refrigerant", phase="liquid", tests=[blend.pk], **answers)
                self.assertIn("single-component", str(response.context["form"].errors["tests"]))
        self.assertFalse(Sample.objects.exists())
        page = self.client.get(reverse("lab:sample_create"))
        self.assertContains(page, 'data-single-component="r1233zde r1234zee r134a r22"')

    def test_tests_must_fit_sample_type(self):
        non_cond = TestMethod.objects.get(name="Non-Condensables")
        response = self.intake(origin="Plant 4", unit_id="U1", sample_type="refrigerant", phase="liquid", tests=[non_cond.pk])
        self.assertIn("tests", response.context["form"].errors)
        self.assertFalse(Sample.objects.exists())

    def test_blend_comes_from_refrigerant_and_can_be_added_later(self):
        blend_test = TestMethod.objects.get(name="Blend Analysis")
        page = self.client.get(reverse("lab:sample_create"))
        self.assertNotIn("blend", page.context["form"].fields)
        self.intake(origin="Plant 4", unit_id="U1", refrigerant="__other__", refrigerant_other="R-999X", sample_type="refrigerant", phase="liquid", tests=[blend_test.pk])
        sample = Sample.objects.get()  # Logged even though R-999X isn't set up yet.
        self.assertIsNone(sample.blend)
        self.assertContains(self.client.get(sample.get_absolute_url()), "set up as a blend yet")
        new_blend = Blend.objects.create(name="R-999X")
        self.client.get(sample.get_absolute_url())
        sample.refresh_from_db()
        self.assertEqual(sample.blend, new_blend)

    def test_no_unit_id_for_compressor_group(self):
        moisture = TestMethod.objects.get(name="Moisture")
        self.intake(origin="Lab 11", lab11_area="Compressor Group", gcs_stand="GCS 4", unit_id="stale",
                    sample_type="refrigerant", phase="liquid", tests=[moisture.pk])
        self.assertEqual(Sample.objects.get().unit_id, "")
        response = self.intake(origin="Lab 11", lab11_area="Air Cooled Chiller", sample_type="refrigerant", phase="liquid", tests=[moisture.pk])
        self.assertIn("unit_id", response.context["form"].errors)

    def test_edit_keeps_answers(self):
        tan = TestMethod.objects.get(name="TAN")
        self.intake(origin="Plant 7", plant7_location="Bay B North", plant7_refrigerant="R-514A", unit_id="U9", sample_type="oil", tests=[tan.pk])
        sample = Sample.objects.get()
        page = self.client.get(reverse("lab:sample_edit", args=[sample.pk]))
        form = page.context["form"]
        self.assertEqual(form["plant7_location"].value(), "Bay B North")
        self.assertEqual(form["plant7_refrigerant"].value(), "R-514A")

    def test_sample_log_columns_and_filters(self):
        moisture = TestMethod.objects.get(name="Moisture")
        tan = TestMethod.objects.get(name="TAN")
        a = self.make_sample(received_on=datetime.date(2026, 10, 26), refrigerant_type="R-454B",
                             lubricant_type="CPI 2931 AW", sampled_on=datetime.date(2026, 10, 25),
                             submitter="Logan Kiasau", requested_by_name="Tejesh Dube")
        b = self.make_sample(received_on=datetime.date(2026, 10, 27))
        c = self.make_sample(received_on=datetime.date(2026, 10, 28))
        self.add_test(a, "Moisture")
        self.add_test(b, "TAN")
        done = self.add_test(c, "Moisture")
        done.status = SampleTest.STATUS_COMPLETED
        done.save()

        def shown(**params):
            response = self.client.get(reverse("lab:sample_log"), params)
            return {sample.pk for sample, _ in response.context["rows"]}

        self.assertEqual(shown(tests=[moisture.pk]), {a.pk})
        self.assertEqual(shown(tests=[moisture.pk, tan.pk]), {a.pk, b.pk})
        self.assertEqual(shown(**{"from": "2026-10-27"}), {b.pk, c.pk})
        self.assertEqual(shown(**{"from": "2026-10-26", "to": "2026-10-27"}), {a.pk, b.pk})
        self.assertEqual(shown(**{"from": "2026-02-30"}), {a.pk, b.pk, c.pk})
        summary = self.client.get(reverse("lab:sample_log"), {"tests": [moisture.pk], "from": "2026-10-26"})
        self.assertEqual(summary.context["filter_summary"], "Needs Moisture · From 10/26/2026")
        self.assertContains(self.client.get(reverse("lab:sample_log")), "All samples")

        page = self.client.get(reverse("lab:sample_log")).content.decode()
        headers = ["LTO", "Refrigerant Type", "Lubricant", "Sample Date", "Submitter", "Engineer", "Comments"]
        positions = [page.index(f">{h}</th>") for h in headers]
        self.assertEqual(positions, sorted(positions))
        for text in ["R-454B", "CPI 2931 AW", "10/25/2026", "Logan Kiasau", "Tejesh Dube"]:
            self.assertIn(text, page)

    def test_log_columns_put_other_general_tests_after_oil(self):
        response = self.client.get(reverse("lab:sample_log"))
        names = [m.name for _, methods in response.context["column_groups"] for m in methods]
        self.assertEqual(names[0], "Results Reported")
        self.assertLess(names.index("Wear Metals by ICP-OES"), names.index("Determination of Anions in Water"))
        self.assertEqual(names[-1], "Unknown (Chemist Input Needed)")
        # The filter dropdown keeps its order.
        self.assertEqual([g for g, _ in response.context["groups"]], ["General", "Refrigerant", "Oil"])

    def test_report_addressed_to_engineer(self):
        sample = self.make_sample(requested_by_name="Lee, Jordan")
        response = self.client.post(reverse("lab:report_create", args=[sample.pk]))
        report = Report.objects.get()
        self.assertRedirects(response, report.get_absolute_url())
        self.assertEqual(report.recipient_email, "Lee, Jordan")
        self.assertEqual(outlook.compose(report)["to"], ["Lee, Jordan"])

    def test_intake_lists_general_tests_last(self):
        page = self.client.get(reverse("lab:sample_create")).content.decode()
        self.assertLess(page.index("<h3>Refrigerant</h3>"), page.index("<h3>Oil</h3>"))
        self.assertLess(page.index("<h3>Oil</h3>"), page.index("<h3>General</h3>"))

    def test_pages_render(self):
        cal, _, _ = self.make_calibration()
        sample = self.make_sample(lto="108782")
        self.make_sample(lto="108782")
        for name in ["TAN", "Blend Analysis", "Viscosity", "Results Reported"]:
            self.add_test(sample, name)
        report = Report.objects.create(report_number="R1")
        report.samples.add(sample)
        urls = [
            reverse("lab:sample_log"),
            reverse("lab:sample_log") + "?q=water&from=2026-07-01&tests=1",
            reverse("lab:sample_create"),
            sample.get_absolute_url(),
            reverse("lab:sample_edit", args=[sample.pk]),
            reverse("lab:report_list"),
            report.get_absolute_url(),
            reverse("lab:calibration_list"),
            reverse("lab:calibration_list") + f"?gc={self.gc.pk}&view=archive",
            reverse("lab:calibration_create"),
            reverse("lab:calibration_create") + f"?gc={self.gc.pk}&blend={self.blend.pk}",
            cal.get_absolute_url(),
            reverse("lab:verification_create", args=[cal.pk]),
            reverse("lab:formula_list"),
            reverse("lab:formula_new_version", args=[TestMethod.objects.get(name="TAN").pk]),
            reverse("lab:standard_list") + "?show=all",
            reverse("lab:standard_create"),
            reverse("lab:standard_create") + f"?blend={self.blend.pk}",
            reverse("lab:nc_calibration_create"),
            reverse("lab:nc_calibration_create") + f"?gc={self.gc.pk}",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_live_calculation_endpoint(self):
        sample = self.make_sample()
        test = self.add_test(sample, "Moisture")
        formula = test.test_method.current_formula()
        url = reverse("lab:test_calculate", args=[test.pk])
        data = {"formula_id": formula.pk, "input_rep1_ppm": "4", "input_rep2_ppm": "5", "input_rep3_ppm": "6"}
        ok = self.client.post(url, data).json()
        self.assertEqual(ok, {"ok": True, "results": [{"label": "", "value": "5.0", "unit": "ppm"}]})
        bad = self.client.post(url, dict(data, input_rep3_ppm="abc")).json()
        self.assertFalse(bad["ok"])
        self.assertFalse(test.results.exists())

    def add_standard(self, code, r32_g, r125_g, prepared_on=None):
        response = self.client.post(
            reverse("lab:standard_create") + f"?blend={self.blend.pk}",
            {
                "code": code, "prepared_on": (prepared_on or datetime.date.today()).isoformat(),
                "status": StandardCylinder.STATUS_ON_HAND,
                f"mass_{self.r32.pk}": r32_g, f"mass_{self.r125.pk}": r125_g,
            },
        )
        self.assertRedirects(response, reverse("lab:standard_list"))
        return StandardCylinder.objects.get(code=code)

    def test_calibration_and_verification_through_views(self):
        today = datetime.date.today().isoformat()
        cal_std = self.add_standard("R-410A-A", "100", "100")  # 50/50
        ver_std = self.add_standard("R-410A-B", "60", "140")  # 30/70
        old_std = self.add_standard("R-410A-OLD", "100", "100", prepared_on=datetime.date.today() - datetime.timedelta(days=8))
        runs = {
            f"run1_{self.r32.pk}": "10", f"run2_{self.r32.pk}": "10", f"run3_{self.r32.pk}": "10",
            f"run1_{self.r125.pk}": "20", f"run2_{self.r125.pk}": "20", f"run3_{self.r125.pk}": "20",
        }
        url = reverse("lab:calibration_create") + f"?gc={self.gc.pk}&blend={self.blend.pk}"
        response = self.client.post(url, {"standard": old_std.pk, "performed_on": today, **runs})
        self.assertIn("standard", response.context["form"].errors)  # past its 7 days
        response = self.client.post(url, {"standard": cal_std.pk, "performed_on": today, **runs})
        cal = Calibration.objects.get()
        self.assertRedirects(response, cal.get_absolute_url())
        self.assertEqual(cal.standard_cylinder, "R-410A-A")
        cc32, cc125 = cal.components.all()
        self.assertEqual(cc32.standard_percent, 50.0)  # from the standard's masses
        self.assertAlmostEqual(cc32.response_factor, 5.0)
        self.assertAlmostEqual(cc125.response_factor, 2.5)
        # 30/70 standard: areas 6 and 28 give raw 30% + 70%.
        self.client.post(
            reverse("lab:verification_create", args=[cal.pk]),
            {
                "standard": ver_std.pk, "performed_on": today,
                "verification_rule": Blend.RULE_POINTS, "verification_limit": "0.3049",
                f"area_{cc32.pk}": "6.06", f"area_{cc125.pk}": "27.88", f"rt_{cc32.pk}": "3.95",
                "purity": "99.98", "air_area": "100.1", "air_rt": "2.593",
            },
        )
        verification = cal.latest_verification
        self.assertEqual(verification.standard, ver_std)
        self.assertEqual((verification.rule, verification.limit), (Blend.RULE_POINTS, 0.3049))
        r32, r125 = verification.component_rows()
        self.assertAlmostEqual(r32.theoretical_percent, 30.0)
        self.assertAlmostEqual(r32.calculated_percent, 30.3)
        self.assertTrue(verification.passed)
        self.assertEqual(self.blend.active_calibration(), cal)
        self.blend.refresh_from_db()
        self.assertEqual(self.blend.verification_limit, 0.3049)  # remembered for the blend
        self.assertContains(self.client.get(cal.get_absolute_url()), "±0.3049 points")
        self.assertContains(self.client.get(reverse("lab:calibration_list")), "R-410A-B")

    def test_formula_new_version_through_view(self):
        method = TestMethod.objects.get(name="Acidity")
        response = self.client.post(
            reverse("lab:formula_new_version", args=[method.pk]),
            {
                "inputs_text": "v | Volume | mL\nm | Mass | g",
                "expression": "v / m * 1000",
                "result_unit": "ppm",
                "decimals": "1",
                "notes": "",
            },
        )
        self.assertRedirects(response, reverse("lab:formula_list"))
        current = method.current_formula()
        self.assertEqual(current.version, 3)  # v1 placeholder, v2 the workbook formula
        self.assertFalse(current.is_placeholder)

    def test_formula_for_one_or_both_pressures_through_view(self):
        method = TestMethod.objects.get(name="High Boiling Residue")
        url = reverse("lab:formula_new_version", args=[method.pk]) + "?pressure=low"
        # Both pressures share a formula today, so "both" starts ticked.
        self.assertTrue(self.client.get(url).context["form"]["both_pressures"].value())
        data = {"inputs_text": "r | Residue | g\nm | Mass | g", "expression": "r / m * 100", "result_unit": "%", "decimals": "3"}
        self.assertRedirects(self.client.post(url, data), reverse("lab:formula_list"))  # unticked
        low = method.current_formula(Formula.PRESSURE_LOW)
        self.assertEqual((low.version, low.expression), (3, "r / m * 100"))
        self.assertEqual(method.current_formula(Formula.PRESSURE_HIGH).version, 2)  # untouched
        self.assertFalse(self.client.get(url).context["form"]["both_pressures"].value())
        moisture = TestMethod.objects.get(name="Moisture")
        both = dict(data, expression="r / m", both_pressures="on")
        self.client.post(reverse("lab:formula_new_version", args=[moisture.pk]), both)
        for pressure in (Formula.PRESSURE_HIGH, Formula.PRESSURE_LOW):
            self.assertEqual(moisture.current_formula(pressure).expression, "r / m")
        sample = self.make_sample(refrigerant_type="R-514A")
        self.add_test(sample, "High Boiling Residue")
        page = self.client.get(sample.get_absolute_url())
        self.assertContains(page, "Low-pressure refrigerants formula")
        self.assertContains(self.client.get(reverse("lab:formula_list")), "High/medium-pressure refrigerants")

    def test_formula_view_rejects_unsafe_expression(self):
        method = TestMethod.objects.get(name="Acidity")
        response = self.client.post(
            reverse("lab:formula_new_version", args=[method.pk]),
            {"inputs_text": "v | Volume | mL", "expression": "__import__('os')", "decimals": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(method.current_formula().version, 2)


class WorkbookFormulaTests(LabTestCase):
    """The formulas from the Master Routine Spreadsheet give the spreadsheet's answers."""

    def result(self, test_name, refrigerant="R-134a", **inputs):
        sample = self.make_sample(refrigerant_type=refrigerant)
        test = self.add_test(sample, test_name)
        formula = test.test_method.current_formula(sample.pressure)
        data = {"formula_id": formula.pk, **{f"input_{k}": str(v) for k, v in inputs.items()}}
        return services.compute_results(test, data)[0][0]

    def test_hbr_uses_refrigerant_density(self):
        r = self.result("High Boiling Residue", cyl_tare_g=900, cyl_gross_g=1000, beaker_tare_g=50, beaker_final_g=50.05)
        self.assertAlmostEqual(r.value, (0.05 / 0.9) / (100 / 1.21) * 100)
        self.assertEqual(r.unit, "% vol")
        self.assertEqual(r.inputs["refrigerant_density"], 1.21)
        with self.assertRaisesMessage(services.ResultError, "No liquid density is set up for R-22"):
            self.result("High Boiling Residue", refrigerant="R-22", cyl_tare_g=900, cyl_gross_g=1000, beaker_tare_g=50, beaker_final_g=50.05)

    def test_acidity_ocr_ri_moisture(self):
        self.assertAlmostEqual(self.result("Acidity", mass_g=100, start_ml=1, finish_ml=1.5).value, 0.5 * 0.01 * 36500 / 100)
        self.assertAlmostEqual(
            self.result("Oil Circulation Rates", cyl_tare_g=900, cyl_gross_g=1000, beaker_tare_g=50, beaker_final_g=50.5).value, 0.5
        )
        ri = self.result("Refractive Index", ri_contaminant=1.45667, ri_base=1.45478, ri_sample=1.45486)
        self.assertEqual(ri.display_value, "96")  # the spreadsheet's 95.77, reported to a whole %
        self.assertAlmostEqual(self.result("Moisture", rep1_ppm=10, rep2_ppm=12, rep3_ppm=14).value, 12)

    def test_tan_reporting_limit_depends_on_method(self):
        tan = self.result("TAN", mass_g=7.5, start_ml=0, finish_ml=0.06, blank_start_ml=0, blank_finish_ml=0.05)
        self.assertAlmostEqual(tan.value, 0.01 * 0.1 * 56.1 / 7.5)  # 0.0075
        self.assertEqual(tan.display_value, "<0.01")  # 5–10 g method
        tan = self.result("TAN", mass_g=1.5, start_ml=0, finish_ml=0.06, blank_start_ml=0, blank_finish_ml=0.05)
        self.assertEqual(tan.display_value, "<0.05")  # 1–2 g method
        tan = self.result("TAN", mass_g=5.61, start_ml=0, finish_ml=1.5, blank_start_ml=0, blank_finish_ml=0.5)
        self.assertEqual(tan.text_value, "")
        self.assertAlmostEqual(tan.value, 1.0)


class NonCondensablesTests(LabTestCase):
    def test_calibrate_verify_and_report(self):
        self.client.force_login(self.user)
        today = datetime.date.today().isoformat()
        areas = [311.5, 312.9, 321.3, 322.8, 322.2, 322.2, 320.5, 313.5, 314.3, 323.8]  # the workbook's NC sheet
        response = self.client.post(
            reverse("lab:nc_calibration_create") + f"?gc={self.gc.pk}",
            {"performed_on": today, "standard_name": "Nitrogen in Helium", "n2_percent": "1.48",
             **{f"area_{i}": a for i, a in enumerate(areas, start=1)}},
        )
        cal = NCCalibration.objects.get()
        self.assertRedirects(response, cal.get_absolute_url())
        self.assertAlmostEqual(cal.area_per_percent, 215.2027027, places=5)  # workbook: 215.2027
        self.assertIsNone(NCCalibration.active())  # not verified yet

        self.client.post(
            reverse("lab:nc_verification_create", args=[cal.pk]),
            {"performed_on": today, "standard_name": "Nitrogen in Helium", "n2_percent": "0.789",
             "max_error_percent": "6", "area_1": "161.5", "area_2": "159.2", "area_3": "158.6", "area_4": "162.8"},
        )
        verification = NCVerification.objects.get()
        self.assertAlmostEqual(verification.result_percent, 0.7459246, places=6)  # workbook: 0.0074592 (fraction)
        self.assertAlmostEqual(verification.percent_error, -5.4594871, places=5)  # workbook: -5.46%
        self.assertTrue(verification.passed)
        self.assertEqual(NCCalibration.active(), cal)

        sample = self.make_sample()
        test = self.add_test(sample, "Non-Condensables")
        services.save_results(test, self.user, {"nc_area_1": "310", "nc_area_2": "312", "nc_area_3": "314.2", "nc_area_4": "312.2"})
        test.refresh_from_db()
        self.assertEqual(test.nc_calibration, cal)
        self.assertAlmostEqual(test.results.get().value, 1.4502606, places=6)  # workbook: 0.0145026 (fraction)
        self.assertContains(self.client.get(sample.get_absolute_url()), "NC calibration")


class InstrumentTests(LabTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.gc2 = Instrument.objects.create(name="GC2", order=1)

    def calibrate(self, instrument, r32_area=10.0):
        cal, cc32, cc125 = self.make_calibration(verify_area_r32=r32_area)
        cal.instrument = instrument
        cal.save()
        return cal, cc32, cc125

    def test_calibrations_are_per_instrument(self):
        cal1, _, _ = self.calibrate(self.gc)
        self.assertEqual(self.blend.active_calibration(instrument=self.gc), cal1)
        self.assertIsNone(self.blend.active_calibration(instrument=self.gc2))
        cal2, _, _ = self.calibrate(self.gc2)
        self.assertEqual(self.blend.active_calibration(instrument=self.gc2), cal2)
        page = self.client.get(reverse("lab:calibration_list") + f"?gc={self.gc2.pk}")
        self.assertEqual(page.context["instrument"], self.gc2)
        self.assertEqual(list(page.context["calibrations"]), [cal2])
        self.assertEqual([i.name for i in page.context["instruments"]], ["GC7890", "GC2"])
        # Retired instruments drop out of the tabs and their calibrations can't be used.
        self.gc2.active = False
        self.gc2.save()
        self.assertIsNone(self.blend.active_calibration(instrument=self.gc2))
        self.assertNotIn(self.gc2, self.client.get(reverse("lab:calibration_list")).context["instruments"])

    def test_sample_picks_the_instrument(self):
        cal1, cc32_1, cc125_1 = self.calibrate(self.gc)
        cal2, cc32_2, cc125_2 = self.calibrate(self.gc2)
        sample = self.make_sample()
        test = self.add_test(sample, "Blend Analysis")
        page = self.client.get(sample.get_absolute_url())
        self.assertEqual(page.context["calibration"], cal1)  # first usable one
        page = self.client.get(sample.get_absolute_url() + f"?gc={self.gc2.pk}")
        self.assertEqual(page.context["calibration"], cal2)
        self.assertContains(page, "GC2 – calibrated")
        services.save_results(test, self.user, {"calibration_id": cal2.pk, f"area_{cc32_2.pk}": "10", f"area_{cc125_2.pk}": "10"})
        test.refresh_from_db()
        self.assertEqual(test.calibration, cal2)
        # A calibration that's no longer usable is refused.
        cal2.verifications.all().delete()
        with self.assertRaisesMessage(services.ResultError, "GC2 calibration can't be used"):
            services.compute_results(test, {"calibration_id": cal2.pk, f"area_{cc32_2.pk}": "10", f"area_{cc125_2.pk}": "10"})

    def test_only_admins_manage_instruments(self):
        url = reverse("lab:instrument_list")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotContains(self.client.get(reverse("lab:calibration_list")), "Manage instruments")
        self.user.is_staff = True
        self.user.save()
        self.assertContains(self.client.get(reverse("lab:calibration_list")), "Manage instruments")
        self.client.post(url, {"action": "add", "new-name": "GC8890", "new-description": "Bay 2", "new-order": "2"})
        gc3 = Instrument.objects.get(name="GC8890")
        self.client.post(
            reverse("lab:instrument_edit", args=[gc3.pk]),
            {"action": "save", f"i{gc3.pk}-name": "GC8890A", f"i{gc3.pk}-description": "", f"i{gc3.pk}-order": "2"},
        )
        gc3.refresh_from_db()
        self.assertEqual(gc3.name, "GC8890A")
        self.assertFalse(gc3.active)  # Active left unticked: retired
        self.client.post(reverse("lab:instrument_edit", args=[gc3.pk]), {"action": "delete"})
        self.assertFalse(Instrument.objects.filter(pk=gc3.pk).exists())
        # An instrument with calibrations can only be retired.
        self.calibrate(self.gc)
        self.client.post(reverse("lab:instrument_edit", args=[self.gc.pk]), {"action": "delete"})
        self.assertTrue(Instrument.objects.filter(pk=self.gc.pk).exists())
