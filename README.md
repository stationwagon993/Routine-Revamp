# Routine Revamp

Lab workflow app that replaces the Excel sample log, the Master Routine Spreadsheet and the report macros.

```
Intake form ─► Sample log row (status per test)
                   │ click
                   ▼
              Sample page ─► enter raw data, results calculated on the spot
                   │         (blend analysis uses the active, verified calibration)
                   ▼
              Report ─► PDF saved to the network drive
                     ─► pre-filled Outlook draft with the PDF attached
                   │
                   ▼
              User checks and sends ─► "Results Reported" marked complete
```

## What's in it

| Page | What it does |
|---|---|
| **Sample Log** | The spreadsheet grid: one row per sample, one column per test, same colours (yellow *requested*, green *completed* with initials, orange *needs compiling*, red *cancelled*, blue *oldest in process*). Search, filter to in-process samples, click a row to open it. |
| **New Sample** | Intake form. Sample numbers are assigned automatically from the received date (Julian date + sequence, e.g. `26188-2`). *Results Reported* is added to every sample. |
| **Sample page** | One card per test: enter raw data and see the result as you type. Results record who entered them, the formula version, and the calibration used. Status can be changed by hand. |
| **Reports** | A report covers one sample or several (other samples with the same LTO # can be added). Generate PDF → Create Outlook draft → mark as sent. |
| **Calibrations** | Per blend: 3 GC runs → response factors. A calibration can only be used after a verification passes (every component within ±0.03 percentage points). Valid 6 months; expired ones move to the Archive view automatically. The sample log warns 14 days before a calibration expires. |
| **Formulas** | Edit the calculation for any calculated test. Every change is a new version; old results keep the version they used. Formulas are checked so they can only do arithmetic. |
| **Admin** | Users (set each person's initials under their profile), test methods (method codes, column order, add new tests), blends and their components. |

## First-time setup (Windows)

1. Install Python 3.12 or newer from python.org (tick *Add python.exe to PATH*).
2. Copy this folder to the computer.
3. Copy `config.example.ini` to `config.ini` and set:
   - `database_path`: the shared database file on the network drive.
   - `reports_dir`: the folder where report PDFs are saved.
   - `lab_name` and `email_signature`.
4. Double-click `run.bat`. The first run installs everything and opens the app at http://127.0.0.1:8000.
5. Create the first admin account once (open a command prompt in this folder):
   ```
   .venv\Scripts\python manage.py createsuperuser
   ```
   Then add the other users under **Admin → Users**, and give each one their initials.

After that, users just double-click `run.bat`.

### Outlook drafts

With desktop Outlook installed, **Create Outlook draft** opens a new Outlook message with the To/CC, subject, body and PDF already filled in. The user checks it and clicks Send. If Outlook can't be reached, the browser downloads a `.eml` draft that opens the same way in Outlook.

## Shared database: please read

The app runs on each user's computer, and they all share one SQLite database file on the network drive. The app takes a lock for every save and waits for other users, which works for a lab of about 10 people entering data. SQLite on a network share is still less robust than a database server, though:

- Make sure the network drive is backed up.
- If you see "database is locked" errors or the lab grows, move to PostgreSQL on a server, or run the app on one lab PC and let everyone else open it in their browser. Both are configuration changes; no code changes are needed.

## Still to do

- **Real calculations.** TAN, RI, moisture, HBR, OCR and acidity are seeded with *placeholder* formulas, flagged in yellow on screen and marked *DRAFT* on the PDF. Replace them under **Formulas**.
- **Blend analysis calculation.** Currently `% = peak area × response factor`, where the response factor is standard % ÷ mean area of the 3 calibration runs. Confirm this, and whether results should be normalised to 100%.
- **Intake form details.** The form has the basic fields so far.
- **Report layout.** Match the current Excel report template.
- **Method codes.** Fill in the ESIS codes under Admin → Test methods.
- **Inventory integration.** Standard cylinder IDs are free text for now.
- **Excel import.** Bring in historical sample log rows (optional).

## Development

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
.venv/bin/python manage.py migrate
.venv/bin/python manage.py test lab
.venv/bin/python manage.py runserver
```

This repository is **public**. Never commit `config.ini`, database files or report PDFs. `.gitignore` already excludes them.
