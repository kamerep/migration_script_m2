# M2 Mandat → CSP Odoo migration

Turns the exports of M2 Mandat into the **CSP client import workbook** (`csp_client_import`, Odoo 20), and records
every decision it takes on the way so each error can be traced to its source row and its rule.

Nothing is written to Odoo. The run produces files you load with *Contacts › Configuration › Client Import*.

## Quick start

```bash
python3 -m venv --without-pip .venv && .venv/bin/python get-pip.py && .venv/bin/pip install openpyxl xlrd anthropic mcp
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
signatories, powers of attorney (Vollmachten), contact persons, fees (ABF / FLF / LEI), open items (invoices),
compliance data and the document index.
German headers, or the English grid titles of M2 Professional (*Infosystem › Mandate info*: Mandate overview,
Address, Detail addr., Bank / Account / Authorized to sign, Fees, Invoice, Compliance, Document). Report titles above
the header, total lines, Windows-1252 CSVs with `;` are handled. A file is classified by its headers and file name; the
score of every candidate is kept.

Powers of attorney go to the **Powers of Attorney** sheet (Odoo module `csp_power_attorney`). A Vollmachten export
gives attorney, kind, scope, signing, validity, revocation, notarised / apostille and the M2 number; the status comes
from the dates. A file of the M2 documents named Vollmacht or attorney gives a Draft entry for a client that has none
from the export, to complete in Odoo. A bare "POA" in a file name is Proof of Address, not a power of attorney.

An export of one mandate's grid has no mandate column: name the file after the mandate
(`KP-482-ACC Detail addr.xlsx`) or keep the report title with the number, and every row is linked to it. On those
grids `Adrno.` / `Address no.` is the person's or the bank's address, never the client's.

Sub-folders named after a mandate number (M2's document store, e.g. `KP-00482-ACC/LEI/…`) are indexed as the
client's documents.

## What it writes (per run, in `runs/<stamp>/` from the UI)

| File | Content |
|---|---|
| `CSP_Import_M2.xlsx` | Template sheets; every row is expected to load without errors |
| `CSP_Import_M2_Review.xlsx` | Rows left out (reason + fix), Changes, Not Loaded, Closed Mandates, Fees, Compliance, Documents, Receivables per company, Files, Decisions |
| `journal.jsonl` | One JSON event per decision: level, rule, stage, file, row, M2 key, old/new value, alternatives, fix |
| `summary.json`, `run.log` | Totals, per-record trail index, and the readable log |

## The UI

One page (`m2_ui.html`), no external fonts or scripts, light and dark, in five colour palettes (Settings › This browser,
or the palette button at the foot of the side bar; kept per browser). Level colours stay the same in every palette. A side bar groups the sections:

* **Run**: **Overview** (status and next step, counts, result per sheet, downloads, the rules that fired),
  **Needs decision** (what blocks rows: mandate holder → operating company, *never a default company*; M2 employee
  abbreviations; M2 Detail addr. type codes → register sheet; unrecognised files; unmapped columns; unresolved
  clients. Each section is marked *blocks rows* or *optional*; a saved answer gets a tick and an **Undo**, the bar
  counts them, *Run again* applies them), **AI assistant**, **Data flow** (the M2 DataFlow chart from the M2
  servers to Odoo production, with the open run's numbers: export rows by kind, archive inventory, what this tool
  produced and what Ollama did on this machine; charts of the files read, documents per mandate holder, rows per
  sheet and the local model's calls).
* **Inspect**: **Journal** (the live event stream with filters by level, stage, rule, file and text; click an event
  for its details, the fix and the JSON), **Records** (the full decision trail of one M2 mandate across all files,
  plus its final import row), **Files & columns** (every column: read as which field, or not loaded with sample
  values), **Review rows**, **Changes & not loaded**, **Finance** (receivables, fees, compliance, documents).
* **Configure**: **Settings**, **Docs** (the guides of `docs/` with search, and reference pages generated from the
  code: HTTP API, MCP tools, command line, keyboard and links).

Feedback: a stage strip with a progress line while a run goes (percentage in the browser tab), messages at the
bottom right (saved, failed, run finished, with Undo or the next step as a button), a spinner on the button that is
working, placeholders while a section loads, and a banner with automatic reconnection when the tool stops answering.

Keys: `Ctrl+K` search and commands (sections, mandates, rules, guides), `Ctrl+Enter` run, `/` search of the section,
`[` `]` and `1`–`0` change section, `?` the list. Every place has an address: `#decide`, `#records/KP-482-ACC`,
`#journal/COMPANY_UNKNOWN`, `#docs/ref-api`, and `?run=<stamp>` for an earlier run.

