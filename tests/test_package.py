"""Document packages of a run: the files to upload in Odoo, the attachments of the emails, resumable."""
import csv
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import m2_package  # noqa: E402


def cfb(tree):
    """A minimal compound file (the format of an Outlook .msg): tree = {name: bytes | {sub-tree}}. Every stream
    is padded to 4 KB so it lives in the normal sectors (no mini stream); strings are read with NULs stripped."""
    free, end, fat_mark = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    entries, data, fat = [], bytearray(), []

    def stream(blob):
        blob = blob + b'\0' * (max(4096, -(-len(blob) // 512) * 512) - len(blob))
        start = len(fat)
        count = len(blob) // 512
        fat.extend(list(range(start + 1, start + count)) + [end])
        data.extend(blob)
        return start, len(blob)

    def add(name, kind, start=end, size=0):
        entries.append([name, kind, free, free, free, start, size])
        return len(entries) - 1

    def walk(node, parent):
        previous = None
        for name, value in node.items():
            if isinstance(value, dict):
                me = add(name, 1)
                walk(value, me)
            else:
                me = add(name, 2, *stream(value))
            if previous is None:
                entries[parent][4] = me                 # child
            else:
                entries[previous][3] = me               # right sibling
            previous = me
    add('Root Entry', 5)
    walk(tree, 0)
    directory = bytearray()
    for name, kind, left, right, child, start, size in entries:
        raw = name.encode('utf-16-le') + b'\0\0'
        directory += (raw.ljust(64, b'\0') + struct.pack('<HBBIII', len(raw), kind, 1, left, right, child)
                      + b'\0' * 36 + struct.pack('<III', start, size, 0))
    directory += b'\0' * (-len(directory) % 512)
    dir_start = len(fat)
    count = len(directory) // 512
    fat.extend(list(range(dir_start + 1, dir_start + count)) + [end])
    data.extend(directory)
    fat_sectors = -(-(len(fat) + 1) // 127)
    fat_start = len(fat)
    fat.extend([fat_mark] * fat_sectors)
    fat.extend([free] * (fat_sectors * 128 - len(fat)))
    difat = list(range(fat_start, fat_start + fat_sectors)) + [free] * (109 - fat_sectors)
    header = (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 16 + struct.pack('<HHHHH', 0x3E, 3, 0xFFFE, 9, 6)
              + b'\0' * 6 + struct.pack('<IIIIIIIII', 0, fat_sectors, dir_start, 0, 4096, end, 0, end, 0)
              + struct.pack('<109I', *difat))
    return bytes(header) + bytes(data) + struct.pack(f'<{len(fat)}I', *fat)


def outlook_msg():
    """An Outlook email with a passport scan, a signature logo and an attached email."""
    name = lambda text: text.encode('utf-16-le')
    return cfb({
        '__substg1.0_0037001F': name('Documents for the file'),
        '__attach_version1.0_#00000000': {'__substg1.0_37010102': b'%PDF passport',
                                          '__substg1.0_3707001F': name('Passport Example.pdf')},
        '__attach_version1.0_#00000001': {'__substg1.0_37010102': b'logo',
                                          '__substg1.0_3707001F': name('image001.png'),
                                          '__substg1.0_3712001F': name('image001.png@01D0')},
        '__attach_version1.0_#00000002': {'__substg1.0_3701000D': {'__substg1.0_0037001F': name('Fwd')},
                                          '__substg1.0_3707001F': name('Fwd.msg')},
    })


def eml():
    from email.message import EmailMessage
    message = EmailMessage()
    message['Subject'] = 'Invoice'
    message.set_content('See attached.')
    message.add_attachment(b'%PDF invoice' * 50, maintype='application', subtype='pdf',
                           filename='Invoice 2024-01.pdf')
    message.add_attachment(b'%PDF passport', maintype='application', subtype='pdf', filename='Passport copy.pdf')
    return message.as_bytes()


class TestPackages(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.run, self.archive = base / 'run', base / 'z'
        self.run.mkdir()
        files = [('KP-00031-ENT', 'Corporate documents', 'Minutes.pdf', b'm' * 600, 'CORPDOC', None, True),
                 ('KP-00031-ENT', 'Legal', 'Vollmacht.pdf', b'v' * 600, None, 'PWRATT', True),
                 ('KP-00031-ENT', 'Corresp. with client', 'Re.msg', outlook_msg(), 'CORRCL', None, False),
                 ('KP-00031-ENT', 'Corresp. with client', 'Invoice.eml', eml(), 'CORRCL', None, False),
                 ('KP-00031-ENT', 'QB', 'Orbit Ltd.QBW', b'q' * 700, 'FINACC', None, True),
                 ('KP-00007-CON', 'KYC', 'Gone.pdf', None, 'KYC', None, True)]                     # not on disk
        docs, typed = [], []
        for key, folder, name, data, odoo_folder, odoo_type, upload in files:
            if data is not None:
                path = self.archive / 'NOVA-PH' / key / folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            docs.append({'key': key, 'folder': folder, 'file': name, 'client_state': 'loaded', 'created': '2020-01-01',
                         'on_disk': f"archive: NOVA-PH\\{key}\\{folder}\\{name}"})
            typed.append({'key': key, 'upload': upload, 'odoo_folder': odoo_folder, 'odoo_type': odoo_type})
        (self.run / 'summary.json').write_text(json.dumps({'source': str(base), 'documents': docs}))
        (self.run / 'ai_suggestions.json').write_text(json.dumps({'documents': typed}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_packages_and_manifest(self):
        result = m2_package.build(self.run, self.archive, max_mb=1)
        self.assertEqual((result['packed'], result['missing'], result['packages']), (6, 1, 1))
        with zipfile.ZipFile(self.run / 'packages' / 'M2_Documents_0001.zip') as book:
            rows = list(csv.DictReader(io.TextIOWrapper(book.open('manifest.csv'), encoding='utf-8')))
            self.assertEqual([r['name'] for r in rows], ['Minutes.pdf', 'Invoice 2024-01.pdf', 'Passport copy.pdf',
                                                         'Passport Example.pdf', 'Vollmacht.pdf', 'Orbit Ltd.QBW'])
            by_name = {r['name']: r for r in rows}
            self.assertEqual(by_name['Orbit Ltd.QBW']['folder'], 'FINACC')
            self.assertEqual(by_name['Passport Example.pdf']['suggested_type'], 'PASSPORT')   # by its own name
            self.assertEqual(by_name['Invoice 2024-01.pdf']['folder'], 'CORRCL')            # the email's folder
            self.assertTrue(by_name['Passport Example.pdf']['m2_path'].endswith('Re.msg > Passport Example.pdf'))
            self.assertEqual(book.read(by_name['Passport Example.pdf']['member']).rstrip(b'\0'), b'%PDF passport')
            self.assertEqual(rows[0]['folder'], 'CORPDOC')
            self.assertEqual(by_name['Vollmacht.pdf']['suggested_type'], 'PWRATT')
            self.assertEqual(by_name['Vollmacht.pdf']['m2_ref'], 'archive:KP-00031-ENT/Legal/Vollmacht.pdf')
            self.assertEqual(book.read(rows[0]['member']), b'm' * 600)

    def test_resume_writes_nothing_twice(self):
        m2_package.build(self.run, self.archive, max_mb=1)
        again = m2_package.build(self.run, self.archive, max_mb=1)
        self.assertEqual((again['packages'], again['packed']), (1, 6))
        self.assertEqual(again['emails'], {'read': 2, 'attachments': 3, 'signature_images': 1, 'duplicates': 0,
                                           'attached_emails': 1, 'unreadable': 0})

    def test_needs_the_archive_folder(self):
        with self.assertRaises(m2_package.Error):
            m2_package.build(self.run, None)

    def test_needs_classified_documents(self):
        (self.run / 'ai_suggestions.json').unlink()
        with self.assertRaises(m2_package.Error):
            m2_package.build(self.run, self.archive)


if __name__ == '__main__':
    unittest.main()
