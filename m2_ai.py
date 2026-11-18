#!/usr/bin/env python3
"""AI assistance for the M2 -> CSP migration: local Ollama by default, Claude for the hard cases.

    .venv/bin/python m2_ai.py status                 which providers answer, with which models
    .venv/bin/python m2_ai.py suggest runs/<stamp>   suggestions for the open decisions of a run
    .venv/bin/python m2_ai.py documents runs/<stamp> classify the indexed documents of a run
    .venv/bin/python m2_ai.py summary runs/<stamp>   plain-language summary of a run (local model only)

The migration itself stays rule-based and repeatable: the AI never changes a run. It reads a finished run
(journal.jsonl, summary.json) and writes runs/<stamp>/ai_suggestions.json. Each suggestion is a proposed
decision for the *Needs decision* tab (the same decision a person would take there) with its confidence, the
provider and model that made it, and its reason. Nothing is applied until somebody accepts it.

Both providers are spoken to with the Anthropic SDK over the Messages API: Ollama serves the same API on
http://localhost:11434 (Ollama >= 0.14), the cloud is api.anthropic.com.

Privacy (settings `ai` in m2_config.json):
  * provider 'ollama' (default): everything stays on this machine.
  * provider 'auto': Ollama first; a suggestion below `escalate_below` confidence is asked again to Claude,
    only when `allow_cloud` is on and only for tasks whose input is headers and codes (column titles, M2 type
    codes, mandate-holder codes, legal-form words). Client names, file names, sample values and documents are
    never sent to the cloud.
  * provider 'claude': the cloud-safe tasks go to Claude; the others still run on Ollama.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / 'm2_config.json'
SUGGESTIONS_FILE = 'ai_suggestions.json'

DEFAULTS = {
    'enabled': True,
    'provider': 'ollama',                    # ollama | auto | claude
    'ollama_url': 'http://localhost:11434',
    'ollama_model': 'llama3.1:8b',
    'embed_model': 'nomic-embed-text',
    'min_confidence': 0.35,             # weaker suggestions are not shown
    'claude_model': 'claude-opus-5-5',
    'claude_effort': 'low',
    'allow_cloud': False,                    # company switch: may headers/codes go to Claude?
    'escalate_below': 0.6,
    'timeout': 120,
    'batch': 25,
    'companies': [],                         # Odoo operating companies the holder codes may map to
    'after_run': False,                      # run the assistant by itself after each migration run (UI)
}

# Tasks whose input is only headers / codes: the only ones that may ever go to the cloud.
CLOUD_SAFE = {'columns_cloud', 'file_kinds_cloud', 'officer_types', 'companies', 'legal_forms'}

REGISTERS = ('directors', 'ubos', 'shareholders', 'secretaries', 'agents', 'contacts', 'signatories', 'ignore')
ENTITY_TYPES = ('company', 'foundation', 'trust', 'individual')

DOC_TYPES = {
    'passport_id': r'pass(port)?|reisepass|\bid\b|identit|ausweis|identity card|\bpp\b',
    'proof_of_address': r'utility|proof of address|wohnsitz|address proof|rechnung strom|meldebest|residence',
    'incorporation': r'incorporat|gr[uü]ndung|certificate of inc|\bcoi\b|formation',
    'register_extract': r'register|handelsregister|\bhr[- _]?auszug|auszug|extract|good standing|incumbency',
    'articles': r'statut|articles|memorandum|\bm&a\b|\bmoa\b|\baoa\b|satzung|bylaws|deed|trust deed|charter',
    'share_register': r'aktienbuch|share ?register|shareholder|aktien|share cert|anteil',
    'resolution': r'resolution|beschluss|protokoll|minutes|\bgv\b|\bvr\b|board',
    'power_of_attorney': r'vollmacht|power of attorney|\bpoa\b|signatory|zeichnung',
    'kyc_compliance': r'kyc|\bform a\b|\bform k\b|\bfatca\b|\bcrs\b|world ?check|pep|compliance|due diligence|'
                      r'\bcdd\b|\bedd\b|sanction|source of (funds|wealth)|risk',
    'lei': r'\blei\b|legal entity identifier',
    'invoice_fee': r'invoice|rechnung|honorar|\bfee\b|faktura|\babf\b|\bflf\b|\bbill\b',
    'financials': r'bilanz|balance sheet|financial|jahresrechnung|annual account|audit|revision|p&l|'
                  r'erfolgsrechnung|accounting|buchhaltung|ledger|trial balance|adjusting entr|\bfs\b',
    'bank': r'\bbank|kontoauszug|bank statement|account statement|\biban\b|mandate letter|account opening|'
            r'kontoeröffnung',
    'tax': r'steuer|\btax\b|\bvat\b|mwst|\btin\b',
    'contract': r'vertrag|contract|agreement|engagement|mandatsvertrag|\bnda\b|service agreement',
    'correspondence': r'brief|letter|mail|korrespondenz|correspondence|\.msg$|\.eml$',
}
DOC_RULES = [(k, re.compile(v, re.I)) for k, v in DOC_TYPES.items()]
# Program data of accounting software (QuickBooks, Banana, ...): not documents to attach to the client.
SYSTEM_FILE = re.compile(r'\.(qbw|qbb|qbm|qbo|tlg|nd|dsn|ini|lgb|cfs|cfx|cfe|si|dat|db|lnk|tmp|bak|ldb|lck)$|'
                         r'(^|[\\/])(segments(_\w+)?|segments\.gen|_[a-z0-9]+\.(cfs|cfx|cfe|si))$', re.I)
# Names that say nothing about the content (scanner output, numbers): no model is asked, a person must look.
GENERIC_NAME = re.compile(r'^(scan|img|image|dsc|doc|dok|document|dokument|file|datei|neu|new|copy|kopie)?'
                          r'[\s_-]*[\d\s_()-]*\.[a-z0-9]{2,5}$', re.I)


# ---------------------------------------------------------------------------------------------------------------
def load_settings(config=None, path=DEFAULT_CONFIG):
    if config is None:
        try:
            config = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            config = {}
    ai = dict(DEFAULTS)
    ai.update({k: v for k, v in (config.get('ai') or {}).items() if not k.startswith('_')})
    if os.environ.get('M2_AI_PROVIDER'):
        ai['provider'] = os.environ['M2_AI_PROVIDER']
    if os.environ.get('OLLAMA_HOST'):
        host = os.environ['OLLAMA_HOST']
        ai['ollama_url'] = host if host.startswith('http') else f'http://{host}'
    return ai


def parse_json(text):
    """The first JSON object or array in a model answer (models like to wrap it in prose or ``` fences)."""
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.M).strip()
    pairs = sorted((('[', ']'), ('{', '}')), key=lambda oc: (text.find(oc[0]) == -1, text.find(oc[0])))
    for opener, closer in pairs:            # the outermost bracket first: '{"a": [..]}' is an object
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise ValueError(f"no JSON in the answer: {text[:200]!r}")


class AI:
    """One object per run of the assistant: the two clients, the call log and the privacy gate."""

    def __init__(self, settings=None, log=None):
        self.s = settings or load_settings()
        self.calls = []
        self.log = log or (lambda msg: None)
        self._clients = {}

    # -- providers -------------------------------------------------------------------------------------------
    def _client(self, provider):
        if provider not in self._clients:
            import anthropic  # noqa: PLC0415 - optional: the migration runs without it
            if provider == 'ollama':
                self._clients[provider] = anthropic.Anthropic(base_url=self.s['ollama_url'], api_key='ollama',
                                                              timeout=self.s['timeout'], max_retries=1)
            else:
                self._clients[provider] = anthropic.Anthropic(timeout=self.s['timeout'])
        return self._clients[provider]

    def embed(self, texts):
        """Vectors of texts from the local embedding model (cached); None when it does not answer."""
        cache = self.__dict__.setdefault('_vectors', {})
        todo = [t for t in dict.fromkeys(texts) if t not in cache]
        if todo:
            import urllib.request  # noqa: PLC0415
            started = time.time()
            try:
                req = urllib.request.Request(self.s['ollama_url'].rstrip('/') + '/api/embed', json.dumps(
                    {'model': self.s['embed_model'], 'input': todo}).encode(), {'content-type': 'application/json'})
                with urllib.request.urlopen(req, timeout=self.s['timeout']) as r:  # noqa: S310 - local Ollama
                    cache.update(zip(todo, json.loads(r.read())['embeddings']))
                self.calls.append({'task': 'embed', 'provider': 'ollama', 'model': self.s['embed_model'],
                                   'at': datetime.datetime.now().isoformat(timespec='seconds'),
                                   'items': len(todo), 'ok': True, 'seconds': round(time.time() - started, 2)})
            except Exception as exc:  # noqa: BLE001
                self.calls.append({'task': 'embed', 'provider': 'ollama', 'ok': False, 'error': str(exc)[:200]})
                return None
        return [cache[t] for t in texts]

    def similarity(self, text, candidates):
        """Best cosine similarity of text to any candidate (0..1), or None without the embedding model."""
        vectors = self.embed([text] + list(candidates)) if candidates else None
        if not vectors:
            return None
        a = vectors[0]
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        best = 0.0
        for b in vectors[1:]:
            nb = math.sqrt(sum(x * x for x in b)) or 1.0
            best = max(best, sum(x * y for x, y in zip(a, b)) / (na * nb))
        return best

    def cloud_ready(self):
        return bool(self.s['allow_cloud']) and bool(os.environ.get('ANTHROPIC_API_KEY')
                                                    or os.environ.get('ANTHROPIC_AUTH_TOKEN'))

    def route(self, task):
        """Provider for the first attempt of a task."""
        if self.s['provider'] == 'claude' and task in CLOUD_SAFE and self.cloud_ready():
            return 'claude'
        return 'ollama'

    def ask(self, task, system, payload, provider=None, max_tokens=4000):
        """One JSON question; returns the parsed answer (or None) and records the call."""
        provider = provider or self.route(task)
        if provider == 'claude' and task not in CLOUD_SAFE:
            raise PermissionError(f"task {task} may not leave this machine")
        user = json.dumps(payload, ensure_ascii=False)
        record = {'task': task, 'provider': provider, 'at': datetime.datetime.now().isoformat(timespec='seconds'),
                  'prompt_sha': hashlib.sha256((system + user).encode()).hexdigest()[:12], 'items': len(payload.get(
                      'items', [])) if isinstance(payload, dict) else None}
        started = time.time()
        messages = [{'role': 'user', 'content': user}]
        try:
            client = self._client(provider)
            for attempt in (1, 2):
                if provider == 'ollama':
                    model = self.s['ollama_model']
                    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                                  messages=messages)
                else:
                    model = self.s['claude_model']
                    resp = client.beta.messages.create(
                        model=model, max_tokens=max_tokens, system=system,
                        output_config={'effort': self.s['claude_effort']},
                        betas=['server-side-fallback-2026-07-01'], fallbacks='default', messages=messages)
                    if resp.stop_reason == 'refusal':
                        raise RuntimeError('the model declined the request')
                answer = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
                try:
                    parsed = parse_json(answer)
                    break
                except ValueError:
                    if attempt == 2:
                        raise
                    # Small local models sometimes answer in prose: ask once more, for the JSON only.
                    messages = messages + [{'role': 'assistant', 'content': answer or '(empty)'},
                                           {'role': 'user', 'content': 'Reply again with only the JSON, exactly '
                                                                       'as asked, and no other text.'}]
            record.update(model=model, ok=True, seconds=round(time.time() - started, 2), attempts=attempt,
                          tokens_in=getattr(resp.usage, 'input_tokens', None),
                          tokens_out=getattr(resp.usage, 'output_tokens', None))
            self.calls.append(record)
            return parsed
        except Exception as exc:  # noqa: BLE001 - one failed call must not stop the others
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300], seconds=round(time.time() - started, 2))
            self.calls.append(record)
            self.log(f"{provider} {task}: {record['error']}")
            return None

    def status(self):
        out = {'provider': self.s['provider'], 'allow_cloud': self.s['allow_cloud'],
               'ollama': {'url': self.s['ollama_url'], 'model': self.s['ollama_model']},
               'claude': {'model': self.s['claude_model'], 'key': bool(os.environ.get('ANTHROPIC_API_KEY')
                                                                       or os.environ.get('ANTHROPIC_AUTH_TOKEN'))}}
        try:
            import urllib.request  # noqa: PLC0415
            with urllib.request.urlopen(self.s['ollama_url'].rstrip('/') + '/api/tags', timeout=5) as r:  # noqa: S310
                models = [m['name'] for m in json.loads(r.read()).get('models', [])]
            out['ollama'].update(up=True, models=models, model_present=self.s['ollama_model'] in models)
        except Exception as exc:  # noqa: BLE001
            out['ollama'].update(up=False, error=str(exc)[:200])
        try:
            import anthropic  # noqa: F401, PLC0415
            out['sdk'] = True
        except ImportError:
            out['sdk'] = False
        return out

    # -- batched classification with escalation --------------------------------------------------------------
    def classify(self, task, system, items, allowed, cloud_task=None, cloud_items=None):
        """items: [{'id':..., ...}] -> {id: {'value', 'confidence', 'reason', 'provider'}} (values in `allowed`)."""
        results = {}
        size = max(1, int(self.s['batch']))
        for i in range(0, len(items), size):
            chunk = items[i:i + size]
            answer = self.ask(task, system, {'allowed': sorted(allowed), 'items': chunk})
            for row in _rows(answer):
                key, value = str(row.get('id')), row.get('value')
                if value in allowed and key in {str(x['id']) for x in chunk}:
                    conf = _conf(row.get('confidence'))
                    if self.calls[-1].get('provider') == 'ollama':
                        conf = min(conf, 0.85)      # small local models are not calibrated
                    results[key] = {'value': value, 'confidence': conf,
                                    'reason': str(row.get('reason') or '')[:300],
                                    'provider': self.calls[-1].get('provider'), 'model': self.calls[-1].get('model')}
        # escalation of the weak ones, headers/codes only
        if self.s['provider'] in ('auto', 'claude') and self.cloud_ready():
            cloud_task = cloud_task or task
            if cloud_task in CLOUD_SAFE:
                source = {str(x['id']): x for x in (cloud_items or items)}
                weak = [source[str(x['id'])] for x in items if str(x['id']) in source and (
                    str(x['id']) not in results or results[str(x['id'])]['confidence'] < self.s['escalate_below'])]
                weak = [x for x in weak if results.get(str(x['id']), {}).get('provider') != 'claude']
                for i in range(0, len(weak), size):
                    chunk = weak[i:i + size]
                    answer = self.ask(cloud_task, system, {'allowed': sorted(allowed), 'items': chunk},
                                      provider='claude')
                    for row in _rows(answer):
                        key, value = str(row.get('id')), row.get('value')
                        if value in allowed:
                            results[key] = {'value': value, 'confidence': _conf(row.get('confidence')),
                                            'reason': str(row.get('reason') or '')[:300], 'provider': 'claude',
                                            'model': self.s['claude_model']}
        return results


def _rows(answer):
    if isinstance(answer, dict):
        answer = answer.get('items') or answer.get('results') or [answer]
    return [r for r in answer or [] if isinstance(r, dict)]


def _conf(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


ANSWER = ("Answer with JSON only: an array of objects {\"id\": <the item id>, \"value\": <one of allowed, exactly>, "
          "\"confidence\": <0..1>, \"reason\": <one short sentence>}. If no allowed value fits, use the closest "
          "with a low confidence. No prose outside the JSON.")


# ---------------------------------------------------------------------------------------------------------------
def read_run(run_dir):
    run_dir = Path(run_dir)
    events = []
    journal = run_dir / 'journal.jsonl'
    if journal.is_file():
        for line in journal.read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    try:
        summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        summary = {}
    return events, summary


def kind_fields():
    import migration_script_m2_odoo as m2  # noqa: PLC0415 - needs the CSP import spec
    return {k: sorted(v['fields']) for k, v in m2.KINDS.items()}, {k: v['title'] for k, v in m2.KINDS.items()}


KEY_FIELDS = {'mandate_no', 'address_no', 'debtor_no', 'client_ref', 'client_no', 'ref'}
CHECKS = [  # field-name pattern -> what every sample must look like
    (r'(^|_)e?mail(_|$)', re.compile(r'@')),
    (r'(^|_)iban(_|$)', re.compile(r'^[A-Z]{2}\d{2}[A-Z0-9 ]{8,}$', re.I)),
    (r'(^|_)(swift|bic)(_|$)', re.compile(r'^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$', re.I)),
    (r'(^|_)currency(_|$)', re.compile(r'^[A-Z]{3}$|^(fr|sfr|euro?s?|\$|€|£)$', re.I)),
    (r'(^|_)(date|start|end|birth|appointment|resignation)(_|$)', re.compile(r'\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|^\d{4}-\d\d-\d\d')),
    (r'(^|_)(zip|postal)(_|$)', re.compile(r'^[A-Z0-9 -]{3,10}$', re.I)),
    (r'(^|_)(phone|mobile|fax)(_|$)', re.compile(r'^[+0-9 ()/.-]{6,}$')),
    (r'(^|_)(amount|price|fee|balance|total)(_|$)', re.compile(r'^-?[0-9\'’ .,]+$')),
]


def check_alias(result, column, samples, ai=None, aliases=None):
    """Keep a column -> field suggestion only when the sample values fit the field; temper the confidence.

    The model's own confidence is not trusted alone: it is capped by how close the header is (local embedding
    model) to the header names the migration already knows for that field."""
    field, conf, notes = result['value'], result['confidence'], []
    if result.get('provider') == 'ollama':
        conf = min(conf, 0.85)          # small local models are not calibrated: never 'sure'
    sim = ai.similarity(column, list(aliases or []) + [field.replace('_', ' ')]) if ai else None
    if sim is not None:
        cap = 0.95 if sim >= 0.8 else 0.75 if sim >= 0.65 else 0.45 if sim >= 0.58 else 0.2
        conf = min(conf, cap) if sim < 0.8 else max(conf, 0.8)
        notes.append(f"header similarity {sim:.2f}")
    values = [s for s in samples if s.strip()]
    for pattern, rx in CHECKS:
        if re.search(pattern, field) and values:
            if all(rx.search(v.strip()) for v in values):
                conf = min(1.0, conf + 0.05)
                notes.append('samples fit')
            else:
                return None             # e.g. 'Remark' -> email without an @ in the samples
    if values and all(re.fullmatch(r'\d{1,2}', v) for v in values):
        conf = min(conf, 0.3)
        notes.append('samples look like a sort order')
    if field in KEY_FIELDS or field.endswith('_no'):
        conf = min(conf, 0.4)
        notes.append('key field: confirm by hand')
    return {**result, 'confidence': round(conf, 2),
            'reason': '; '.join([result.get('reason') or ''] + notes).strip('; ')}


def suggest(run_dir, ai=None, config=None):
    """Suggestions for the open decisions of a finished run -> ai_suggestions.json (also returned)."""
    run_dir = Path(run_dir)
    ai = ai or AI(load_settings(config))
    events, summary = read_run(run_dir)
    fields, titles = kind_fields()
    out = []

    # 1. columns with no CSP field -> alias of a field of the file's kind (not one the file already reads)
    import migration_script_m2_odoo as m2  # noqa: PLC0415
    fields_aliases = {k: v['fields'] for k, v in m2.KINDS.items()}
    cols = []
    for f in summary.get('files') or []:
        kind, mapped = f.get('kind'), set((f.get('mapping') or {}).keys())
        if kind not in fields:
            continue
        for u in f.get('unused') or []:
            cols.append({'id': f"c{len(cols)}", 'kind': kind, 'column': u['column'], 'file': f['file'],
                         'free': sorted(set(fields[kind]) - mapped),
                         'samples': [str(x)[:40] for x in (u.get('samples') or [])[:3]]})
    for kind in sorted({c['kind'] for c in cols}):
        group = [c for c in cols if c['kind'] == kind]
        allowed = set(group[0]['free']) | {'none'}
        system = ("You map column headers of exports of M2 Mandat (Swiss fiduciary software, German or English) to "
                  f"the fields of a '{titles.get(kind, kind)}' import. The allowed fields are the ones not yet read "
                  "from another column. 'none' means the column holds no such field (it is kept as a note): prefer "
                  "'none' for remarks, sort orders, flags and free text. Match by meaning, also across German. "
                  + ANSWER)
        local = [{'id': c['id'], 'column': c['column'], 'samples': c['samples']} for c in group]
        cloud = [{'id': c['id'], 'column': c['column']} for c in group]
        res = ai.classify('columns', system, local, allowed, cloud_task='columns_cloud', cloud_items=cloud)
        for c in group:
            r = res.get(c['id'])
            if r and r['value'] != 'none':
                r = check_alias(r, c['column'], c['samples'], ai, fields_aliases.get(kind, {}).get(r['value']))
                if r and r['confidence'] >= ai.s['min_confidence']:
                    out.append({'type': 'alias', 'key': c['column'], 'kind': kind, 'field': r['value'],
                                'file': c['file'], **r})

    # 2. files not recognised -> kind
    files = [e for e in events if e.get('code') == 'KIND_UNKNOWN']
    if files:
        allowed = set(titles) | {'ignore'}
        system = ("You classify spreadsheet exports of M2 Mandat by their column headers (and file name when "
                  "given) into one export kind. Kinds: " + '; '.join(f"{k} = {v}" for k, v in titles.items())
                  + "; ignore = not client data (phone lists, staff lists, notes). " + ANSWER)
        local = [{'id': f"f{i}", 'file': e['file'], 'header': (e.get('header') or [])[:30]} for i, e in
                 enumerate(files)]
        cloud = [{'id': x['id'], 'header': x['header']} for x in local]
        res = ai.classify('file_kinds', system, local, allowed, cloud_task='file_kinds_cloud', cloud_items=cloud)
        for x in local:
            if x['id'] in res:
                out.append({'type': 'file_kind', 'key': x['file'], 'value': res[x['id']]['value'],
                            **{k: v for k, v in res[x['id']].items() if k != 'value'}})

    # 3. M2 Detail addr. type codes / functions -> register
    codes = sorted({str(e.get('value')) for e in events if e.get('code') == 'OFFICER_FUNCTION_UNKNOWN'
                    and e.get('value')})
    if codes:
        system = ("M2 Mandat 'Detail addr.' rows link a person or company to a client with a type code or function "
                  "(German or English, often abbreviated: DIR director, VR Verwaltungsrat=directors, GF "
                  "Geschäftsführer=directors, BO/WB/wirtschaftlich Berechtigter=ubos, AK/Aktionär=shareholders, "
                  "SEC=secretaries, RA/agent=agents, CP/Kontakt=contacts, ZB/Zeichnungsberechtigt=signatories). "
                  "Pick the register sheet; 'ignore' if it is not a role to register. " + ANSWER)
        items = [{'id': f"o{i}", 'code': c} for i, c in enumerate(codes)]
        res = ai.classify('officer_types', system, items, set(REGISTERS))
        for x in items:
            if x['id'] in res:
                out.append({'type': 'officer_type', 'key': x['code'], **res[x['id']]})

    # 4. mandate holder codes -> operating company (only when the companies are known)
    companies = [c for c in (ai.s.get('companies') or []) if c] or sorted(
        {v for k, v in ((config or {}).get('company_map') or {}).items() if not k.startswith('_') and v})
    holders = sorted({str(e.get('value')) for e in events if e.get('code') == 'COMPANY_UNKNOWN' and e.get('value')})
    if holders and companies:
        system = ("M2 mandates carry a mandate-holder code (the trust company holding the mandate, e.g. IT, HK, "
                  "NOVA-PH). Pick the Odoo operating company it most likely stands for, from the allowed list, by "
                  "initials, country or name. Use a low confidence when it is a guess. " + ANSWER)
        items = [{'id': f"h{i}", 'holder': h} for i, h in enumerate(holders)]
        res = ai.classify('companies', system, items, set(companies))
        for x in items:
            if x['id'] in res:
                out.append({'type': 'company', 'key': x['holder'], **res[x['id']]})

    # 5. legal forms not known -> entity type (shown as a hint: the import column is filled by hand)
    forms = sorted({m.group(1) for e in events if e.get('code') == 'ENTITY_TYPE_UNKNOWN'
                    for m in [re.search(r"legal form '([^']*)'", e.get('message', ''))] if m})
    if forms:
        system = ("Classify legal-form words of Swiss/international entities (AG, GmbH, Stiftung, Anstalt, Trust, "
                  "Ltd, Holding, LLC, ...) into an entity type. " + ANSWER)
        items = [{'id': f"l{i}", 'legal_form': f} for i, f in enumerate(forms)]
        res = ai.classify('legal_forms', system, items, set(ENTITY_TYPES))
        for x in items:
            if x['id'] in res:
                out.append({'type': 'legal_form', 'key': x['legal_form'], **res[x['id']]})

    out = [x for x in out if x['confidence'] >= ai.s['min_confidence']]
    out.sort(key=lambda x: (x['type'], -x['confidence']))
    return save(run_dir, ai, suggestions=out)


def classify_documents(run_dir, ai=None, config=None, use_model=True):
    """Type of each indexed document: keyword rules first, the local model for the rest (never the cloud)."""
    run_dir = Path(run_dir)
    ai = ai or AI(load_settings(config))
    _events, summary = read_run(run_dir)
    docs = summary.get('documents') or []
    types, todo = {}, []
    for i, d in enumerate(docs):
        file = d.get('file') or ''
        own = f"{file} {d.get('description') or ''}"          # the file's own name first, then its folder
        hit = next((k for k, rx in DOC_RULES if rx.search(own)), None)
        how, conf = 'keyword in the file name', 0.9
        if not hit:
            hit = next((k for k, rx in DOC_RULES if rx.search(d.get('folder') or '')), None)
            how, conf = 'keyword in the folder name', 0.75
        doc_id = f"d{i}"
        if SYSTEM_FILE.search(file):
            types[doc_id] = {'value': 'system_file', 'confidence': 0.95, 'reason': 'accounting-software data file',
                             'provider': 'rules'}
        elif hit:
            types[doc_id] = {'value': hit, 'confidence': conf, 'reason': how, 'provider': 'rules'}
        elif GENERIC_NAME.match(d.get('file') or '') and not d.get('description'):
            types[doc_id] = {'value': 'other', 'confidence': 0.2, 'reason': 'generic file name: open it to type it',
                             'provider': 'rules'}
        else:
            todo.append({'id': doc_id, 'folder': d.get('folder') or '', 'file': d.get('file') or '',
                         'description': d.get('description') or ''})
    if todo and use_model:
        system = ("You classify documents of a fiduciary / corporate-services client file by folder and file name "
                  "(German or English). Types: " + ', '.join(DOC_TYPES) + ", other. " + ANSWER)
        types.update(ai.classify('documents', system, todo, set(DOC_TYPES) | {'other'}))
    rows = []
    for i, d in enumerate(docs):
        t = types.get(f"d{i}") or {'value': 'other', 'confidence': 0.0, 'reason': 'not classified', 'provider': ''}
        where = d.get('where') or ''
        rows.append({'key': d.get('key'), 'holder': where[len('archive '):] if where.startswith('archive ')
                     else where, 'folder': d.get('folder'), 'file': d.get('file'),
                     'on_disk': d.get('on_disk'), 'doc_type': t['value'], 'confidence': t['confidence'],
                     'provider': t.get('provider'), 'reason': t.get('reason')})
    coverage, gaps = document_coverage(rows)
    try:
        write_coverage_workbook(run_dir / COVERAGE_FILE, coverage, rows)
    except (OSError, ImportError) as exc:
        print(f"Coverage workbook not written: {exc}", file=sys.stderr)
    return save(run_dir, ai, documents=rows, document_gaps=gaps, document_coverage=coverage)


# -- coverage per client, across the mandate holders ---------------------------------------------------------
# The extension of an M2 mandate number is its service type; each number has exactly one extension, and the
# same mandate (with its files) can sit under any of the mandate holders' archive folders.
MANDATE_TYPES = {'ENT': 'company', 'ACC': 'company', 'CON': 'person', 'DIR': 'person', 'ADM': 'internal'}
# What a mandate needs before its file counts as complete: each entry is one need, met by any of its types.
REQUIRED = {
    'company': (('incorporation',), ('register_extract', 'articles'), ('kyc_compliance',), ('passport_id',)),
    'person': (('passport_id',), ('proof_of_address',), ('kyc_compliance',)),
}
KYC_FOLDER = re.compile(r'kyc|compliance|due diligence|\bcdd\b|gwg', re.I)
COVERAGE_FILE = 'Archive_KYC_coverage.xlsx'


def client_key(value):
    """One key per mandate whatever the holder or the zero padding: KP-00031-ENT and KP-31-ENT are one."""
    return re.sub(r'(?<!\d)0+(?=\d)', '', re.sub(r'\s+', '', str(value or '')).upper())


def mandate_type(key):
    """Service type from the extension of the mandate number (ENT, ACC, CON, DIR, ADM); None when unknown."""
    found = re.search(r'-([A-Z]+)$', client_key(key))
    return MANDATE_TYPES.get(found.group(1)) if found else None


def document_coverage(rows):
    """Per mandate, over every holder it is filed under: what it has, what it misses, what another holder fills.

    Returns (coverage, gaps): coverage lists every mandate; gaps only companies and persons with a need unmet in
    all holders together. A need met in one holder's folder but not in another's is 'covered_elsewhere'."""
    per = {}
    for r in rows:
        key = client_key(r.get('key')) or '?'
        c = per.setdefault(key, {'key': r.get('key') or '?', 'holders': {}, 'files': 0})
        c['files'] += 1
        types = c['holders'].setdefault(r.get('holder') or '', set())
        types.add(r['doc_type'])
        if KYC_FOLDER.search(r.get('folder') or ''):          # a KYC folder is the mandate's KYC file
            types.add('kyc_compliance')
    coverage, gaps = [], []
    for key in sorted(per):
        c = per[key]
        kind = mandate_type(key)
        found = set().union(*c['holders'].values())
        entry = {'key': c['key'], 'type': kind or 'unknown', 'extension': key.rsplit('-', 1)[-1] if '-' in key
                 else '', 'holders': sorted(c['holders']), 'files': c['files'], 'missing': [],
                 'covered_elsewhere': []}
        for need in REQUIRED.get(kind, ()):
            label = ' or '.join(need)
            if not found.intersection(need):
                entry['missing'].append(label)
                continue
            have = sorted(h for h, t in c['holders'].items() if t.intersection(need))
            lacking = sorted(h for h, t in c['holders'].items() if not t.intersection(need))
            if lacking:
                entry['covered_elsewhere'].append({'need': label, 'missing_in': lacking, 'found_in': have})
        entry['status'] = ('internal' if kind == 'internal' else 'type unknown' if kind is None
                           else 'gaps' if entry['missing'] else 'complete')
        coverage.append(entry)
        if entry['missing']:
            gaps.append({k: entry[k] for k in ('key', 'type', 'holders', 'missing', 'covered_elsewhere')})
    return coverage, gaps


def write_coverage_workbook(path, coverage, rows):
    """Archive_KYC_coverage.xlsx: one row per mandate, where each need was found, and a sheet per holder."""
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Mandates'
    ws.append(['Mandate', 'Type', 'Status', 'Holders', 'Files', 'Missing in every holder',
               'Missing in one holder, found in another'])
    for c in coverage:
        ws.append([c['key'], c['type'], c['status'], ', '.join(h or '(exports)' for h in c['holders']), c['files'],
                   ', '.join(c['missing']), '; '.join(f"{x['need']}: in {', '.join(x['found_in'])}, not in "
                                                      f"{', '.join(x['missing_in'])}" for x in c['covered_elsewhere'])])
    wf = wb.create_sheet('Where found')
    wf.append(['Mandate', 'Holder', 'Document type', 'Folder', 'File', 'Confidence'])
    for r in sorted(rows, key=lambda r: (client_key(r.get('key')), r.get('holder') or '', r['doc_type'])):
        wf.append([r.get('key'), r.get('holder'), r['doc_type'], r.get('folder'), r.get('file'), r['confidence']])
    wh = wb.create_sheet('Per holder')
    wh.append(['Holder', 'Mandates', 'Complete', 'With gaps', 'Internal (ADM)', 'Type unknown',
               'Shared with another holder'])
    holders = sorted({h for c in coverage for h in c['holders']})
    for h in holders:
        mine = [c for c in coverage if h in c['holders']]
        wh.append([h or '(exports)', len(mine)] + [sum(1 for c in mine if c['status'] == s)
                                                   for s in ('complete', 'gaps', 'internal', 'type unknown')]
                  + [sum(1 for c in mine if len(c['holders']) > 1)])
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = 'A2'
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(60, max(10, *(len(str(x.value or ''))
                                                                                    for x in col[:200])) + 2)
    wb.save(path)


def summarize(run_dir, ai=None, config=None):
    """Short report of a run for people (n8n mail / chat). Local model only: it sees client keys and messages."""
    run_dir = Path(run_dir)
    ai = ai or AI(load_settings(config))
    events, summary = read_run(run_dir)
    worst = [{'level': e['level'], 'code': e['code'], 'message': e['message'][:200]} for e in events
             if e.get('level') in ('error', 'review')][:60]
    by_code = {}
    for e in events:
        if e.get('level') in ('error', 'review', 'warning'):
            by_code[e['code']] = by_code.get(e['code'], 0) + 1
    docs = summary.get('documents') or []
    archive_only = not (summary.get('clients') or []) and bool(docs)
    payload = {'totals': summary.get('totals'), 'levels': summary.get('levels'), 'by_code': by_code,
               'examples': worst, 'archive_only': archive_only,
               'document_folders': len({d.get('key') for d in docs})}
    try:                                       # what the document check found, when it ran before
        found = json.loads((run_dir / SUGGESTIONS_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        found = {}
    if found.get('documents'):
        types = {}
        for d in found['documents']:
            types[d['doc_type']] = types.get(d['doc_type'], 0) + 1
        gaps = found.get('document_gaps') or []
        payload['document_types'] = dict(sorted(types.items(), key=lambda kv: -kv[1]))
        cov = found.get('document_coverage') or []
        payload['mandates_by_status'] = {s: sum(1 for c in cov if c['status'] == s)
                                         for s in ('complete', 'gaps', 'internal', 'type unknown')}
        payload['mandates_under_several_holders'] = sum(1 for c in cov if len(c['holders']) > 1)
        payload['gaps_filled_by_another_holder'] = sum(len(c['covered_elsewhere']) for c in cov)
        payload['mandates_missing_documents'] = len(gaps)
    if archive_only:
        payload['note'] = ("Archive-only run: only the file list of the document archive was given, on purpose. "
                           "NO_MANDATES is expected; it is not a failure. Report what the archive shows (document types, KYC coverage); there are no rows to review.")
    system = ("You write the status report of one run of a data migration (M2 Mandat -> CSP Odoo). Audience: the "
              "operations team. JSON only: {\"headline\": one sentence, \"status\": \"green\"|\"amber\"|\"red\", "
              "\"actions\": [up to 6 short imperative actions, most blocking first], \"notes\": short paragraph}. "
              "Red when there are errors, amber when rows are in review, green otherwise; for an archive-only "
              "run: amber, and the actions are about adding the mandate export and checking KYC documents.")
    answer = ai.ask('summary', system, payload, provider='ollama', max_tokens=1500)
    if not isinstance(answer, dict):
        lv = summary.get('levels') or {}
        answer = {'headline': f"{(summary.get('totals') or {}).get('import', 0)} rows to import, "
                              f"{(summary.get('totals') or {}).get('review', 0)} to review.",
                  'status': 'red' if lv.get('error') else 'amber' if lv.get('review') else 'green',
                  'actions': [f"{c}: {n}" for c, n in sorted(by_code.items(), key=lambda x: -x[1])[:6]],
                  'notes': 'Written without the model (it did not answer).'}
    answer['actions'] = list(answer.get('actions') or [])[:6]
    answer['totals'] = summary.get('totals')
    return save(run_dir, ai, summary=answer)


def save(run_dir, ai, **parts):
    path = Path(run_dir) / SUGGESTIONS_FILE
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    data.update(parts)
    data['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    data['settings'] = {k: ai.s[k] for k in ('provider', 'ollama_model', 'claude_model', 'allow_cloud',
                                              'escalate_below')}
    data['calls'] = (data.get('calls') or []) + ai.calls
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    ai.calls = []
    return data


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('command', choices=['status', 'suggest', 'documents', 'summary', 'all'])
    p.add_argument('run', nargs='?', help='run folder (runs/<stamp>)')
    p.add_argument('--config', default=str(DEFAULT_CONFIG))
    a = p.parse_args(argv)
    config = json.loads(Path(a.config).read_text(encoding='utf-8')) if Path(a.config).is_file() else {}
    ai = AI(load_settings(config), log=lambda m: print('!', m, file=sys.stderr))
    if a.command == 'status':
        print(json.dumps(ai.status(), indent=1))
        return 0
    if not a.run or not Path(a.run).is_dir():
        p.error('give the run folder')
    result = {}
    if a.command in ('suggest', 'all'):
        result = suggest(a.run, ai, config)
    if a.command in ('documents', 'all'):
        result = classify_documents(a.run, ai, config)
    if a.command in ('summary', 'all'):
        result = summarize(a.run, ai, config)
    print(json.dumps({k: result.get(k) for k in ('suggestions', 'summary', 'document_gaps') if k in result},
                     indent=1, ensure_ascii=False, default=str)[:6000])
    return 0


if __name__ == '__main__':
    sys.exit(main())
