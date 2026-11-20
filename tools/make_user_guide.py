#!/usr/bin/env python3
"""Write docs/M2_Migration_User_Guide.html: the guide for the people who use the tool, with its screenshots built in.

    python3 tools/make_user_guide.py <folder of screenshots>

The screenshots come from a copy of the tool run on the fictitious sample data (bin/m2ctl.sh sample) with
client data hidden (?privacy=1): never from a run of real M2 exports. One PNG per screen, named after the tab:
overview, decide, ai, flow, journal, records_M-0001, files, review, values, finance, settings, docs. They are
reduced to JPEG and put into the page as data URIs, so the one file works in the Docs tab, a browser and the PDF
(tools/make_docs_pdf.sh docs/M2_Migration_User_Guide.html).
"""
import base64
import io
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / 'docs' / 'M2_Migration_User_Guide.html'

STYLE = """body{font:15px/1.6 system-ui,'Segoe UI',Roboto,sans-serif;color:#1f2328;max-width:920px;margin:32px auto;padding:0 16px}
h1{color:#3a1846;font-size:26px;border-bottom:2px solid #3a1846;padding-bottom:6px}h2{font-size:20px;margin-top:34px;color:#4f2460}
h3{font-size:16px;margin:20px 0 6px}table{border-collapse:collapse;width:100%;margin:10px 0}
th,td{border:1px solid #d8dee5;padding:6px 9px;text-align:left;vertical-align:top}th{background:#eef1f4}
.note{border-left:3px solid #3a1846;background:#f4f1fa;padding:9px 13px}.tip{border-left:3px solid #1a7f37;background:#eef8f0;padding:9px 13px}
.by{margin-top:32px;padding-top:8px;border-top:1px solid #d8dee5;color:#59636e;font-size:12.5px}
@page{size:A4;margin:14mm}@media print{body{margin:0;max-width:none;padding:0}figure{break-inside:avoid}h2,h3{break-after:avoid}}"""


