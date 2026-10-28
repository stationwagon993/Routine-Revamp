"""
Pre-filled Outlook email drafts for reports.

On Windows with desktop Outlook installed (and pywin32), the draft opens
directly in an Outlook window for the user to check and send. Otherwise an
unsent .eml file is returned; opening it in Outlook gives the same draft.
"""

from email.message import EmailMessage
from pathlib import Path

from django.conf import settings


def _addresses(raw):
    return [a.strip() for a in raw.replace(",", ";").split(";") if a.strip()]


def compose(report):
    numbers = ", ".join(s.sample_number for s in report.ordered_samples())
    subject = f"Analysis report {report.report_number} – Sample {numbers}"
    body = (
        "Hello,\n\n"
        f"Please find attached the analysis report for sample(s) {numbers}.\n\n"
        "Let us know if you have any questions."
    )
    if settings.EMAIL_SIGNATURE:
        body += "\n\n" + settings.EMAIL_SIGNATURE
    return {
        "to": _addresses(report.recipient_email),
        "cc": _addresses(report.cc_emails),
        "subject": subject,
        "body": body,
        "attachment": Path(report.pdf_path),
    }


def open_in_outlook(report):
    """Open a draft in desktop Outlook. Returns False if Outlook is not available."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False

    draft = compose(report)
    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.To = "; ".join(draft["to"])
        mail.CC = "; ".join(draft["cc"])
        mail.Subject = draft["subject"]
        mail.Body = draft["body"]
        mail.Attachments.Add(str(draft["attachment"].resolve()))
        mail.Display(False)
    except Exception:  # Outlook not installed or refused the automation call.
        return False
    finally:
        pythoncom.CoUninitialize()
    return True


def eml_bytes(report):
    """An unsent email (.eml) with the report PDF attached."""
    draft = compose(report)
    message = EmailMessage()
    message["To"] = ", ".join(draft["to"])
    if draft["cc"]:
        message["Cc"] = ", ".join(draft["cc"])
    message["Subject"] = draft["subject"]
    # Tells Outlook to open the file as an editable draft with a Send button.
    message["X-Unsent"] = "1"
    message.set_content(draft["body"])
    message.add_attachment(
        draft["attachment"].read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=draft["attachment"].name,
    )
    return message.as_bytes()
