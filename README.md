# M2 Mandat → CSP Odoo migration

Turns the exports of M2 Mandat into the **CSP client import workbook** (`csp_client_import`, Odoo 20), and records
every decision it takes on the way so each error can be traced to its source row and its rule.

Nothing is written to Odoo. The run produces files you load with *Contacts › Configuration › Client Import*.

## Quick start

```bash
python3 -m venv --without-pip .venv && .venv/bin/python get-pip.py && .venv/bin/pip install openpyxl xlrd
.venv/bin/python m2_ui.py                 # UI on http://127.0.0.1:8796 (next free port if taken)
.venv/bin/python migration_script_m2_odoo.py <exports folder> [<output folder>]   # same run, terminal only
.venv/bin/python make_sample_m2.py        # fictitious M2 exports in ./sample_m2 to try it
.venv/bin/python make_sample_m2.py --english   # same, with the English M2 grid titles, in ./sample_m2_en
.venv/bin/python -m unittest discover -s tests -v
```

The template columns are read from `/data/odoo20_enhancements/csp_client_import/models/import_spec.py`
(override with `CSP_IMPORT_SPEC`), so the output always matches the installed module.

## What it reads

Every `.xlsx`, `.xls` and `.csv` in the folder: addresses, mandates, officers (board / secretary / agent in one list,
routed by their function), directors, beneficial owners, shareholders, secretaries, agents, bank accounts,
signatories, contact persons, fees (ABF / FLF / LEI), open items (invoices), compliance data and the document index.
German headers, or the English grid titles of M2 Professional (*Infosystem › Mandate info*: Mandate overview,
Address, Detail addr., Bank / Account / Authorized to sign, Fees, Invoice, Compliance, Document). Report titles above
the header, total lines, Windows-1252 CSVs with `;` are handled. A file is classified by its headers and file name; the
score of every candidate is kept.

An export of one mandate's grid has no mandate column: name the file after the mandate
(`AP-482-ACC Detail addr.xlsx`) or keep the report title with the number, and every row is linked to it. On those
grids `Adrno.` / `Address no.` is the person's or the bank's address, never the client's.

Sub-folders named after a mandate number (M2's document store, e.g. `AP-00482-ACC/LEI/…`) are indexed as the
client's documents.

## What it writes (per run, in `runs/<stamp>/` from the UI)

| File | Content |
|---|---|
| `CSP_Import_M2.xlsx` | Template sheets; every row is expected to load without errors |
| `CSP_Import_M2_Review.xlsx` | Rows left out (reason + fix), Changes, Not Loaded, Closed Mandates, Fees, Compliance, Documents, Receivables per company, Files, Decisions |
| `journal.jsonl` | One JSON event per decision: level, rule, stage, file, row, M2 key, old/new value, alternatives, fix |
| `summary.json`, `run.log` | Totals, per-record trail index, and the readable log |

## The UI

* **Overview** – counts, result per sheet, downloads, the rules that fired.
* **Needs decision** – what blocks rows: mandate holder → operating company (*never a default company*), M2
  employee abbreviations, M2 Detail addr. type codes → register sheet, unrecognised files, unmapped columns,
  unresolved clients. Saving writes `m2_config.json`; *Run again* applies it.
* **Journal** – the live event stream with filters by level, stage, rule, file and text; click an event for all
  its details and the fix.
* **Records** – the full decision trail of one M2 mandate across all files, plus its final import row.
* **Files & columns** (every column: read as which field, or not loaded with sample values), **Review rows**,
  **Changes & not loaded**, **Finance** (receivables, fees, compliance, documents), **Settings**.
* **Sample data** / **Sample (English M2)** write fictitious exports to try a run.

## Key rules

* A mandate whose holder is not in the company map goes to review (plan §18: never a default company).
* Closed mandates (status closed, or end date passed) are not loaded unless switched on; their register rows follow.
* M2 `Surname, Given name` becomes `Given Surname`; persons split in name columns are joined.
* Register rows link to their client by mandate no → debtor no → address no → exact unique name; close names are
  suggested, never guessed. M2 numbers match without their zero padding (`AP-00482-ACC` = `AP-482-ACC`).
* The mandate's address is found by address no, by debtor no, or by the debtor no as the address *Number* (in M2 the
  debtor is an address).
* `Active = N` closes a mandate like a closed status. `Postcode, City` printed together (`MH 96960 Majuro`) is
  split into country code, postcode and town. `Founded` and `Business activity` come from the address when the
  mandate has none.
* Detail addr. rows are routed by their `Type` code through *Settings › officer types* (e.g. `DIR` → directors,
  `BO` → ubos, `CP` → contacts), else by the wording of the function; unknown codes go to review. `collective` J/N
  becomes joint / sole signing; Remark 2 and 3 are added to the Remark.
* A Banks-grid row with a bank but no account number is the bank relationship: its bank name and clearing number
  (BCN) go to the client's accounts when the client has one bank. An account without a currency column takes the ISO
  code written in its type or remark (`Current account USD`).
* Several rows of one person at one client are merged (the importer matches on client + name); other periods go
  into the Remark.
* M2 values with no CSP column are kept in the Not Loaded sheet and in the client's Notes.
* Fees and open items are not template data: they are listed for finance and give the receivables per company to
  reconcile after the opening items are posted. The LEI fee's due date fills the client's *LEI Renewal Date*; a fee
  with a past finish date is listed as ended. Paid invoices (nothing open) are left out of the receivables; the open
  amount in the invoice currency is used.
* Compliance data (ML-related, risk category, country of max. risk, lock, note) has no template column: it goes into
  the client's Notes and the Compliance sheet unchanged; a lock is flagged. The risk rating is decided in Odoo.

Documentation: `docs/` (HTML and PDF).

## Portwatch (deploy and monitoring)

The UI is registered in `/data/portwatch/ports.toml` as **M2 → CSP Migration** (port 8796, group Tools):

* health check on `/health` (answers without the token: whether a run goes and how the last one ended)
* log: `logs/m2_ui.log`, one line per UI start, run start, run result (rows, errors, warnings) and failed request
* Start / Stop / Restart through `bin/m2ctl.sh`
* actions: **Deploy** (unit tests, then restart on the new code, then health check; stops at the first failure),
  Run tests, Status, Last run summary, Migrate last folder, Sample run

`bin/m2ctl.sh start|stop|restart|status|test|deploy|sample|migrate [dir]|last` does the same by hand.