Accessibility and phones: every text keeps at least 4.5:1 contrast in every palette, light and dark; below 900 px
the side bar becomes a top bar whose section button lists every section; 44 px targets on touch screens; a skip
link, one page heading per section, labelled fields and keyboard-reachable tables; reduced motion, Windows high
contrast and "more contrast" are honoured. The page runs under a strict Content-Security-Policy (its one script is
allowed by hash, nothing may frame it). Unsaved changes to the settings JSON are never dropped silently.
`bin/m2ctl.sh audit [run]` re-checks all of it against the running page (Interface Craft playbook audit: layout
at 320–1440 px, structure, motion, axe-core when present, and contrast in all ten palette and theme combinations).

Logo and icons are in `assets/` (`icon.svg`, `favicon.svg`, `logo.svg`, `logo-dark.svg` are the sources;
`python3 tools/make_icons.py` rebuilds the PNG / ICO / web manifest). `tools/make_docs_pdf.sh` rebuilds the PDF of
each guide from its HTML.

## Key rules

* A mandate whose holder is not in the company map goes to review (plan §18: never a default company).
* Closed mandates (status closed, or end date passed) are not loaded unless switched on; their register rows follow.
* M2 `Surname, Given name` becomes `Given Surname`; persons split in name columns are joined.
* Register rows link to their client by mandate no → debtor no → address no → exact unique name; close names are
  suggested, never guessed. M2 numbers match without their zero padding (`KP-00482-ACC` = `KP-482-ACC`).
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


## AI assistant (Ollama local, Claude optional)

The migration itself stays rule-based and repeatable. The assistant (`m2_ai.py`) reads a **finished** run and proposes
decisions; nothing is applied until somebody presses **Accept** (or tells Claude to decide).

| Task | What it does | Where it runs |
|---|---|---|
| Suggest decisions | column → CSP field, unrecognised file → kind, M2 type code → register, holder code → company, legal form → entity type | Ollama; with `allow_cloud`, weak answers on headers/codes go to Claude |
| Classify documents | type of each indexed file (passport, register extract, LEI, …) and clients missing KYC documents | keyword rules, then Ollama only |
| Summary | status (green / amber / red), headline and next actions of a run | Ollama only |

* Both providers are reached with the Anthropic SDK over the Messages API: Ollama (≥ 0.14) serves it on
  `http://localhost:11434` (`llama3.1:8b`, embeddings `nomic-embed-text`), Claude is `claude-opus-5-5`.
* A suggestion's confidence is not the model's word alone: the local model is capped at 85 %, column suggestions
  are checked against the header names already known for the field (embedding similarity) and against the sample
  values (an e-mail field needs an `@`, a currency a 3-letter code, sort orders 1, 2, 3 are not keys, key fields
  are never above 40 %). Suggestions below `min_confidence` are hidden.
* **Privacy:** client names, file names, sample values and documents never leave the machine. Only column headers
  and M2 codes may go to Claude, and only when the company setting `allow_cloud` is on and `ANTHROPIC_API_KEY` is
  set (in `.env`, read by `bin/m2ctl.sh`; never committed).
* UI: tab **AI assistant** (models, Run all / Suggest / Classify / Summary, suggestions with Accept, document types,
  every model call), AI hints with Accept in **Needs decision**, AI summary on **Overview**, AI settings.
* While the assistant works, a bar shows its progress from 0 to 100%, batch by batch, with the step it is on and
  the time left; **Stop** ends it after the current batch (`POST /api/ai/stop`). Classify documents asks once per
  name pattern, most common first, up to `doc_model_limit` names per run (default 1,000).
