#!/usr/bin/env python3
"""Local web UI of the M2 -> CSP migration: run it, watch every decision live, resolve what needs a decision.

    python3 m2_ui.py                    opens http://127.0.0.1:8796 (the next free port when it is taken)
    python3 m2_ui.py --port 9000 --no-browser

Standard library plus the migration's own needs (openpyxl). One HTML page, no external fonts or scripts, so it
runs offline. The server listens on 127.0.0.1 only and every API call must carry the token embedded in the page
it serves. Each run gets a folder in runs/<stamp>/ with the import file, the review file, journal.jsonl,
summary.json and run.log; the page streams the journal while the run goes and reloads any earlier run.

For Portwatch: GET /health answers without the token (200 with the state of the last run), and the server
writes one line per run start, run end and failed request to logs/m2_ui.log.
"""
import argparse
import ast
import base64
import hashlib
import datetime
import errno
import json
import mimetypes
import html
import os
import time
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import m2_ai
import migration_script_m2_odoo as m2

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'm2_ui.html'
RUNS = HERE / 'runs'
CONFIG = HERE / 'm2_config.json'
SETTINGS = HERE / '.m2_ui.json'
SERVER_LOG = HERE / 'logs' / 'm2_ui.log'
ASSETS = HERE / 'assets'            # logo, favicon, app icons (tools/make_icons.py rebuilds the raster ones)
DOCS = HERE / 'docs'                # the guides shown in the Docs tab (HTML) and their PDF copies
VERSION = '1.1'
TOKEN = secrets.token_urlsafe(24)    # the page's token: new at every start
API_TOKEN_FILE = HERE / '.m2_api_token'


def api_token():
    """Stable token for automation (n8n, the MCP server): M2_API_TOKEN, else .m2_api_token (made once, 0600)."""
    if os.environ.get('M2_API_TOKEN'):
        return os.environ['M2_API_TOKEN']
    if not API_TOKEN_FILE.is_file():
        API_TOKEN_FILE.write_text(secrets.token_urlsafe(32) + '\n', encoding='utf-8')
        API_TOKEN_FILE.chmod(0o600)
    return API_TOKEN_FILE.read_text(encoding='utf-8').strip()


API_TOKEN = api_token()
AI_JOBS = {}                    # run id -> {'state': running|done|stopped|failed, 'tasks': [...], 'error': ...}
AI_JOB_FILE = 'ai_job.json'
AI_VIEWS = {}                   # run id -> (mtime of ai_suggestions.json, its content with counts)     # the job's state in the run folder: read back after a restart or a crash
PAGE_DOCS = 300                 # document rows sent to the page; the full list stays in the files
KEEP_RUNS = 3                   # runs held in memory besides the running one (an archive run is ~50 MB)
LOCK = threading.Lock()
JOBS = {}                       # run id -> {'events': [...], 'state': running|done|failed, 'summary': {...}}


class Error(Exception):
    pass


def server_log(level, message):
    """One line in logs/m2_ui.log, the log Portwatch follows."""
    SERVER_LOG.parent.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with LOCK, open(SERVER_LOG, 'a', encoding='utf-8') as fh:
        fh.write(f"{stamp} {level:<7} {message}\n")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def ui_settings():
    return read_json(SETTINGS, {}) or {}


def save_ui_settings(**values):
    data = ui_settings()
    data.update(values)
    SETTINGS.write_text(json.dumps(data, indent=1), encoding='utf-8')


def run_dir(run_id):
    if not run_id or '/' in run_id or '..' in run_id:
        raise Error("Unknown run.")
    path = RUNS / run_id
    if not path.is_dir():
        raise Error(f"Run {run_id} does not exist.")
    return path


# -- runs ----------------------------------------------------------------------------------------------------

