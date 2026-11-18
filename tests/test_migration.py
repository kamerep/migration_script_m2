"""Tests of the M2 -> CSP migration on the fictitious sample exports.

    .venv/bin/python -m unittest discover -s tests -v
"""
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import migration_script_m2_odoo as m2  # noqa: E402

CONFIG = {'company_map': {'IT': 'Interis AG', 'HK': 'EDG Hong Kong Ltd'},
          'employee_map': {'PK': 'Peter Kamere', 'AB': 'Anna Brunner', 'MW': 'Mei Wong'}}


class TestConvert(unittest.TestCase):

    def setUp(self):
        self.c = m2.Convert({})

    def test_dates(self):
        self.assertEqual(self.c.date('12.05.2011')[0], datetime.date(2011, 5, 12))
        self.assertEqual(self.c.date('2019-06-21')[0], datetime.date(2019, 6, 21))
        value, note = self.c.date('06/21/2019')
        self.assertEqual(value, datetime.date(2019, 6, 21))
        self.assertIn('swapped', note)
        self.assertIsNone(self.c.date('2022-02-30')[0])
        self.assertEqual(self.c.date(43831)[0], datetime.date(2020, 1, 1))
        self.assertIsNone(self.c.date('n/a')[0])

    def test_numbers(self):
        self.assertEqual(self.c.number("CHF 100'000.00")[:2], (100000.0, 'CHF'))
        self.assertEqual(self.c.number('1.250,50')[0], 1250.5)
        self.assertEqual(self.c.number('10,000.00 HKD')[:2], (10000.0, 'HKD'))
        self.assertIsNone(self.c.number('n/a')[0])

    def test_country_email_signing(self):
        self.assertEqual(self.c.country('Schweiz')[0], 'CH')
        self.assertEqual(self.c.country('VAE')[0], 'AE')
        self.assertEqual(self.c.country('British Virgin Islands')[0], 'VG')
        self.assertIsNone(self.c.country('Atlantis')[0])
        self.assertEqual(self.c.email('Hans <h@x.ch>')[0], 'h@x.ch')
        self.assertEqual(self.c.signing('Kollektivunterschrift zu zweien')[0], 'jointly')
        self.assertEqual(self.c.signing('Einzelunterschrift')[0], 'singly')


