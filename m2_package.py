"""Document packages of a run: the M2 files to upload into the clients' Documents folders in Odoo.

The tool still writes files only. A package is a zip of at most `max_mb` MB with a manifest.csv; the Odoo module
csp_document_import reads it (Documents > Configuration > M2 Document Packages) and files each document in the
client's workspace folder. Every file goes: emails (.msg, .eml) to Correspondence, with their attachments also taken
out and filed like any other document of the client (signature images and attachments already packed are skipped),
program files (QuickBooks) to Financial Accounting, a file whose place is not known to M2 Archive (to sort).

    .venv/bin/python m2_package.py runs/<id> --archive-root /mnt/z [--max-mb 90]

`archive_root` is where the archive drive is readable on this machine (a mounted Z: share or a copy of it): the
inventory path Z:\\NOVA-PH\\KP-00031-ENT\\KYC\\pass.pdf is read as <archive_root>/NOVA-PH/KP-00031-ENT/KYC/pass.pdf.
A stopped run resumes: packages already written are kept.
"""
import argparse
import csv
import email
import email.policy
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

PACKAGES = 'packages'
STATE_FILE = 'packages.json'
MANIFEST = 'manifest.csv'
COLUMNS = ['client', 'folder', 'suggested_type', 'member', 'name', 'm2_ref', 'm2_path', 'modified', 'sha1', 'size']


class Error(Exception):
    """A message for the person."""


EMAIL = re.compile(r'\.(msg|eml)$', re.I)
IMAGE = re.compile(r'\.(png|jpe?g|gif|bmp|tiff?|emz|wmz)$', re.I)
INLINE_MAX = 30 * 1024        # a small image in an email is a logo or a signature, not a document


def _msg_text(ole, folder, tag):
    """A string property of an Outlook .msg storage (Unicode 001F or 8-bit 001E), or ''."""
    for kind, codec in (('001F', 'utf-16-le'), ('001E', 'cp1252')):
        path = f"{folder}__substg1.0_{tag}{kind}"
        if ole.exists(path):
            return ole.openstream(path).read().decode(codec, 'replace').rstrip('\x00').strip()
    return ''


def email_attachments(name, data):
    """(attachments, embedded) of an email file: attachments = [(file name, bytes, content id)], embedded = how many
    attached emails (Outlook items) there are, which are not opened. Raises ValueError when it cannot be read."""
    if name.lower().endswith('.eml'):
        message = email.message_from_bytes(data, policy=email.policy.default)
        out, embedded = [], 0
        for part in message.iter_attachments():
            if part.get_content_type() == 'message/rfc822':
                embedded += 1
                continue
            payload = part.get_payload(decode=True)
            if payload:
                out.append((part.get_filename() or '', payload, part.get('Content-ID') or ''))
        return out, embedded
    try:
        import olefile
    except ImportError as exc:
        raise ValueError("the olefile package is missing (.venv/bin/pip install olefile)") from exc
    if not olefile.isOleFile(data):
        raise ValueError("not an Outlook .msg file")
    out, embedded = [], 0
    with olefile.OleFileIO(data) as ole:
        tops = sorted({p[0] for p in ole.listdir(streams=True, storages=True)
                       if p and p[0].startswith('__attach_version1.0_#')})
        for top in tops:
            folder = top + '/'
            if ole.exists(folder + '__substg1.0_3701000D'):
                embedded += 1
                continue
            if not ole.exists(folder + '__substg1.0_37010102'):
                continue
            payload = ole.openstream(folder + '__substg1.0_37010102').read()
            file = (_msg_text(ole, folder, '3707') or _msg_text(ole, folder, '3704')
                    or _msg_text(ole, folder, '3001'))
            out.append((file, payload, _msg_text(ole, folder, '3712')))
    return out, embedded


