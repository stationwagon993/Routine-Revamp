from django.urls import path

from . import views

app_name = "lab"

urlpatterns = [
    path("", views.sample_log, name="sample_log"),
    path("samples/new/", views.sample_create, name="sample_create"),
    path("samples/<int:pk>/", views.sample_detail, name="sample_detail"),
    path("samples/<int:pk>/edit/", views.sample_edit, name="sample_edit"),
    path("samples/<int:pk>/add-test/", views.sample_add_test, name="sample_add_test"),
    path("samples/<int:pk>/report/", views.report_create, name="report_create"),
    path("tests/<int:pk>/results/", views.test_results, name="test_results"),
    path("tests/<int:pk>/calculate/", views.test_calculate, name="test_calculate"),
    path("tests/<int:pk>/status/", views.test_status, name="test_status"),
    path("reports/", views.report_list, name="report_list"),
    path("reports/<int:pk>/", views.report_detail, name="report_detail"),
    path("reports/<int:pk>/generate/", views.report_generate, name="report_generate"),
    path("reports/<int:pk>/pdf/", views.report_pdf, name="report_pdf"),
    path("reports/<int:pk>/email/", views.report_email, name="report_email"),
    path("reports/<int:pk>/sent/", views.report_mark_sent, name="report_mark_sent"),
    path("calibrations/", views.calibration_list, name="calibration_list"),
    path("calibrations/new/", views.calibration_create, name="calibration_create"),
    path("calibrations/<int:pk>/", views.calibration_detail, name="calibration_detail"),
    path("calibrations/<int:pk>/verify/", views.verification_create, name="verification_create"),
    path("formulas/", views.formula_list, name="formula_list"),
    path("formulas/<int:method_pk>/new/", views.formula_new_version, name="formula_new_version"),
]