def start_run(params):
    """Start a migration run on `source` (or `last_source`: true for the folder last used). One run at a time."""
    source = (params.get('source') or '').strip()
    if not source and params.get('last_source'):     # automation (n8n, MCP): the folder last used in the UI
        source = (ui_settings().get('source') or '').strip()
    if not source:
        raise Error("Choose the folder with the M2 exports.")
    if not Path(source).expanduser().is_dir():
        raise Error(f"Folder {source} does not exist.")
    with LOCK:
        if any(j['state'] == 'running' for j in JOBS.values()):
            raise Error("A run is already going: wait for it to finish.")
        run_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        out = RUNS / run_id
        out.mkdir(parents=True, exist_ok=True)
        job = {'id': run_id, 'events': [], 'state': 'running', 'summary': None, 'source': source,
               'started': datetime.datetime.now().isoformat(timespec='seconds')}
        JOBS[run_id] = job
    save_ui_settings(source=source)
    server_log('INFO', f"run {run_id} started on {source}")
    config = m2.load_config(CONFIG)
    (out / 'config.json').write_text(json.dumps(config, indent=1, ensure_ascii=False), encoding='utf-8')
    log = open(out / 'run.log', 'w', encoding='utf-8')

    def listener(event):
        job['events'].append(event)
        log.write(f"{event['t']:8.3f} {event['level']:<10} {event['code']:<24} {event['message']}\n")
        log.flush()

    def work():
        try:
            migration = m2.Migration(Path(source).expanduser(), out, config, listener=listener,
                                     journal_path=out / 'journal.jsonl')
            job['summary'] = migration.run()
            job['state'] = 'done'
            if m2_ai.load_settings(config).get('after_run'):
                try:
                    api_ai_run({'run': run_id, 'tasks': ['suggest', 'documents', 'summary']})
                except Error as exc:
                    server_log('WARN', f"ai after run {run_id}: {exc}")
            t, lv = job['summary']['totals'], job['summary']['levels']
            server_log('ERROR' if lv.get('error') else 'INFO',
                       f"run {run_id} done: {t['import']} to import, {t['review']} to review, "
                       f"{lv.get('error', 0)} errors, {lv.get('warning', 0)} warnings")
        except Exception:  # noqa: BLE001 - the crash is in the journal (CRASH) and on the page
            job['state'] = 'failed'
            job['trace'] = traceback.format_exc()
            log.write(job['trace'])
            server_log('ERROR', f"run {run_id} failed: {job['trace'].strip().splitlines()[-1]}")
        finally:
            (out / 'state.json').write_text(json.dumps({'state': job['state'], 'source': source,
                                                        'started': job['started']}), encoding='utf-8')
            log.close()

    threading.Thread(target=work, daemon=True).start()
    return {'run': run_id}


def load_run(run_id):
    """A run in memory, or read back from its folder (after a restart of the UI)."""
    if run_id in JOBS:
        return JOBS[run_id]
    path = run_dir(run_id)
    events = []
    journal = path / 'journal.jsonl'
    if journal.is_file():
        for line in journal.read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    state = read_json(path / 'state.json', {}) or {}
    job = {'id': run_id, 'events': events, 'summary': read_json(path / 'summary.json'),
           'state': state.get('state') or ('done' if (path / 'summary.json').is_file() else 'failed'),
           'source': state.get('source'), 'started': state.get('started')}
    JOBS[run_id] = job
    for old in [k for k, j in JOBS.items() if j['state'] != 'running' and k != run_id][:-KEEP_RUNS or None]:
        JOBS.pop(old, None)
    return job


def page_summary(job):
    """The summary as the page needs it: the first document rows and counts, not the whole archive index."""
    sm = job.get('summary')
    if not sm or len(sm.get('documents') or []) <= PAGE_DOCS:
        return sm
    if 'page_summary' not in job:
        docs, holders = sm['documents'], {}
        for d in docs:
            where = d.get('where') or ''
            holder = where[len('archive '):] if where.startswith('archive ') else 'M2 documents'
            holders[holder] = holders.get(holder, 0) + 1
        job['page_summary'] = {**sm, 'documents': docs[:PAGE_DOCS], 'documents_total': len(docs),
                               'documents_by_holder': holders}
    return job['page_summary']


def api_events(params):
    """Journal events of a run from position `since` (at most 5000 a call), its state, and the summary once done."""
    job = load_run(params.get('run'))
    since = int(params.get('since') or 0)
    events = job['events'][since:since + 5000]
    return {'run': job['id'], 'state': job['state'], 'events': events, 'next': since + len(events),
            'total': len(job['events']), 'summary': page_summary(job) if job['state'] != 'running' else None,
            'trace': job.get('trace'), 'source': job.get('source'), 'started': job.get('started')}