def safe_name(name, n):
    """A file name of an attachment, without a path, never empty."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', (name or '').strip()).strip(' .')
    return name[:150] or f"attachment_{n}"


def poa_ref(key, folder, file):
    """The M2 Reference of a power of attorney found in the documents (as the migration writes it)."""
    return f"archive:{key}/{folder + '/' if folder else ''}{file}"


def source_path(doc, archive_root, source):
    """Where the file of a document row is readable here, or None."""
    on_disk = doc.get('on_disk') or ''
    if on_disk.startswith('archive: '):
        if not archive_root:
            return None
        parts = [p for p in re.split(r'[\\/]+', on_disk[len('archive: '):]) if p]
        return Path(archive_root).joinpath(*parts)
    return Path(source) / on_disk if on_disk and source else None


def rows_to_upload(run_dir):
    """Document rows of the run that go to Odoo: classified for upload, of a client that is imported."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
    ai_file = run_dir / 'ai_suggestions.json'
    typed = json.loads(ai_file.read_text(encoding='utf-8')).get('documents') if ai_file.is_file() else None
    if not typed:
        raise Error("Classify the documents first (AI assistant > Classify documents).")
    docs = summary.get('documents') or []
    if len(typed) != len(docs) or any('upload' not in t for t in typed[:1]):
        raise Error("The document types are of an earlier version of this run or of the tool: classify the "
                    "documents again (AI assistant > Classify documents).")
    out = []
    for doc, t in zip(docs, typed):
        is_email = bool(EMAIL.search(doc.get('file') or ''))
        if not t.get('upload') or doc.get('client_state') != 'loaded':
            continue
        out.append({**doc, 'odoo_type': t.get('odoo_type'), 'odoo_folder': t.get('odoo_folder'), 'email': is_email})
    out.sort(key=lambda d: (d['key'], str(d.get('created') or ''), d.get('folder') or '', d['file']))
    return out, summary.get('source') or ''