def shot(folder, name, caption):
    """One screenshot as a figure with the picture inside the page (JPEG, at most 1400 px wide)."""
    path = Path(folder) / f'{name}.png'
    if not path.is_file():
        return f'<p class="note">Screenshot {name} is missing.</p>'
    image = Image.open(path).convert('RGB')
    if image.width > 1400:
        image = image.resize((1400, round(image.height * 1400 / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=72, optimize=True)
    data = base64.b64encode(buffer.getvalue()).decode()
    # inline styles: the Docs tab shows the guide inside the tool's page, without the guide's own <style>
    return (f'<figure style="margin:14px 0 22px"><img alt="{caption}" src="data:image/jpeg;base64,{data}" '
            f'style="width:100%;height:auto;border:1px solid #d8dee5;border-radius:8px">'
            f'<figcaption style="font-size:13px;color:#59636e;margin-top:6px">{caption}</figcaption></figure>')


def guide(folder):
    s = lambda name, caption: shot(folder, name, caption)  # noqa: E731
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>M2 Migration – User Guide</title>
<style>{STYLE}</style></head><body><h1>M2 Migration – User Guide</h1>

<p>This guide is for the people who move the clients of <b>M2 Mandat</b> into <b>Odoo</b> with the M2 → CSP Migration tool.
No technical knowledge is needed. Each step says what you see, what to do, and what comes next.</p>
<div class="note"><b>The pictures in this guide show no client data.</b> They were taken from the tool's made-up sample data,
with <i>Hide client data</i> switched on: names, values and messages appear as grey bars. Your screens show your real data.</div>

<h2>1. What the tool does, in one minute</h2>
<ul>
<li><b>It reads what M2 exports.</b> You export the client lists from M2 (mandates, addresses, officers, owners, bank details…)
into one folder. The tool reads them as they are, German or English.</li>
<li><b>It prepares the files for Odoo.</b> It writes <b>CSP_Import_M2.xlsx</b>, ready for Odoo's Client Import, and
<b>CSP_Import_M2_Review.xlsx</b> with everything it left out and why.</li>
<li><b>You fill in what M2 does not know.</b> For example which of our companies looks after a client, or which Odoo
employee an M2 staff code stands for. The tool asks; it never guesses a company.</li>
<li><b>A local AI helps.</b> The AI assistant runs on this computer (Ollama). It proposes answers and sorts the documents;
you accept or not. Client data never goes to the cloud.</li>
<li><b>Nothing is written to Odoo by the tool.</b> You load the prepared file in Odoo yourself, after checking it.</li>
<li><b>Every client keeps its own trail.</b> For each M2 client the tool records every step it took: what it read,
what it changed, why it loaded the client or left it out.</li>
</ul>

<h2>2. The path from M2 to Odoo</h2>
<table><tr><th style="width:5%">#</th><th style="width:28%">Step</th><th>Where in the tool</th></tr>
<tr><td>1</td><td>Export the lists from M2</td><td>In M2 (your M2 contact does this), into one folder</td></tr>
<tr><td>2</td><td>Run the migration</td><td>Top bar: <b>Browse…</b> to the folder, then <b>Run migration</b></td></tr>
<tr><td>3</td><td>Read the result</td><td><b>Overview</b>: the summary and the <b>Next steps</b> list</td></tr>
<tr><td>4</td><td>Fill in the missing details</td><td><b>Needs decision</b>, then <b>Run again</b></td></tr>
<tr><td>5</td><td>Let the AI help</td><td><b>AI assistant</b>: suggestions, document types, the sorting list</td></tr>
<tr><td>6</td><td>Check clients one by one</td><td><b>Records</b> (each client's trail) and <b>Review rows</b></td></tr>
<tr><td>7</td><td>Load into Odoo</td><td>Odoo: Contacts › Configuration › Client Import (Check, then Import)</td></tr></table>
<p>Steps 4 to 6 can be repeated as often as needed: a run takes seconds and always gives the same result for the same
exports and answers.</p>

<h2>3. Export from M2 and run</h2>
<p>Put every list exported from M2 in <b>one folder</b>: Excel or CSV, as M2 gives them. Keep the column titles. The tool
works out by itself which list is which (mandates, addresses, officers…), and says so in <i>Files &amp; columns</i>.</p>
<ol><li>In the top bar press <b>Browse…</b> and choose the folder.</li>
<li>Press <b>Run migration</b>. The run takes a few seconds; the bar under the top bar shows its stages.</li>
<li>When it says <b>done</b>, the Overview shows the result.</li></ol>
<div class="tip"><b>Sample data</b> (top bar) loads made-up exports to try the tool safely.</div>

<h2>4. Overview: the result and what to do next</h2>
<p>The Overview answers three questions: how many clients are ready, how many wait for you, and what to do next.</p>
{s('overview', 'Overview: the result of the run, the Next steps list, and the counts. The coloured boxes open the matching details.')}
<ul><li><b>The summary line</b>, e.g. <i>23 row(s) ready, 8 wait in review</i>, with a button to the next place to look.</li>
<li><b>Next steps</b> is your checklist. Each line says what is still needed and has a button that goes there.
A tick means done. Lines marked <i>(optional)</i> add data but do not block the import.</li>
<li><b>The boxes</b> count what happened: rows to import, rows to review, errors, warnings, values changed, values
not loaded. Click one to see those items.</li></ul>

<h2>5. Needs decision: fill in the missing details</h2>
<p>M2 does not know everything Odoo needs. This page lists each question once, with the clients it concerns.
Answer, press <b>Save</b>, and when you are done press <b>Run again</b>. Your answers are kept and used on every later run.</p>
{s('decide', 'Needs decision: each section is one kind of question. "blocks rows" means those clients wait until it is answered.')}
<table><tr><th style="width:30%">Section</th><th>What you answer</th></tr>
<tr><td>Operating company of a mandate holder</td><td>Which of our companies looks after the clients of an M2 mandate holder.
The tool never picks a default company: until you answer, those clients wait.</td></tr>
<tr><td>Employees</td><td>Which Odoo employee an M2 staff code (e.g. <i>FF</i>) stands for. Until then, Client Advisor and
Deputy stay empty; the clients still load.</td></tr>
<tr><td>Type codes</td><td>Which register an M2 role code fills (directors, owners, shareholders…).</td></tr>
<tr><td>Files not recognised</td><td>What an export file is, when the tool could not tell.</td></tr>
<tr><td>Columns with no CSP field</td><td>Whether an M2 column should go into an Odoo field. Each column shows what it means
(e.g. <i>MWSTPFLICHT = VAT liable</i>), where it is in the file, and a suggested field with <b>Use</b>. Type in the
<b>Load into</b> box to search the Odoo fields by name. Nothing is lost if you skip it: the values stay in the client's Notes.</td></tr>
<tr><td>Clients not found, other checks</td><td>Items to fix in M2 or in the review file, then run again.</td></tr></table>
<div class="tip"><b>The AI's suggestion</b> appears next to a question with a percentage (how sure it is) and <b>Accept</b>.
Accept saves it like your own answer; you can take any answer back with the arrow next to it.</div>

<h2>6. AI assistant: help from a local model</h2>
<p>The AI assistant runs on <b>this computer</b> with Ollama. It reads the finished run and <b>proposes</b>: answers for
Needs decision, the type of each document, where each document goes in the client's Odoo folders, and a short
summary of the run. It never changes anything by itself.</p>
{s('ai', 'AI assistant: the model status, the buttons for each task, the suggestions with Accept, and the document types.')}
<ul><li><b>Run all</b> does everything; or press one task: <b>Suggest decisions</b>, <b>Classify documents</b>, <b>Summary</b>.
A bar shows the progress in percent. <b>Stop</b> stops it; <b>Resume</b> goes on where it stopped.</li>
<li><b>Document types and folders</b>: each file of the M2 archive gets a type (passport, register extract, invoice…)
and the Odoo folder it goes to (Corporate Documents, Bank Statements, Correspondence…).</li>
<li><b>Sorting list</b>: files the rules cannot place are listed in an Excel file, one row per M2 folder or name pattern.
Choose the Odoo folder from the list, save, press <b>Apply my sorting list</b>: the tool remembers it for every later run.</li>
<li><b>Document packages</b>: the files to upload into Odoo's Documents, in zip files, emails excepted.</li></ul>
<div class="note"><b>Privacy.</b> Client names, values, file names and documents stay on this computer. A company setting
can allow only column titles and M2 codes to go to Claude (Anthropic's AI) for harder questions; it is off unless your
company switches it on.</div>

<h2>7. Data flow: the whole picture</h2>
<p>The Data flow shows where the data comes from and where it goes, with the numbers of the current run: the M2 servers
and the archive, the exports, this tool and the local AI, the two files it writes, and Odoo (staging, then production).</p>
{s('flow', 'Data flow: from M2 to Odoo. The outlined lane is what this tool does on this computer; the dashed boxes happen outside it.')}
<p>Below the diagram: what came out of M2 (per export), the documents per mandate holder, and the rows per Odoo sheet.</p>

<h2>8. Records: each client's own trail</h2>
<p>Every M2 client has a trail: every step the tool took for it, in order. Pick a client on the left (or type its M2
number or name): on the right you see what was read, how the client was found, what was changed and why it was loaded or
left out, and the row that goes to Odoo.</p>
{s('records_M-0001', "Records: the list of clients, most serious first, and the decision trail of the selected client.")}
<ul><li>The coloured labels show the level of each step: <i>decision</i>, <i>change</i>, <i>not loaded</i>, <i>review</i>, <i>error</i>.</li>
<li>Click a step for its details. <b>Link</b> copies an address that opens this client's trail directly: handy to show a colleague.</li></ul>

<h2>9. Journal: everything in order</h2>
<p>The Journal lists every step of the run for all clients, as it happened. Use the buttons at the top to see only errors,
reviews or changes, and the search box to find a client, a column or a value.</p>
{s('journal', 'Journal: every event of the run with its level, stage, rule and message. Here with client data hidden.')}

<h2>10. Review rows, changes and files</h2>
<h3>Review rows</h3>
<p>The rows the tool could not load as they are. Each says <b>why</b> and <b>how to fix it</b>: in M2 (then export again) or
directly in the review file, then load that row with the import file.</p>
{s('review', 'Review rows: grouped by Odoo sheet, each with the reason and the fix.')}
<h3>Changes &amp; not loaded</h3>
<p>Every value the tool converted or cleaned on the way (a date, a country, a phone number), and every M2 value that has no
place in Odoo. Nothing disappears silently: what is not loaded is listed here and kept in the client's Notes.</p>
{s('values', 'Changes & not loaded: old value, new value and the reason, per client.')}
<h3>Files &amp; columns</h3>
<p>How each export was read: which kind of list, which column went to which field, and which columns were not used.</p>
{s('files', 'Files & columns: each export file and its columns.')}
<h3>Finance</h3>
<p>Fees and open invoices from M2, per company and currency, to reconcile with Odoo after the import.</p>
{s('finance', 'Finance: fees and receivables from M2.')}

<h2>11. Load into Odoo</h2>
<ol><li>On the Overview, download <b>CSP_Import_M2.xlsx</b> (or press <i>Import file</i> in Next steps).</li>
<li>In Odoo open <b>Contacts › Configuration › Client Import</b>, upload the file and press <b>Check</b>. Check writes nothing:
it lists what would be created or updated, and any problem.</li>
<li>When the check is clean, press <b>Import</b>. An import can be undone from the same screen.</li>
<li>Then the documents: <b>Documents › Configuration › M2 Document Packages</b>, one entry per zip file, <b>Import</b>.
Each file goes into its client's folder; the document type is set by a person when the file is checked.</li></ol>
<div class="note">The companies must exist in Odoo with their <b>CSP Short Code</b> (the M2 holder code, on the company's CSP tab):
the import finds the operating company by it. Load in the test database first, then in production.</div>

<h2>12. Settings and Hide client data</h2>
{s('settings', 'Settings: the company map, the employee map and the other answers, all in one place.')}
<p>Everything you answered in Needs decision is kept here and can be changed. The <b>eye</b> button at the bottom of the
side bar switches <b>Hide client data</b> on and off: names, values and messages turn into grey bars, while titles,
buttons and counts stay. Use it when you share your screen, take screenshots or train colleagues.</p>

<h2>13. Help and words used</h2>
<p>The <b>Docs</b> tab has this guide and the detailed ones (rules, AI assistant, archive inventory). Each can be printed as PDF.</p>
{s('docs', 'Docs: the guides, inside the tool.')}
<table><tr><th style="width:26%">Word</th><th>Meaning</th></tr>
<tr><td>M2 key / M2 number</td><td>The client's number in M2, e.g. <i>KP-00031-ENT</i>. It identifies the client in Odoo too.</td></tr>
<tr><td>Mandate holder</td><td>The M2 database / office a mandate belongs to (e.g. ARVIS-PH). Each is one of our operating companies.</td></tr>
<tr><td>Operating company</td><td>Our company in Odoo that looks after the client.</td></tr>
<tr><td>CSP Short Code</td><td>The company's short code in Odoo (the M2 holder code), used to find it on import.</td></tr>
<tr><td>Run</td><td>One pass of the tool over the exports. Each run is kept with its files and journal.</td></tr>
<tr><td>Decision</td><td>An answer the tool needs from you, or a choice the tool made by its rules (both are in the trail).</td></tr>
<tr><td>Review row</td><td>A row that could not be loaded as it is, with the reason and the fix.</td></tr>
<tr><td>Not loaded</td><td>An M2 value with no place in Odoo: listed in the review file and kept in the client's Notes.</td></tr>
<tr><td>Ollama</td><td>The program that runs the AI model on this computer.</td></tr></table>

<p class="by">M2 → CSP Migration · User Guide. Screenshots: made-up sample data, client data hidden.</p>
</body></html>
"""


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    OUT.write_text(guide(argv[0]), encoding='utf-8')
    print(f"Written: {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == '__main__':
    main()