* Answers are saved as they come (`runs/ai_cache.jsonl`): a stopped, failed or interrupted job shows **Resume**
  (`POST /api/ai/run` with `resume`), which runs only the open tasks and reuses saved answers. The model's memory
  is checked before it loads and freed when the job ends. The page gets the first 300 documents plus counts.
* M2 table exports (database column names `MT`, `MNR`, `MART`, `MADFIRMA`, `MANDBEZ1`…) are read: the mandate
  number is built as prefix-number-type (KP-00031-ENT), event `REF_COMPOSED`.
* Terminal: `bin/m2ctl.sh ai status|suggest|documents|summary|all [runs/<stamp>]`. Output: `runs/<stamp>/ai_suggestions.json`.

## Claude Code (MCP server)

`m2_mcp.py` gives Claude Code the tool through the running UI (`.mcp.json` registers it for this folder;
elsewhere: `claude mcp add m2-migration -- /data/migration_script/bin/m2ctl.sh mcp`):

`m2_status`, `m2_runs`, `m2_run`, `m2_summary`, `m2_open_decisions` (with the AI suggestion of each),
`m2_journal`, `m2_record`, `m2_ai_assist`, `m2_decide` (only after the user's yes), and `ollama_ask`, which hands
work over client data to the local model instead of reading it in the cloud.

## Archive inventory (M2 document archive without copying it)

When the M2 database cannot be shared, the document archive (`Z:\NOVA-PH\KP-00031-ENT\…`, one folder per mandate)
still tells which client has which documents. On the Windows side:

```powershell
powershell -ExecutionPolicy Bypass -File tools\m2_archive_inventory.ps1 -Root 'Z:\NOVA-PH'
```

writes `M2 Archive inventory.csv` (path, size, date only: no file is opened or copied). Put it next to the exports:
it is read as **Archive inventory**, every file is indexed under the mandate whose number names its folder (zero
padding ignored), the folder above it is kept as the holder's archive. Rules: `ARCHIVE_FOLDER`,
`ARCHIVE_FOLDER_UNKNOWN` (folder not a mandate of the exports), `ARCHIVE_NOT_MANDATE` (files outside mandate
folders), `ARCHIVE_NO_FOLDER` (mandates with no archive folder). The AI assistant then types the documents and lists
the clients missing KYC documents.

While the Windows script writes the inventory, **Overview › Archive inventory** shows a progress bar of the mandate holders
done, being listed and waiting (from `archive_holders.txt` beside the CSV), and *Run migration* asks before running on an
unfinished listing.

## n8n workflows

`n8n/m2-n8n.sh install|start|stop|status|import|run-report` runs n8n locally (127.0.0.1, port 5678 or the next free
one, data in `~/.local/share/n8n-data`). Workflows in `n8n/workflows/`:

* **M2 migration – run and local AI report**: manual, weekdays 07:00 or webhook `POST /webhook/m2-run`
  (`{"source": "/path"}` optional) → run → wait → local AI (suggest, documents, summary) → `runs/<run>/n8n_report.md`
  and the JSON answer. Attach a mail or chat node after *Format report* to send it.
* **M2 documents – classify by name with Ollama**: webhook `POST /webhook/m2-classify` `{"files": [...]}` → keyword
  rules, then Ollama → `{documents: [{file, type, confidence, by}]}`.

n8n calls the UI with the stable API token (`bin/m2ctl.sh token`, stored in `.m2_api_token`, 0600, not in git) and
may write files only under `runs/`.

Documentation: `docs/` (HTML and PDF), also shown in the **Docs** tab of the UI. For agents and developers: `AGENTS.md`.

## Portwatch (deploy and monitoring)

The UI is registered in `/data/portwatch/ports.toml` as **M2 → CSP Migration** (port 8796, group Tools):

* health check on `/health` (answers without the token: whether a run goes and how the last one ended)
* log: `logs/m2_ui.log`, one line per UI start, run start, run result (rows, errors, warnings) and failed request
* Start / Stop / Restart through `bin/m2ctl.sh`
* actions: **Deploy** (unit tests, then restart on the new code, then health check; stops at the first failure),
  Run tests, Status, Last run summary, Migrate last folder, Sample run

`bin/m2ctl.sh start|stop|restart|status|test|deploy|sample|migrate [dir]|last|audit [run]` does the same by hand.