def build(run_dir, archive_root=None, max_mb=90, progress=None, stop=None):
    """Write the packages of a run; returns the totals. progress(percent, message) is told as it goes."""
    run_dir = Path(run_dir)
    rows, source = rows_to_upload(run_dir)
    if not archive_root and any((d.get('on_disk') or '').startswith('archive: ') for d in rows):
        raise Error("Give the archive folder: where the archive drive (Z:) is mounted or copied on this machine.")
    folder = run_dir / PACKAGES
    folder.mkdir(exist_ok=True)
    state_path = folder / STATE_FILE
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
    if state.get('archive_root') not in (None, str(archive_root or '')):
        state = {}                                  # another archive: start again
    done = set(state.get('packed') or [])
    state.update(archive_root=str(archive_root or ''), max_mb=max_mb, packages=state.get('packages') or [],
                 missing=state.get('missing') or [], packed=sorted(done))
    mail = state.setdefault('emails', {'read': 0, 'attachments': 0, 'signature_images': 0, 'duplicates': 0,
                                       'attached_emails': 0, 'unreadable': 0})
    seen = set(state.get('hashes') or [])          # client:sha1 of what is packed: an attachment is filed once
    rules = None
    limit = max_mb * 1024 * 1024
    book = buffer = writer = None
    size = 0

    def close():
        nonlocal book, buffer, writer, size
        if not book:
            return
        book.writestr(MANIFEST, buffer.getvalue())
        book.close()
        state['packed'] = sorted(done)
        state['hashes'] = sorted(seen)
        state_path.write_text(json.dumps(state, indent=1), encoding='utf-8')
        book = buffer = writer = None
        size = 0

    def add(doc, name, data, odoo_folder, odoo_type, ref, m2_path):
        nonlocal book, buffer, writer, size
        if book and size + len(data) > limit:
            close()
        if not book:
            file = f"M2_Documents_{len(state['packages']) + 1:04d}.zip"
            book = zipfile.ZipFile(folder / file, 'w', zipfile.ZIP_DEFLATED)
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, COLUMNS)
            writer.writeheader()
            state['packages'].append({'file': file, 'documents': 0, 'bytes': 0})
        sha1 = hashlib.sha1(data).hexdigest()
        member = f"files/{state['packages'][-1]['documents'] + 1:06d}_{name}"
        book.writestr(member, data)
        writer.writerow({
            'client': doc['key'], 'folder': odoo_folder or '', 'suggested_type': odoo_type or '', 'member': member,
            'name': name, 'm2_ref': ref, 'm2_path': m2_path, 'modified': doc.get('created') or '', 'sha1': sha1,
            'size': len(data)})
        state['packages'][-1]['documents'] += 1
        state['packages'][-1]['bytes'] += len(data)
        size += len(data)
        seen.add(f"{doc['key']}:{sha1}")

    total = len(rows)
    for n, doc in enumerate(rows, 1):
        if stop and stop():
            close()
            state['stopped'] = True
            state_path.write_text(json.dumps(state, indent=1), encoding='utf-8')
            return totals(state, total)
        ref = poa_ref(doc['key'], doc.get('folder') or '', doc['file'])
        if ref in done:
            continue
        path = source_path(doc, archive_root, source)
        if not path or not path.is_file():
            state['missing'].append({'client': doc['key'], 'm2_path': doc.get('on_disk'),
                                     'why': 'no archive root set' if path is None else 'not found'})
            done.add(ref)
            continue
        data = path.read_bytes()
        if not doc.get('email'):
            add(doc, doc['file'], data, doc.get('odoo_folder'), doc.get('odoo_type'), ref, doc.get('on_disk') or '')
            done.add(ref)
        else:
            if rules is None:
                import m2_ai
                rules = m2_ai.document_rules()
            try:
                attachments, embedded = email_attachments(doc['file'], data)
            except Exception as exc:  # noqa: BLE001 - a damaged email is listed, the run goes on
                mail['unreadable'] += 1
                state['missing'].append({'client': doc['key'], 'm2_path': doc.get('on_disk'),
                                         'why': f"email not readable: {exc}"[:200]})
                done.add(ref)
                continue
            mail['read'] += 1
            mail['attached_emails'] += embedded
            add(doc, doc['file'], data, doc.get('odoo_folder'), doc.get('odoo_type'), ref, doc.get('on_disk') or '')
            for i, (raw_name, payload, cid) in enumerate(attachments, 1):
                name = safe_name(raw_name, i)
                if IMAGE.search(name) and (cid or len(payload) < INLINE_MAX):
                    mail['signature_images'] += 1
                    continue
                if EMAIL.search(name):
                    mail['attached_emails'] += 1
                    continue
                hit = next((k for k, rx in m2_ai.DOC_RULES if rx.search(name)), None) or 'other'
                if m2_ai.SYSTEM_FILE.search(name):
                    hit = 'system_file'
                odoo_type, odoo_folder, upload, _how = m2_ai.odoo_target(
                    {'key': doc['key'], 'folder': doc.get('folder') or '', 'file': name, 'doc_type': hit}, rules)
                if not upload:
                    continue
                if not odoo_type and not odoo_folder:
                    odoo_folder = doc.get('odoo_folder')
                if f"{doc['key']}:{hashlib.sha1(payload).hexdigest()}" in seen:
                    mail['duplicates'] += 1
                    continue
                add(doc, name, payload, odoo_folder, odoo_type, f"{ref}#{i}",
                    f"{doc.get('on_disk') or ''} > {name}")
                mail['attachments'] += 1
            done.add(ref)
        if progress and n % 50 == 0:
            progress(round(n * 100 / total), f"{n} of {total} documents")
    close()
    state['stopped'] = False
    state_path.write_text(json.dumps(state, indent=1), encoding='utf-8')
    if progress:
        progress(100, f"{total} documents")
    return totals(state, total)


def totals(state, total):
    return {'documents': total, 'packages': len(state['packages']),
            'packed': sum(p['documents'] for p in state['packages']),
            'bytes': sum(p['bytes'] for p in state['packages']), 'missing': len(state['missing']),
            'stopped': bool(state.get('stopped')), 'folder': PACKAGES, 'emails': state.get('emails') or {}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('run', help="Folder of the run (runs/<id>)")
    parser.add_argument('--archive-root', help="Where the archive drive (Z:) is readable here")
    parser.add_argument('--max-mb', type=int, default=90, help="Largest package size (Odoo uploads up to 128 MB)")
    args = parser.parse_args(argv)
    try:
        result = build(args.run, args.archive_root, args.max_mb,
                       progress=lambda pct, msg: print(f"{pct:3d}% {msg}", file=sys.stderr))
    except Error as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
