"""Document packages of a run: the files to upload in Odoo, all but emails, resumable."""
import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import m2_package  # noqa: E402


class TestPackages(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.run, self.archive = base / 'run', base / 'z'
        self.run.mkdir()
        files = [('KP-00031-ENT', 'Corporate documents', 'Minutes.pdf', b'm' * 600, 'CORPDOC', None, True),
                 ('KP-00031-ENT', 'Legal', 'Vollmacht.pdf', b'v' * 600, None, 'PWRATT', True),
                 ('KP-00031-ENT', 'Corresp. with client', 'Re.msg', b'e', 'CORRCL', None, False),   # email
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
        self.assertEqual((result['packed'], result['missing'], result['packages']), (2, 1, 1))
        with zipfile.ZipFile(self.run / 'packages' / 'M2_Documents_0001.zip') as book:
            rows = list(csv.DictReader(io.TextIOWrapper(book.open('manifest.csv'), encoding='utf-8')))
            self.assertEqual([r['name'] for r in rows], ['Minutes.pdf', 'Vollmacht.pdf'])
            self.assertEqual(rows[0]['folder'], 'CORPDOC')
            self.assertEqual(rows[1]['suggested_type'], 'PWRATT')
            self.assertEqual(rows[1]['m2_ref'], 'archive:KP-00031-ENT/Legal/Vollmacht.pdf')   # as the POA draft
            self.assertEqual(book.read(rows[0]['member']), b'm' * 600)

    def test_resume_writes_nothing_twice(self):
        m2_package.build(self.run, self.archive, max_mb=1)
        again = m2_package.build(self.run, self.archive, max_mb=1)
        self.assertEqual((again['packages'], again['packed']), (1, 2))

    def test_needs_the_archive_folder(self):
        with self.assertRaises(m2_package.Error):
            m2_package.build(self.run, None)

    def test_needs_classified_documents(self):
        (self.run / 'ai_suggestions.json').unlink()
        with self.assertRaises(m2_package.Error):
            m2_package.build(self.run, self.archive)


if __name__ == '__main__':
    unittest.main()
