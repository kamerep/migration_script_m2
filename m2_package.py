"""Document packages of a run: the M2 files to upload into the clients' Documents folders in Odoo.

The tool still writes files only. A package is a zip of at most `max_mb` MB with a manifest.csv; the Odoo module
csp_document_import reads it (Documents > Configuration > M2 Document Packages) and files each document in the
client's workspace folder. Emails (.msg, .eml) and program files stay in the archive.

    .venv/bin/python m2_package.py runs/<id> --archive-root /mnt/z [--max-mb 90]

`archive_root` is where the archive drive is readable on this machine (a mounted Z: share or a copy of it): the
inventory path Z:\\NOVA-PH\\KP-00031-ENT\\KYC\\pass.pdf is read as <archive_root>/NOVA-PH/KP-00031-ENT/KYC/pass.pdf.
A stopped run resumes: packages already written are kept.
"""
import argparse
import csv
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
        if not t.get('upload') or doc.get('client_state') != 'loaded':
            continue
        out.append({**doc, 'odoo_type': t.get('odoo_type'), 'odoo_folder': t.get('odoo_folder')})
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
        state_path.write_text(json.dumps(state, indent=1), encoding='utf-8')
        book = buffer = writer = None
        size = 0

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
        if book and size + len(data) > limit:
            close()
        if not book:
            name = f"M2_Documents_{len(state['packages']) + 1:04d}.zip"
            book = zipfile.ZipFile(folder / name, 'w', zipfile.ZIP_DEFLATED)
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, COLUMNS)
            writer.writeheader()
            state['packages'].append({'file': name, 'documents': 0, 'bytes': 0})
        member = f"files/{state['packages'][-1]['documents'] + 1:06d}_{doc['file']}"
        book.writestr(member, data)
        writer.writerow({
            'client': doc['key'], 'folder': doc.get('odoo_folder') or '', 'suggested_type': doc.get('odoo_type') or '',
            'member': member, 'name': doc['file'], 'm2_ref': ref, 'm2_path': doc.get('on_disk') or '',
            'modified': doc.get('created') or '', 'sha1': hashlib.sha1(data).hexdigest(), 'size': len(data)})
        state['packages'][-1]['documents'] += 1
        state['packages'][-1]['bytes'] += len(data)
        size += len(data)
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
            'stopped': bool(state.get('stopped')), 'folder': PACKAGES}


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