class TestSampleRun(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src, out = Path(cls.tmp.name) / 'src', Path(cls.tmp.name) / 'out'
        subprocess.run([sys.executable, str(HERE / 'make_sample_m2.py'), str(src)], check=True, capture_output=True)
        cls.streamed = []
        cls.job = m2.Migration(src, out, CONFIG, listener=cls.streamed.append, journal_path=out / 'journal.jsonl')
        cls.summary = cls.job.run()
        cls.out = out
        cls.codes = [e['code'] for e in cls.job.journal.events]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def events(self, code):
        return [e for e in self.job.journal.events if e['code'] == code]

    def test_outputs_and_journal(self):
        for name in m2.IMPORT_FILE, m2.REVIEW_FILE, 'journal.jsonl', 'summary.json':
            self.assertTrue((self.out / name).is_file(), name)
        lines = (self.out / 'journal.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), len(self.job.journal.events))
        self.assertEqual(len(self.streamed), len(lines), "every event reaches the listener (the UI)")
        json.loads((self.out / 'summary.json').read_text(encoding='utf-8'))

    def test_import_file_matches_template(self):
        book = openpyxl.load_workbook(self.out / m2.IMPORT_FILE, read_only=True)
        self.assertNotIn('Bank Access', book.sheetnames)  # credentials sheet: only written when M2 has data
        for sheet in [s for s in m2.import_spec.SHEETS if s['key'] != 'bank_access']:
            header = [c.value for c in next(book[sheet['title']].iter_rows(max_row=1))]
            self.assertEqual(header, [c[1] for c in sheet['columns']])

    def test_files_classified(self):
        kinds = {t['file']: t['kind'] for t in self.job.files}
        self.assertEqual(kinds['Adressen.xlsx'], 'addresses')
        self.assertEqual(kinds['Mandate.xlsx'], 'mandates')
        self.assertEqual(kinds['Organe.xlsx'], 'officers')
        self.assertEqual(kinds['Wirtschaftlich Berechtigte.csv'], 'ubos')
        self.assertEqual(kinds['Offene Posten.csv'], 'open_items')
        self.assertTrue(any(e['file'] == 'Telefonliste Mitarbeiter.xlsx' for e in self.events('KIND_UNKNOWN')))
        self.assertIn('HEADER_ROW', self.codes)
        self.assertIn('ROW_TOTAL', self.codes)

    def test_never_default_company(self):
        refs = {r['m2_reference']: r for r in self.job.rows['clients']}
        self.assertEqual(refs['M-0001']['company'], 'Interis AG')
        review = {r['key']: r for r in self.job.review['clients']}
        self.assertIn('M-0004', review)  # holder XY is not mapped
        self.assertIn('M-0007', review)
        self.assertNotIn('M-0004', refs)

    def test_client_values(self):
        refs = {r['m2_reference']: r for r in self.job.rows['clients']}
        self.assertEqual(refs['M-0002']['type'], 'Individual')
        self.assertEqual(refs['M-0002']['name'], 'Hans Muster')
        self.assertEqual(refs['M-0005']['entity_type'], 'foundation')
        self.assertEqual(refs['M-0001']['advisor'], 'Peter Kamere')
        self.assertEqual((refs['M-0001']['share_capital'], refs['M-0001']['share_capital_currency']),
                         (100000.0, 'CHF'))
        self.assertIn('Jurisdiction: Zug', refs['M-0001']['notes'])
        self.assertNotIn('M-0006', refs)  # closed mandate
        self.assertIn('MANDATE_CLOSED', self.codes)
        self.assertIn('DUPLICATE_REF', self.codes)

    def test_registers(self):
        directors = {(r['client'], r['name']) for r in self.job.rows['directors']}
        self.assertIn(('M-0001', 'Anna Keller'), directors)
        self.assertEqual(len([d for d in directors if d == ('M-0001', 'Anna Keller')]), 1)  # periods merged
        self.assertEqual([r['client'] for r in self.job.rows['secretaries']], ['M-0003'])
        ubo = next(r for r in self.job.rows['ubos'] if r['client'] == 'M-0001' and r['name'] == 'Hans Muster')
        self.assertEqual(ubo['ownership'], 60.0)
        reasons = ' '.join(r['reason'] for r in self.job.review['directors'])
        self.assertIn('end date is before', reasons)
        self.assertIn('M-0099', reasons)
        self.assertIn('Kassierin', reasons)
        self.assertIn('IBAN_CHECK', self.codes)

    def test_trail_and_receivables(self):
        trail = self.summary['records']['M-0001']
        self.assertGreater(len(trail), 20)
        self.assertEqual(self.summary['receivables']['Interis AG']['CHF'], 5000.0)
        self.assertEqual(len(self.summary['fees']), 3)


class TestKeys(unittest.TestCase):

    def test_zero_padding(self):
        self.assertEqual(m2.rkey('AP-00482-ACC'), m2.rkey('AP-482-ACC'))
        self.assertEqual(m2.rkey('021091'), m2.rkey(21091))
        self.assertNotEqual(m2.rkey('AP-482-ACC'), m2.rkey('AP-4820-ACC'))
        self.assertEqual(m2.MANDATE_RE.search('KP-482-ACC Detail addr')[1], 'KP-482-ACC')


class TestEnglishRun(unittest.TestCase):
    """M2 Professional with the English interface: the grid titles of Infosystem > Mandate info."""

    CONFIG = {'company_map': {'NOVA-PH': 'Nova Services Inc'},
              'employee_map': {'FF': 'Fiona Fischer', 'JD': 'Jon Dale'},
              'officer_types': {'DIR': 'directors', 'BO': 'ubos', 'CP': 'contacts'}}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src, out = Path(cls.tmp.name) / 'src', Path(cls.tmp.name) / 'out'
        subprocess.run([sys.executable, str(HERE / 'make_sample_m2.py'), '--english', str(src)], check=True,
                       capture_output=True)
        cls.job = m2.Migration(src, out, cls.CONFIG, journal_path=out / 'journal.jsonl')
        cls.summary = cls.job.run()
        cls.out = out
        cls.codes = [e['code'] for e in cls.job.journal.events]
        cls.clients = {r['m2_reference']: r for r in cls.job.rows['clients']}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_files_classified(self):
        kinds = {t['file']: t['kind'] for t in self.job.files}
        self.assertEqual(kinds['Mandate overview.xlsx'], 'mandates')
        self.assertEqual(kinds['Addresses.xlsx'], 'addresses')
        self.assertEqual(kinds['KP-482-ACC Detail addr.xlsx'], 'officers')
        self.assertEqual(kinds['KP-482-ACC Banks.xlsx'], 'bank_accounts')
        self.assertEqual(kinds['KP-482-ACC Authorized to sign.xlsx'], 'signatories')
        self.assertEqual(kinds['KP-482-ACC Fees.xlsx'], 'fees')
        self.assertEqual(kinds['Last invoices.xlsx'], 'open_items')
        self.assertEqual(kinds['Compliance.xlsx'], 'compliance')
        self.assertEqual(kinds['Documents.xlsx'], 'documents')

    def test_mandate_columns(self):
        c = self.clients['KP-482-ACC']
        self.assertEqual(c['name'], 'Coral Reef Holdings Ltd.')            # Mandate desig. 1
        self.assertEqual(c['company'], 'Nova Services Inc')                # Mad.Company
        self.assertEqual(c['advisor'], 'Fiona Fischer')                     # CDB
        self.assertEqual(self.clients['KP-501-ACC']['deputy'], 'Jon Dale')  # SSB
        self.assertEqual(c['contract_start'], datetime.date(2020, 6, 18))  # Mandate start
        self.assertEqual(c['entity_type'], 'company')                       # Mandate form
        self.assertNotIn('KP-502-TRU', self.clients)                        # Active = N
        self.assertIn('KP-610-ACC', {r['key'] for r in self.job.review['clients']})

    def test_address_columns(self):
        c = self.clients['KP-482-ACC']  # address found through Debtor = address Number
        self.assertEqual((c['zip'], c['city'], c['country']), ('96960', 'Ajeltake Island, Majuro', 'MH'))
        self.assertEqual(c['street2'], 'Lagoon Road')
        self.assertEqual(c['email'], 'office@coralreef.example')
        self.assertEqual(c['incorporation_date'], datetime.date(2020, 6, 12))  # Founded
        self.assertEqual(c['activity'], 'Holding of investments')              # Business activity
        self.assertEqual(c['notes'].count('Alpha'), 1)
        self.assertEqual(self.clients['KP-501-ACC']['city'], 'Belize City')

    def test_registers_from_mandate_grids(self):
        self.assertIn('REF_FROM_FILE', self.codes)
        directors = {r['name']: r for r in self.job.rows['directors']}
        self.assertEqual(directors['Grace Lim']['client'], 'KP-482-ACC')
        self.assertEqual(directors['Grace Lim']['signing_authority'], 'singly')     # collective N
        self.assertEqual(directors['Daniel Okafor']['signing_authority'], 'jointly')  # collective J
        self.assertIn('Resident in Lagos', directors['Daniel Okafor']['remark'])    # Remark2
        self.assertEqual([r['name'] for r in self.job.rows['ubos']], ['Grace Lim'])
        contact = self.job.rows['contacts'][0]
        self.assertEqual((contact['name'], contact['function']), ('Ana Ramos', 'Bookkeeping contact'))
        self.assertIn("'XQ'", ' '.join(r['reason'] for r in self.job.review['directors']))
        self.assertEqual(len(self.job.rows['signatories']), 2)

    def test_bank_relationship_and_currency(self):
        accounts = {r['acc_number']: r for r in self.job.rows['bank_accounts']}
        self.assertEqual(set(accounts), {'001-778812-01', '001-778812-02'})
        for acc in accounts.values():
            self.assertEqual((acc['bank_name'], acc['routing_number']), ('Pacific Coast Bank Ltd', '8842'))
        self.assertEqual(accounts['001-778812-01']['currency'], 'USD')  # from the account type
        self.assertEqual(accounts['001-778812-02']['currency'], 'EUR')  # from the remark

    def test_fees_and_invoices(self):
        self.assertEqual(self.clients['KP-482-ACC']['lei_renewal'], datetime.date(2027, 4, 18))
        fees = {f['fee']: f for f in self.job.fee_rows}
        self.assertEqual(fees['Flat Fee']['status'], 'ended')
        self.assertEqual(fees['Annual Basic Fee']['billed_by'], 'Nova Services Inc')
        self.assertEqual(self.summary['receivables'], {'Nova Services Inc': {'USD': 1750.0}})
        self.assertEqual(self.codes.count('OPEN_ITEM_PAID'), 2)

    def test_compliance_and_documents(self):
        self.assertIn('Risk category: 2 Average risk', self.clients['KP-482-ACC']['notes'])
        self.assertIn('COMPLIANCE_LOCK', self.codes)
        docs = {d['file']: d for d in self.job.documents}
        self.assertEqual(docs['LEI certificate.pdf']['on_disk'], 'KP-00482-ACC/LEI/LEI certificate.pdf')
        self.assertEqual(docs['LEI application.pdf']['key'], 'KP-482-ACC')  # folder only, zero-padded name
        self.assertIn('FOLDER_NOT_CLIENT', self.codes)
        book = openpyxl.load_workbook(self.out / m2.REVIEW_FILE, read_only=True)
        self.assertTrue({'Compliance', 'Documents'} <= set(book.sheetnames))

    def test_archive_inventory(self):
        kinds = {t['file']: t['kind'] for t in self.job.files}
        self.assertEqual(kinds['Archive inventory.csv'], 'inventory')
        docs = {d['file']: d for d in self.job.documents}
        passport = docs['Passport A Example.pdf']
        self.assertEqual((passport['key'], passport['folder']), ('KP-501-ACC', 'KYC'))  # zero padding ignored
        self.assertEqual(passport['where'], 'archive NOVA-PH')
        self.assertTrue(passport['on_disk'].startswith('archive: NOVA-PH'))
        self.assertEqual(docs['Trust deed.pdf']['key'], 'KP-502-TRU')
        self.assertEqual(docs['Register extract.pdf']['client_state'], 'not in the exports')
        self.assertNotIn('Thumbs.db', docs)
        for code in ('ARCHIVE_FOLDER', 'ARCHIVE_FOLDER_UNKNOWN', 'ARCHIVE_NOT_MANDATE', 'ARCHIVE_NO_FOLDER'):
            self.assertIn(code, self.codes)


if __name__ == '__main__':
    unittest.main()
