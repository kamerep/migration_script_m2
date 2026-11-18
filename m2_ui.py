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
import datetime
import errno
import json
import mimetypes
import os
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
AI_JOBS = {}                    # run id -> {'state': running|done|failed, 'tasks': [...], 'error': ...}
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
    return job


def api_events(params):
    job = load_run(params.get('run'))
    since = int(params.get('since') or 0)
    events = job['events'][since:since + 5000]
    return {'run': job['id'], 'state': job['state'], 'events': events, 'next': since + len(events),
            'total': len(job['events']), 'summary': job['summary'] if job['state'] != 'running' else None,
            'trace': job.get('trace')}


def api_runs(_params):
    runs = []
    if RUNS.is_dir():
        for path in sorted(RUNS.iterdir(), reverse=True)[:60]:
            if not path.is_dir():
                continue
            summary = read_json(path / 'summary.json') or {}
            state = read_json(path / 'state.json') or {}
            running = JOBS.get(path.name, {}).get('state') == 'running'
            runs.append({'id': path.name, 'state': 'running' if running else state.get('state') or
                         ('done' if summary else 'failed'),
                         'source': state.get('source') or summary.get('source'),
                         'totals': summary.get('totals'), 'levels': summary.get('levels')})
    return {'runs': runs}


def api_state(_params):
    config = m2.load_config(CONFIG)
    return {'config': config, 'settings': ui_settings(), 'kinds': {k: v['title'] for k, v in m2.KINDS.items()},
            'kind_fields': {k: sorted(v['fields']) for k, v in m2.KINDS.items()},
            'spec': m2.SPEC_PATH, 'sheets': {k: v['title'] for k, v in m2.SHEETS.items()},
            'running': next((j['id'] for j in JOBS.values() if j['state'] == 'running'), None),
            'sample': str(HERE / 'sample_m2')}


def api_config(params):
    config = params.get('config')
    if not isinstance(config, dict):
        raise Error("Settings must be a JSON object.")
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return {'ok': True}


def api_decide(params):
    """One decision from the Decisions panel, written to the settings for the next run."""
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
        allowed = ('directors', 'ubos', 'shareholders', 'secretaries', 'agents', 'contacts', 'signatories', 'ignore')
        if value not in allowed:
            raise Error(f"Choose one of {', '.join(allowed)}.")
        config.setdefault('officer_types', {})[key] = value
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
           'min_confidence': float, 'batch': int, 'timeout': int, 'companies': list,
           'after_run': bool}


def api_ai_status(_params):
    config = m2.load_config(CONFIG)
    return {**m2_ai.AI(m2_ai.load_settings(config)).status(), 'settings': m2_ai.load_settings(config)}


def api_ai_settings(params):
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
    config = m2.load_config(CONFIG)
    settings = m2_ai.load_settings(config)
    if not settings.get('enabled', True):
        raise Error("AI assistance is off (Settings > AI).")
    job = AI_JOBS[run_id] = {'state': 'running', 'tasks': tasks, 'done': [], 'error': None,
                             'started': datetime.datetime.now().isoformat(timespec='seconds')}
    server_log('INFO', f"ai {run_id}: {', '.join(tasks)} with {settings['provider']}")

    def work():
        ai = m2_ai.AI(settings, log=lambda m: server_log('WARN', f"ai {run_id}: {m}"))
        try:
            for task in tasks:
                {'suggest': m2_ai.suggest, 'documents': m2_ai.classify_documents,
                 'summary': m2_ai.summarize}[task](path, ai, config)
                job['done'].append(task)
            job['state'] = 'done'
        except Exception as exc:  # noqa: BLE001
            job.update(state='failed', error=f"{type(exc).__name__}: {exc}")
            server_log('ERROR', f"ai {run_id} failed: {exc}")

    threading.Thread(target=work, daemon=True).start()
    return {'run': run_id, 'state': 'running', 'tasks': tasks}


def api_ai(params):
    run_id = params.get('run')
    path = run_dir(run_id)
    data = read_json(path / m2_ai.SUGGESTIONS_FILE, {}) or {}
    job = AI_JOBS.get(run_id) or {'state': 'done' if data else 'none'}
    return {'run': run_id, 'job': job, **data}


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


def api_health():
    """No token, no data: whether a run goes and how the last one ended (for Portwatch)."""
    running = next((j['id'] for j in JOBS.values() if j['state'] == 'running'), None)
    last = next((p.name for p in sorted(RUNS.iterdir(), reverse=True) if p.is_dir()), None) if RUNS.is_dir() \
        else None
    state = (read_json(RUNS / last / 'state.json', {}) or {}).get('state') if last else None
    return {'status': 'ok', 'running': running, 'last_run': last, 'last_state': state}


ROUTES = {
    ('GET', '/api/state'): api_state, ('GET', '/api/events'): api_events, ('GET', '/api/runs'): api_runs,
    ('GET', '/api/browse'): api_browse,
    ('POST', '/api/run'): start_run, ('POST', '/api/config'): api_config, ('POST', '/api/decide'): api_decide,
    ('POST', '/api/sample'): api_sample, ('POST', '/api/open'): api_open,
    ('GET', '/api/ai/status'): api_ai_status, ('GET', '/api/ai'): api_ai, ('GET', '/api/summary'): api_summary,
    ('POST', '/api/ai/run'): api_ai_run, ('POST', '/api/ai/settings'): api_ai_settings,
}


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
                              {'Content-Security-Policy': "default-src 'self'; style-src 'unsafe-inline'; "
                                                          "script-src 'unsafe-inline'; img-src 'self' data:"})
        if url.path == '/health':
            return self._send(200, api_health())
        if url.path == '/favicon.ico':
            return self._send(HTTPStatus.NO_CONTENT, b'', 'image/x-icon')
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
        self._route('GET', url.path, params)

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