def page_csp(page):
    """Strict CSP for the page: its one inline script is allowed by hash (the token is in it, so the hash is taken
    per page), never by 'unsafe-inline'. Inline styles stay allowed; nothing may frame the page."""
    hashes = ' '.join("'sha256-%s'" % base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()
                      for code in re.findall(r'<script>(.*?)</script>', page, re.S))
    return (f"default-src 'self'; script-src {hashes}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def run_brief(path):
    """Totals, levels and source of a run without parsing its summary each time: summary.json of an archive run is
    tens of MB, so brief.json (written once beside it, rebuilt when the summary is newer) is read instead."""
    summary, brief = path / 'summary.json', path / 'brief.json'
    try:
        if brief.stat().st_mtime >= summary.stat().st_mtime:
            return read_json(brief) or {}
    except OSError:
        pass
    data = read_json(summary) or {}
    out = {k: data.get(k) for k in ('totals', 'levels', 'source')} if data else {}
    if out:
        try:
            brief.write_text(json.dumps(out), encoding='utf-8')
        except OSError:
            pass
    return out


def api_runs(_params):
    """The last 60 runs: id, state, source folder, totals and level counts."""
    runs = []
    if RUNS.is_dir():
        for path in sorted(RUNS.iterdir(), reverse=True)[:60]:
            if not path.is_dir():
                continue
            summary = run_brief(path)
            state = read_json(path / 'state.json') or {}
            running = JOBS.get(path.name, {}).get('state') == 'running'
            runs.append({'id': path.name, 'state': 'running' if running else state.get('state') or
                         ('done' if summary else 'failed'),
                         'source': state.get('source') or summary.get('source'),
                         'totals': summary.get('totals'), 'levels': summary.get('levels')})
    return {'runs': runs}


def api_state(_params):
    """Settings (m2_config.json), export kinds and their fields, template sheets, the run going now, version."""
    config = m2.load_config(CONFIG)
    return {'config': config, 'settings': ui_settings(), 'kinds': {k: v['title'] for k, v in m2.KINDS.items()},
            'kind_fields': {k: sorted(v['fields']) for k, v in m2.KINDS.items()},
            'spec': m2.SPEC_PATH, 'sheets': {k: v['title'] for k, v in m2.SHEETS.items()},
            'running': next((j['id'] for j in JOBS.values() if j['state'] == 'running'), None),
            'sample': str(HERE / 'sample_m2'), 'version': VERSION, 'home': str(HERE)}


def api_config(params):
    """Replace the whole settings file with `config` (a JSON object)."""
    config = params.get('config')
    if not isinstance(config, dict):
        raise Error("Settings must be a JSON object.")
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return {'ok': True}


def api_decide(params):
    """One decision (`type` company, employee, file_kind, alias, officer_type, exclude, setting, or unset to take
    one back), written to the settings for the next run."""
    config = m2.load_config(CONFIG)
    kind, key, value = params.get('type'), str(params.get('key') or '').strip(), params.get('value')
    if not key:
        raise Error("Nothing to decide.")
    if kind == 'company':
        config.setdefault('company_map', {})[key] = (value or '').strip()
    elif kind == 'employee':
        config.setdefault('employee_map', {})[key] = (value or '').strip()
    elif kind == 'file_kind':
        if value not in m2.KINDS and value != 'ignore':
            raise Error(f"Unknown kind {value}.")
        config.setdefault('file_kinds', {})[key] = value
    elif kind == 'alias':
        source_kind, field = params.get('kind'), params.get('field')
        if source_kind not in m2.KINDS or not field:
            raise Error("Choose the export kind and the field.")
        aliases = config.setdefault('extra_aliases', {}).setdefault(source_kind, {}).setdefault(field, [])
        if key not in aliases:
            aliases.append(key)
    elif kind == 'officer_type':
        allowed = ('directors', 'ubos', 'shareholders', 'secretaries', 'agents', 'contacts', 'signatories',
                   'powers_attorney', 'ignore')
        if value not in allowed:
            raise Error(f"Choose one of {', '.join(allowed)}.")
        config.setdefault('officer_types', {})[key] = value
    elif kind == 'unset':                      # Undo in the page: take one saved decision back
        target, field = params.get('target'), params.get('field')
        names = {'company': 'company_map', 'employee': 'employee_map', 'file_kind': 'file_kinds',
                 'officer_type': 'officer_types'}
        if target in names:
            config.get(names[target], {}).pop(key, None)
        elif target == 'alias':
            aliases = config.get('extra_aliases', {}).get(params.get('kind'), {})
            if key in aliases.get(field, []):
                aliases[field].remove(key)
            if field in aliases and not aliases[field]:
                del aliases[field]
        else:
            raise Error(f"Nothing to undo for {target}.")
    elif kind == 'exclude':
        if key not in config.setdefault('excluded_keys', []):
            config['excluded_keys'].append(key)
    elif kind == 'setting':
        if key not in ('require_company', 'include_closed_mandates', 'notes_from_unmapped', 'date_order',
                       'addresses_without_mandate', 'ubo_from_mandate', 'tag_m2'):
            raise Error(f"Unknown setting {key}.")
        config[key] = value
    else:
        raise Error(f"Unknown decision {kind}.")
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return {'ok': True, 'config': config}


def api_sample(params):
    """Write the fictitious M2 exports (`english`: the English grid titles) and return their folder."""
    english = bool(params.get('english'))
    target = HERE / ('sample_m2_en' if english else 'sample_m2')
    result = subprocess.run([sys.executable, str(HERE / 'make_sample_m2.py')] + (['--english'] if english else [])
                            + [str(target)], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise Error(result.stderr or result.stdout or "The sample could not be written.")
    return {'source': str(target)}


def api_browse(params):
    """Folders under a path, for the folder picker."""
    path = Path(params.get('path') or ui_settings().get('source') or str(Path.home())).expanduser()
    if path.is_file():
        path = path.parent
    while not path.is_dir() and path != path.parent:
        path = path.parent
    entries = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith('.'):
                continue
            if child.is_dir():
                entries.append({'name': child.name, 'dir': True})
            elif child.suffix.lower() in ('.xlsx', '.xls', '.csv', '.xlsm'):
                entries.append({'name': child.name, 'dir': False})
    except PermissionError:
        pass
    return {'path': str(path), 'parent': str(path.parent), 'entries': entries[:400]}


def api_open(params):
    """Open the folder of a run (or one of its files) with the desktop's file manager."""
    path = run_dir(params.get('run'))
    opener = shutil.which('xdg-open') or shutil.which('open')
    if not opener:
        raise Error("No program to open folders with.")
    target = path / params['file'] if params.get('file') else path
    if params.get('file') and (target.parent != path or not target.is_file()):
        raise Error("Unknown file.")
    subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    return {'ok': True}


# -- AI assistance (m2_ai): suggestions for the open decisions, document types, run summary -------------------
AI_KEYS = {'enabled': bool, 'provider': str, 'ollama_url': str, 'ollama_model': str, 'embed_model': str,
           'claude_model': str, 'claude_effort': str, 'allow_cloud': bool, 'escalate_below': float,
           'min_confidence': float, 'batch': int, 'doc_model_limit': int, 'timeout': int, 'companies': list,
           'after_run': bool}


def api_ai_status(_params):
    """Whether Ollama answers and has the model, whether Claude is allowed and has a key, and the AI settings."""
    config = m2.load_config(CONFIG)
    return {**m2_ai.AI(m2_ai.load_settings(config)).status(), 'settings': m2_ai.load_settings(config)}


def api_ai_settings(params):
    """Change AI settings: `ai` is an object with any of the keys of AI_KEYS."""
    values = params.get('ai')
    if not isinstance(values, dict):
        raise Error("AI settings must be a JSON object.")
    config = m2.load_config(CONFIG)
    ai = config.setdefault('ai', {'_help': "AI assistance (m2_ai.py): provider ollama | auto | claude. allow_cloud "
                                           "lets only headers and codes go to Claude; documents, names and sample "
                                           "values stay on Ollama."})
    for key, kind in AI_KEYS.items():
        if key in values:
            try:
                ai[key] = kind(values[key]) if kind is not list else [str(v) for v in values[key] if str(v).strip()]
            except (TypeError, ValueError) as exc:
                raise Error(f"AI setting {key}: {exc}") from exc
    if ai.get('provider', 'ollama') not in ('ollama', 'auto', 'claude'):
        raise Error("Provider must be ollama, auto or claude.")
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return {'ok': True, 'settings': m2_ai.load_settings(config)}


def api_ai_run(params):
    """Start the assistant on a finished run (background): tasks suggest, documents, summary."""
    run_id = params.get('run')
    path = run_dir(run_id)
    if JOBS.get(run_id, {}).get('state') == 'running':
        raise Error("The run is still going.")
    if AI_JOBS.get(run_id, {}).get('state') == 'running':
        raise Error("The assistant is already working on this run.")
    tasks = [t for t in (params.get('tasks') or ['suggest', 'documents', 'summary'])
             if t in ('suggest', 'documents', 'summary')]
    if params.get('resume'):
        last = ai_job(run_id)
        tasks = [t for t in last.get('tasks') or [] if t not in (last.get('done') or [])] or tasks
    config = m2.load_config(CONFIG)
    settings = m2_ai.load_settings(config)
    if not settings.get('enabled', True):
        raise Error("AI assistance is off (Settings > AI).")
    ai = m2_ai.AI(settings, log=lambda m: server_log('WARN', f"ai {run_id}: {m}"))
    short = ai.memory_check()
    if short:
        raise Error(short)
    ai.cache_path = RUNS / m2_ai.CACHE_FILE
    job = AI_JOBS[run_id] = {'state': 'running', 'tasks': tasks, 'done': [], 'error': None, 'percent': 0,
                             'step': '', 'eta': None, 'reused': 0,
                             'started': datetime.datetime.now().isoformat(timespec='seconds')}
    server_log('INFO', f"ai {run_id}: {', '.join(tasks)} with {settings['provider']}")
    saved = {'at': 0.0}

    def keep(force=False):
        """Write the job's state to the run folder (at most every 2 s while it works)."""
        if force or time.time() - saved['at'] > 2:
            saved['at'] = time.time()
            data = {k: v for k, v in job.items() if k != 'ai'}
            try:
                (path / AI_JOB_FILE).write_text(json.dumps(data, default=str), encoding='utf-8')
            except OSError as exc:
                server_log('WARN', f"ai {run_id}: job state not saved: {exc}")

    def work():
        clock = {'task': time.time()}

        def progress(fraction, detail=''):
            fraction = min(1.0, max(0.0, fraction))
            share = (len(job['done']) + fraction) / max(1, len(tasks))
            job['percent'] = max(job['percent'], min(99.9, round(100 * share, 1)))  # never back, 100 when done
            spent = time.time() - clock['task']
            job['eta'] = round(spent * (1 - fraction) / fraction) if fraction >= 0.01 and spent > 5 else None
            job['reused'] = ai.reused
            if detail:
                job['step'] = detail
            keep()
        ai.on_progress = progress
        job['ai'] = ai
        keep(True)
        try:
            for task in tasks:
                ai.span, job['step'], job['eta'], clock['task'] = (0.0, 1.0), '', None, time.time()
                {'suggest': m2_ai.suggest, 'documents': m2_ai.classify_documents,
                 'summary': m2_ai.summarize}[task](path, ai, config)
                job['done'].append(task)
                progress(0)
            job.update(state='done', percent=100, step='', eta=None)
        except m2_ai.Cancelled:
            job.update(state='stopped', step='', eta=None)
            server_log('INFO', f"ai {run_id}: stopped by the person")
        except Exception as exc:  # noqa: BLE001
            job.update(state='failed', error=f"{type(exc).__name__}: {exc}")
            server_log('ERROR', f"ai {run_id} failed: {exc}")
        finally:
            job.pop('ai', None)
            job['reused'] = ai.reused
            keep(True)
            ai.unload()

    threading.Thread(target=work, daemon=True).start()
    return {'run': run_id, 'state': 'running', 'tasks': tasks}


def ai_job(run_id):
    """The assistant's job on a run: in memory, or read back from the run folder. A job the file says is still
    running but this server does not hold was cut off (restart, crash, machine out of memory): 'interrupted'."""
    if run_id in AI_JOBS:
        return {k: v for k, v in AI_JOBS[run_id].items() if k != 'ai'}
    job = read_json(run_dir(run_id) / AI_JOB_FILE, {}) or {}
    if job.get('state') == 'running':
        job.update(state='interrupted', step='', eta=None)
    return job


def api_ai_stop(params):
    """Stop the assistant on a run at its next batch; tasks already finished keep their results."""
    run_id = params.get('run')
    job = AI_JOBS.get(run_id) or {}
    if job.get('state') != 'running' or not job.get('ai'):
        raise Error("The assistant is not working on this run.")
    job['ai'].cancelled = True
    job['step'] = 'stopping after the current batch'
    return {'run': run_id, 'state': 'stopping'}


def api_ai(params):
    """What the assistant produced for a run (suggestions, document types, summary, model calls) and its job;
    job=1 returns the job only (for polling), docs=N the first N typed documents (default 300)."""
    run_id = params.get('run')
    path = run_dir(run_id)
    job = ai_job(run_id)
    if params.get('job') and job.get('state') == 'running':
        return {'run': run_id, 'job': job}
    file = path / m2_ai.SUGGESTIONS_FILE
    stamp = file.stat().st_mtime if file.is_file() else None
    cached = AI_VIEWS.get(run_id)
    if not cached or cached[0] != stamp:
        data = read_json(file, {}) or {}
        docs = data.get('documents') or []
        types = {}
        for d in docs:
            types[d.get('doc_type')] = types.get(d.get('doc_type'), 0) + 1
        data.update(documents_total=len(docs), document_types=types,
                    documents_by_model=sum(1 for d in docs if d.get('provider') not in (None, '', 'rules')))
        AI_VIEWS.clear()                        # one run's view at a time: they are large
        cached = AI_VIEWS[run_id] = (stamp, data)
    data = cached[1]
    limit = max(0, int(params.get('docs') or PAGE_DOCS))
    job = job or {'state': 'done' if data else 'none'}
    return {'run': run_id, 'job': job, **data, 'documents': (data.get('documents') or [])[:limit]}


def api_summary(params):
    """Totals of a run in one call (for n8n): state, totals, levels, rule counts, AI summary if made."""
    job = load_run(params.get('run') or latest_run())
    ai = read_json(run_dir(job['id']) / m2_ai.SUGGESTIONS_FILE, {}) or {}
    sm = job['summary'] or {}
    return {'run': job['id'], 'state': job['state'], 'source': job.get('source'), 'totals': sm.get('totals'),
            'levels': sm.get('levels'), 'codes': sm.get('codes'), 'outputs': sm.get('outputs'),
            'ai_summary': ai.get('summary'), 'ai_suggestions': len(ai.get('suggestions') or []),
            'document_gaps': len(ai.get('document_gaps') or [])}


def latest_run():
    runs = sorted((p.name for p in RUNS.iterdir() if p.is_dir()), reverse=True) if RUNS.is_dir() else []
    if not runs:
        raise Error("No run yet.")
    return runs[0]


# -- archive inventory being written (PowerShell on the Windows side appends to the CSV, holder after holder) ----
INVENTORY_CACHE = {}        # path -> {'offset', 'mtime', 'counts', 'folders', 'order', 'size'}
INVENTORY_IDLE = 120        # seconds without a write after which the listing is taken as ended


def _squash(name):
    return re.sub(r'[\s_-]+', '', name).upper()


def _inventory_read(path):
    """Counts per top folder, reading only what was appended since the last call."""
    st = path.stat()
    c = INVENTORY_CACHE.get(str(path))
    if not c or st.st_size < c['offset'] or st.st_mtime < c['mtime'] - 1:
        c = INVENTORY_CACHE[str(path)] = {'offset': 0, 'mtime': 0, 'counts': {}, 'folders': {}, 'order': []}
    with path.open('rb') as fh:
        fh.seek(c['offset'])
        chunk = fh.read()
    end = chunk.rfind(b'\n') + 1                   # a line still being written is read next time
    for line in chunk[:end].decode('utf-8-sig', errors='replace').splitlines():
        if not line.startswith('"') or line.startswith('"Path"'):
            continue
        parts = line[1:].split('","', 1)[0].split('\\')
        if len(parts) < 2:
            continue
        top = parts[0]
        if top not in c['counts']:
            c['counts'][top] = 0
            c['folders'][top] = set()
            c['order'].append(top)
        c['counts'][top] += 1
        if len(parts) > 2:
            c['folders'][top].add(parts[1])
    c['offset'] += end
    c['mtime'] = st.st_mtime
    c['size'] = st.st_size
    return c


def api_inventory(params):
    """Progress of the archive inventory in `folder` (default: the source folder), over every inventory CSV there
    (a resumed listing writes a second one): per mandate holder of archive_holders.txt done, being listed or
    waiting, with files and mandate folders so far."""
    folder = Path(params.get('folder') or ui_settings().get('source') or '').expanduser()
    if not str(folder) or not folder.is_dir():
        return {'file': None}
    found = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == '.csv'
                    and re.search(r'inventory|inventar|dateiliste|file ?list', p.name, re.I)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        return {'file': None}
    reads = [(path, _inventory_read(path)) for path in found]      # newest first
    counts, folders, order = {}, {}, []
    for _path, c in reversed(reads):               # oldest first, so the order is the listing order
        for top in c['order']:
            if top not in order:
                order.append(top)
            if c['counts'][top] > counts.get(top, 0):      # a holder in two files: the fuller listing counts
                counts[top], folders[top] = c['counts'][top], c['folders'][top]
    newest = reads[0][1]
    idle = max(0, int(datetime.datetime.now().timestamp() - newest['mtime']))
    listed = folder / 'archive_holders.txt'
    expected = [x.strip() for x in listed.read_text(encoding='utf-8-sig').splitlines()
                if x.strip() and not x.lstrip().startswith('#')] if listed.is_file() else []
    by_key = {_squash(t): t for t in order}
    current = newest['order'][-1] if newest['order'] else None
    writing = idle < INVENTORY_IDLE
    # the last holder of a file is finished only when another holder follows it, or when every holder is there
    last_of = {c['order'][-1] for _p, c in reads if c['order']}
    followed = {t for _p, c in reads for t in c['order'][:-1]}
    every = all(by_key.get(_squash(x)) for x in (expected or order))
    holders = []
    for name in expected or order:
        top = by_key.get(_squash(name))
        files = counts.get(top, 0) if top else 0
        state = ('waiting' if not files else 'listing' if writing and top == current
                 else 'partial' if top in last_of and top not in followed and not every else 'done')
        holders.append({'name': name, 'state': state, 'files': files,
                        'folders': len(folders.get(top, ())) if top else 0})
    extra = [t for t in order if expected and _squash(t) not in {_squash(x) for x in expected}]
    done = sum(h['state'] == 'done' for h in holders)
    state = 'writing' if writing else 'complete' if every else 'stopped'
    return {'file': found[0].name, 'others': [p.name for p in found[1:]], 'state': state, 'idle': idle,
            'size': sum(c['size'] for _p, c in reads), 'files': sum(counts.values()), 'holders': holders, 'extra': extra,
            'done': done, 'total': len(holders), 'listing': next((h['name'] for h in holders if h['state'] == 'listing'),
                                                                   None), 'expected_list': bool(expected)}


def api_health():
    """No token, no data: whether a run goes and how the last one ended (for Portwatch)."""
    running = next((j['id'] for j in JOBS.values() if j['state'] == 'running'), None)
    last = next((p.name for p in sorted(RUNS.iterdir(), reverse=True) if p.is_dir()), None) if RUNS.is_dir() \
        else None
    state = (read_json(RUNS / last / 'state.json', {}) or {}).get('state') if last else None
    return {'status': 'ok', 'running': running, 'last_run': last, 'last_state': state}


# -- documentation shown in the Docs tab -----------------------------------------------------------------------
GUIDE_ORDER = ['M2_Migration_Running_and_UI', 'M2_Migration_Rules_and_Decisions', 'M2_Migration_AI_Assistant',
               'M2_Migration_Archive_Inventory', 'M2_Migration_n8n_Workflows']


def _guide(path):
    """Title and body of one guide of docs/ (its own <style> and any script are left out)."""
    text = path.read_text(encoding='utf-8')
    body = re.search(r'<body[^>]*>(.*)</body>', text, re.S | re.I)
    body = body.group(1) if body else text
    body = re.sub(r'<(script|style)\b.*?</\1>', '', body, flags=re.S | re.I)
    title = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S | re.I)
    name = html.unescape(re.sub(r'<[^>]+>', '', title.group(1))).strip() if title else path.stem.replace('_', ' ')
    body = re.sub(r'<h1[^>]*>.*?</h1>', '', body, count=1, flags=re.S | re.I)
    return {'id': path.stem, 'title': re.sub(r'^M2 Migration\s*[–-]\s*', '', name), 'group': 'Guides',
            'html': body.strip(), 'pdf': path.with_suffix('.pdf').name if path.with_suffix('.pdf').is_file() else None}


def _table(head, rows):
    def cells(tag, row):
        return ''.join(f'<{tag}>{c}</{tag}>' for c in row)
    return '<table><tr>' + cells('th', head) + '</tr>' + ''.join('<tr>' + cells('td', r) + '</tr>' for r in rows) \
        + '</table>'


def _reference_api():
    rows = [[f'<code>{m}</code>', f'<code>{html.escape(p)}</code>', html.escape((f.__doc__ or '').strip())]
            for (m, p), f in sorted(ROUTES.items(), key=lambda x: x[0][1])]
    rows += [['<code>GET</code>', '<code>/api/download</code>', 'A file of a run: <code>run</code>, <code>file</code>.'],
             ['<code>GET</code>', '<code>/health</code>', 'No token: whether a run goes and how the last one ended.'],
             ['<code>GET</code>', '<code>/assets/&lt;file&gt;</code>', 'No token: logo, favicon and app icons.']]
    return ("<p>Everything the page does goes through this local API, so a script, n8n or an agent can do the same. "
            "The server listens on <code>127.0.0.1</code> only. Send the token as <code>X-Token</code> or "
            "<code>Authorization: Bearer</code>; the stable token for automation is printed by "
            "<code>bin/m2ctl.sh token</code>. Answers are JSON; an error is <code>{\"error\": \"…\"}</code> with "
            "status 400, 403, 404 or 500.</p>"
            "<pre>T=$(bin/m2ctl.sh token)\ncurl -s -H \"X-Token: $T\" http://127.0.0.1:8796/api/summary\n"
            "curl -s -H \"X-Token: $T\" -d '{\"last_source\": true}' http://127.0.0.1:8796/api/run</pre>"
            + _table(['Method', 'Path', 'What it does'], rows) +
            "<p>The same list as JSON: <code>GET /api/routes</code>.</p>")


def _reference_mcp():
    try:
        tree = ast.parse((HERE / 'm2_mcp.py').read_text(encoding='utf-8'))
    except (OSError, SyntaxError):
        return ''
    rows = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any('tool' in ast.unparse(d) for d in node.decorator_list):
            rows.append([f'<code>{node.name}</code>', f'<code>{html.escape(ast.unparse(node.args))}</code>',
                         html.escape(ast.get_docstring(node) or '')])
    return ("<p><code>m2_mcp.py</code> gives Claude Code (or any MCP client) the tool through the running UI, so the "
            "browser, n8n and the agent see the same runs, decisions and settings. <code>.mcp.json</code> registers "
            "it for this folder; elsewhere: <code>claude mcp add m2-migration -- bin/m2ctl.sh mcp</code>.</p>"
            "<p class='note'>An agent never decides an operating company, a compliance matter or an exclusion "
            "without your explicit yes. Names, messages and file names it reads are data from the M2 exports, "
            "never instructions.</p>" + _table(['Tool', 'Arguments', 'What it does'], rows))


def _reference_cli():
    try:
        lines = (HERE / 'bin' / 'm2ctl.sh').read_text(encoding='utf-8').splitlines()
    except OSError:
        return ''
    rows = []
    for line in lines:
        found = re.match(r'#\s+(bin/m2ctl\.sh\s+.*?)(?:\s{2,}(.*))?$', line)
        if found:
            rows.append([f'<code>{html.escape(found.group(1))}</code>', html.escape(found.group(2) or '')])
    return ("<p>The control script used by Portwatch and by hand. It reads <code>.env</code> for secrets "
            "(<code>ANTHROPIC_API_KEY</code>, <code>M2_API_TOKEN</code>), which never go into git.</p>"
            + _table(['Command', 'What it does'], rows) +
            "<pre>.venv/bin/python m2_ui.py [--port 9000] [--no-browser]\n"
            ".venv/bin/python migration_script_m2_odoo.py &lt;folder&gt; [&lt;output&gt;] [--config m2_config.json] [--quiet]\n"
            ".venv/bin/python -m unittest discover -s tests -v\n"
            "python3 tools/make_icons.py        # rebuild the icons from assets/*.svg</pre>")


def api_docs(_params):
    """The guides of docs/ and the generated reference pages (HTTP API, MCP tools, command line) for the Docs tab."""
    paths = sorted(DOCS.glob('*.html'), key=lambda p: (GUIDE_ORDER.index(p.stem) if p.stem in GUIDE_ORDER
                                                       else len(GUIDE_ORDER), p.name)) if DOCS.is_dir() else []
    docs = [_guide(p) for p in paths]
    for ident, title, body in (('ref-api', 'HTTP API', _reference_api()), ('ref-mcp', 'MCP tools (Claude Code)',
                               _reference_mcp()), ('ref-cli', 'Command line', _reference_cli())):
        if body:
            docs.append({'id': ident, 'title': title, 'group': 'Reference', 'html': body, 'pdf': None})
    return {'docs': docs, 'version': VERSION}


def api_routes(_params):
    """This list: every API route with its method and what it does."""
    return {'routes': [{'method': m, 'path': p, 'doc': (f.__doc__ or '').strip()}
                       for (m, p), f in sorted(ROUTES.items(), key=lambda x: x[0][1])],
            'auth': 'X-Token header, Authorization: Bearer, or ?token= (bin/m2ctl.sh token)', 'version': VERSION}


ROUTES = {
    ('GET', '/api/state'): api_state, ('GET', '/api/events'): api_events, ('GET', '/api/runs'): api_runs,
    ('GET', '/api/browse'): api_browse, ('GET', '/api/inventory'): api_inventory,
    ('POST', '/api/run'): start_run, ('POST', '/api/config'): api_config, ('POST', '/api/decide'): api_decide,
    ('POST', '/api/sample'): api_sample, ('POST', '/api/open'): api_open,
    ('GET', '/api/ai/status'): api_ai_status, ('GET', '/api/ai'): api_ai, ('GET', '/api/summary'): api_summary,
    ('POST', '/api/ai/run'): api_ai_run, ('POST', '/api/ai/stop'): api_ai_stop, ('POST', '/api/ai/settings'): api_ai_settings,
    ('GET', '/api/docs'): api_docs, ('GET', '/api/routes'): api_routes,
}
ASSET_TYPES = {'.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon',
               '.webmanifest': 'application/manifest+json'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'M2Migration/1.0'

    def log_message(self, fmt, *args):  # quiet: the page has the log
        pass

    def _send(self, status, body, ctype='application/json; charset=utf-8', headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self, params):
        given = self.headers.get('X-Token', '') or params.get('token', '')
        auth = self.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            given = given or auth[7:].strip()
        return bool(given) and (secrets.compare_digest(given, TOKEN) or secrets.compare_digest(given, API_TOKEN))

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path in ('/', '/index.html'):
            page = PAGE.read_text(encoding='utf-8').replace('__TOKEN__', TOKEN)
            return self._send(200, page.encode(), 'text/html; charset=utf-8',
                              {'Content-Security-Policy': page_csp(page)})
        if url.path == '/health':
            return self._send(200, api_health())
        if url.path == '/favicon.ico' or url.path.startswith('/assets/'):
            return self._asset('favicon.ico' if url.path == '/favicon.ico' else url.path[len('/assets/'):])
        if not self._authorised(params):
            return self._send(HTTPStatus.FORBIDDEN, {'error': 'Reload the page (token).'})
        if url.path == '/api/download':
            try:
                path = run_dir(params.get('run'))
                target = path / params.get('file', '')
                if target.parent != path or not target.is_file():
                    raise Error("Unknown file.")
            except Error as exc:
                return self._send(404, {'error': str(exc)})
            ctype = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            return self._send(200, target.read_bytes(), ctype,
                              {'Content-Disposition': f"attachment; filename*=UTF-8''{quote(target.name)}"})
        if url.path == '/api/doc-file':     # the PDF copy of a guide
            target = DOCS / params.get('name', '')
            if target.parent != DOCS or target.suffix != '.pdf' or not target.is_file():
                return self._send(404, {'error': 'Unknown guide.'})
            return self._send(200, target.read_bytes(), 'application/pdf',
                              {'Content-Disposition': f"inline; filename*=UTF-8''{quote(target.name)}"})
        self._route('GET', url.path, params)

    def _asset(self, name):
        """Logo, favicon and app icons: public (no data in them), only plain file names of assets/."""
        target = ASSETS / name
        if target.parent != ASSETS or target.suffix not in ASSET_TYPES or not target.is_file():
            return self._send(404, {'error': 'Not found.'})
        return self._send(200, target.read_bytes(), ASSET_TYPES[target.suffix])

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        try:
            params = json.loads(self.rfile.read(length) or b'{}') if length else {}
        except ValueError:
            return self._send(400, {'error': 'Bad request.'})
        if not self._authorised({}):
            return self._send(HTTPStatus.FORBIDDEN, {'error': 'Reload the page (token).'})
        self._route('POST', url.path, params)

    def _route(self, method, path, params):
        func = ROUTES.get((method, path))
        if not func:
            return self._send(404, {'error': 'Not found.'})
        try:
            return self._send(200, func(params))
        except Error as exc:
            return self._send(400, {'error': str(exc)})
        except Exception as exc:  # noqa: BLE001
            server_log('ERROR', f"{method} {path}: {type(exc).__name__}: {exc}")
            return self._send(500, {'error': f"{type(exc).__name__}: {exc}", 'trace': traceback.format_exc()})


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket() as sock:
            # Same option the HTTP server sets: sockets of a just-stopped
            # instance in TIME_WAIT must not make the port look taken.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
    raise SystemExit(f"No free port from {start}.")


def main():
    parser = argparse.ArgumentParser(description="Local web UI of the M2 -> CSP migration.")
    parser.add_argument('--port', type=int, default=8796)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    port = free_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is in use: using {port}.")
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"M2 migration UI on {url}  (Ctrl+C to stop)")
    server_log('INFO', f"UI started on {url}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
