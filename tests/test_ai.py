"""Tests of the AI layer that need no model: privacy gate, routing, answer parsing, guardrails.

    .venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import m2_ai  # noqa: E402


class FakeAI(m2_ai.AI):
    """Answers from a script instead of a model; records which provider each call would have used."""

    def __init__(self, answers, **settings):
        super().__init__({**m2_ai.DEFAULTS, **settings})
        self.answers = answers
        self.asked = []

    def ask(self, task, system, payload, provider=None, max_tokens=4000):
        provider = provider or self.route(task)
        if provider == 'claude' and task not in m2_ai.CLOUD_SAFE:
            raise PermissionError(task)
        self.asked.append((task, provider, payload))
        self.calls.append({'task': task, 'provider': provider, 'model': 'fake', 'ok': True})
        answer = self.answers.get((task, provider), self.answers.get(task))
        return answer(payload) if callable(answer) else answer

    def embed(self, texts):
        return None


class TestParsing(unittest.TestCase):

    def test_json_in_prose_and_fences(self):
        self.assertEqual(m2_ai.parse_json('Sure:\n```json\n[{"id": "a"}]\n```'), [{'id': 'a'}])
        self.assertEqual(m2_ai.parse_json('x {"a": 1} y'), {'a': 1})
        self.assertEqual(m2_ai.parse_json('Report: {"h": "x", "actions": ["a", "b"]}'), {'h': 'x', 'actions': ['a', 'b']})
        with self.assertRaises(ValueError):
            m2_ai.parse_json('no json here')


class TestPrivacy(unittest.TestCase):

    def test_documents_never_go_to_the_cloud(self):
        ai = m2_ai.AI({**m2_ai.DEFAULTS, 'provider': 'claude', 'allow_cloud': True})
        with self.assertRaises(PermissionError):
            ai.ask('documents', 'sys', {'items': []}, provider='claude')
        with self.assertRaises(PermissionError):
            ai.ask('summary', 'sys', {}, provider='claude')
        with self.assertRaises(PermissionError):
            ai.ask('columns', 'sys', {'items': []}, provider='claude')   # samples: local only

    def test_route_needs_switch_and_key(self):
        ai = m2_ai.AI({**m2_ai.DEFAULTS, 'provider': 'claude', 'allow_cloud': False})
        self.assertEqual(ai.route('officer_types'), 'ollama')
        ai = m2_ai.AI({**m2_ai.DEFAULTS, 'provider': 'claude', 'allow_cloud': True})
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}):
            self.assertEqual(ai.route('officer_types'), 'claude')
            self.assertEqual(ai.route('documents'), 'ollama')

    def test_escalation_sends_headers_without_samples(self):
        local = lambda p: [{'id': x['id'], 'value': 'email', 'confidence': 0.2} for x in p['items']]  # noqa: E731
        cloud = lambda p: [{'id': x['id'], 'value': 'email', 'confidence': 0.9} for x in p['items']]  # noqa: E731
        ai = FakeAI({('columns', 'ollama'): local, ('columns_cloud', 'claude'): cloud},
                    provider='auto', allow_cloud=True)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}):
            res = ai.classify('columns', 's', [{'id': 'c0', 'column': 'Mail', 'samples': ['a@b.ch']}],
                              {'email', 'none'}, cloud_task='columns_cloud',
                              cloud_items=[{'id': 'c0', 'column': 'Mail'}])
        self.assertEqual(res['c0']['provider'], 'claude')
        cloud_payload = [p for t, prov, p in ai.asked if prov == 'claude'][0]
        self.assertNotIn('samples', json.dumps(cloud_payload))


class TestGuardrails(unittest.TestCase):

    def test_values_must_be_allowed_and_local_confidence_capped(self):
        ai = FakeAI({'officer_types': [{'id': 'o0', 'value': 'directors', 'confidence': 1},
                                       {'id': 'o1', 'value': 'kings', 'confidence': 1}]})
        res = ai.classify('officer_types', 's', [{'id': 'o0'}, {'id': 'o1'}], set(m2_ai.REGISTERS))
        self.assertEqual(res['o0']['confidence'], 0.85)
        self.assertNotIn('o1', res)

    def test_progress_by_batch_within_a_part(self):
        ai = FakeAI({'officer_types': []}, batch=2)
        seen = []
        ai.on_progress = lambda frac, detail='': seen.append((round(frac, 3), detail))
        ai.part(1, 2)
        ai.classify('officer_types', 's', [{'id': f"o{i}"} for i in range(5)], set(m2_ai.REGISTERS))
        fracs = [f for f, _ in seen]
        self.assertEqual(fracs, sorted(fracs))
        self.assertEqual((fracs[0], fracs[-1]), (0.5, 1.0))
        self.assertIn('batch 3 of 3', seen[-2][1])

    def test_alias_samples_must_fit(self):
        base = {'confidence': 0.8, 'provider': 'ollama', 'reason': ''}
        self.assertIsNone(m2_ai.check_alias({**base, 'value': 'email'}, 'Remark', ['call back']))
        self.assertIsNotNone(m2_ai.check_alias({**base, 'value': 'email'}, 'Mail', ['a@b.ch']))
        self.assertLessEqual(m2_ai.check_alias({**base, 'value': 'name'}, 'Order', ['1', '2'])['confidence'], 0.3)
        self.assertLessEqual(m2_ai.check_alias({**base, 'value': 'mandate_no'}, 'Ref', ['KP-1'])['confidence'], 0.4)
        self.assertIsNone(m2_ai.check_alias({**base, 'value': 'mandate_no'}, 'MANDATS_BEGINN', ['2024-09-11']))
        self.assertIsNotNone(m2_ai.check_alias({**base, 'value': 'contract_start'}, 'BEGINN', ['2024-09-11']))
        # 'mandate_no' must not be read as a date field
        self.assertIsNotNone(m2_ai.check_alias({**base, 'value': 'mandate_no'}, 'Date', ['x']))


class TestDocuments(unittest.TestCase):

    def test_rules_then_model_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'summary.json').write_text(json.dumps({'documents': [
                {'key': 'KP-1-CON', 'folder': 'KYC', 'file': 'Passport J Doe.pdf'},
                {'key': 'KP-1-CON', 'folder': '', 'file': 'scan_0001.pdf'},
                {'key': 'KP-1-CON', 'folder': '', 'file': 'Mietvertrag Zug.pdf'},
                {'key': 'KP-1-CON', 'folder': '', 'file': 'Schreiben Kunde Mai.pdf'}]}))
            ai = FakeAI({'documents': lambda p: [{'id': x['id'], 'value': 'other', 'confidence': 0.4}
                                                 for x in p['items']]})
            data = m2_ai.classify_documents(tmp, ai)
            self.assertEqual([d['doc_type'] for d in data['documents']], ['passport_id', 'other', 'contract', 'other'])
            self.assertEqual(data['documents'][1]['reason'], 'generic file name: open it to type it')
            self.assertEqual(len(ai.asked), 1)                         # only the non-generic unknown name
            self.assertEqual(len(ai.asked[0][2]['items']), 1)
            self.assertEqual(data['documents'][0]['provider'], 'rules')
            self.assertEqual(data['document_gaps'][0]['missing'], ['proof_of_address'])   # KYC folder counts
            self.assertTrue(all(prov == 'ollama' for _t, prov, _p in ai.asked))


    def test_same_name_pattern_asked_once_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = ['Schreiben 2021.pdf', 'Schreiben 2022.pdf', 'Schreiben 2023.pdf', 'Notiz Kunde.pdf']
            Path(tmp, 'summary.json').write_text(json.dumps({'documents': [
                {'key': 'KP-1-CON', 'folder': 'Post', 'file': f} for f in names]}))
            ai = FakeAI({'documents': lambda p: [{'id': x['id'], 'value': 'contract', 'confidence': 0.5}
                                                 for x in p['items']]}, doc_model_limit=1)
            data = m2_ai.classify_documents(tmp, ai)
            self.assertEqual(len(ai.asked[0][2]['items']), 1)          # the most common pattern only
            self.assertEqual([d['doc_type'] for d in data['documents']], ['contract'] * 3 + ['other'])
            self.assertIn('over the limit', data['documents'][3]['reason'])

    def test_stop_at_next_batch(self):
        ai = FakeAI({'officer_types': []}, batch=1)
        ai.on_progress = lambda frac, detail='': setattr(ai, 'cancelled', frac > 0)
        with self.assertRaises(m2_ai.Cancelled):
            ai.classify('officer_types', 's', [{'id': 'o0'}, {'id': 'o1'}, {'id': 'o2'}], set(m2_ai.REGISTERS))
        self.assertEqual(len(ai.asked), 1)

class TestResume(unittest.TestCase):

    def test_answers_kept_and_reused_after_a_stop(self):
        items = [{'id': f"o{i}", 'code': f"C{i}"} for i in range(4)]
        answer = lambda p: [{'id': x['id'], 'value': 'directors', 'confidence': 0.7} for x in p['items']]  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            first = FakeAI({'officer_types': answer}, batch=2)
            first.cache_path = Path(tmp, m2_ai.CACHE_FILE)
            first.on_progress = lambda frac, detail='': setattr(first, 'cancelled', frac >= 0.5)
            with self.assertRaises(m2_ai.Cancelled):
                first.classify('officer_types', 's', items, set(m2_ai.REGISTERS))
            self.assertEqual(len(first.asked), 1)                       # stopped after the first batch
            again = FakeAI({'officer_types': answer}, batch=2)
            again.cache_path = first.cache_path
            res = again.classify('officer_types', 's', items, set(m2_ai.REGISTERS))
            self.assertEqual(len(res), 4)
            self.assertEqual([x['code'] for x in again.asked[0][2]['items']], ['C2', 'C3'])   # only the rest
            self.assertEqual(again.reused, 2)
            self.assertEqual(oct(first.cache_path.stat().st_mode & 0o777), '0o600')
            other = FakeAI({'officer_types': answer}, batch=2, ollama_model='other:1b')    # other model: ask again
            other.cache_path = first.cache_path
            other.classify('officer_types', 's', items, set(m2_ai.REGISTERS))
            self.assertEqual(len(other.asked), 2)

    def test_memory_check(self):
        ai = m2_ai.AI({**m2_ai.DEFAULTS})
        replies = {'/api/ps': {'models': []}, '/api/tags': {'models': [{'name': 'llama3.1:8b', 'size': 5 << 30}]}}
        with mock.patch.object(ai, '_ollama', side_effect=lambda path, *a, **k: replies[path]):
            with mock.patch.object(m2_ai, 'free_memory', return_value=3 << 30):
                self.assertIn('Not enough free memory', ai.memory_check())
            with mock.patch.object(m2_ai, 'free_memory', return_value=16 << 30):
                self.assertIsNone(ai.memory_check())
            replies['/api/ps'] = {'models': [{'name': 'llama3.1:8b'}]}             # loaded already
            with mock.patch.object(m2_ai, 'free_memory', return_value=1 << 30):
                self.assertIsNone(ai.memory_check())


class TestDocumentRules(unittest.TestCase):

    def run_docs(self, docs):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'summary.json').write_text(json.dumps({'documents': docs}))
            return m2_ai.classify_documents(tmp, FakeAI({'documents': []}), use_model=False)['documents']

    def test_folder_words_do_not_make_bank_documents(self):
        out = self.run_docs([{'key': 'A', 'folder': 'Financial Statement', 'file': 'FS 2019 signed.pdf'},
                             {'key': 'A', 'folder': 'Financial accounting', 'file': 'Ledger 2018.xlsx'},
                             {'key': 'A', 'folder': 'Bank Statements', 'file': 'UBS March.pdf'}])
        self.assertEqual([d['doc_type'] for d in out], ['financials', 'financials', 'bank'])

    def test_file_name_before_folder(self):
        out = self.run_docs([{'key': 'A', 'folder': 'Invoices PDF', 'file': 'Passport copy.pdf'},
                             {'key': 'A', 'folder': 'Invoices PDF', 'file': '20220336 2022-12-16.pdf'}])
        self.assertEqual([d['doc_type'] for d in out], ['passport_id', 'invoice_fee'])
        self.assertEqual([d['confidence'] for d in out], [0.9, 0.75])

    def test_poa_is_proof_of_address_unless_attorney(self):
        out = self.run_docs([{'key': 'A', 'folder': 'KYC', 'file': 'POA 2021.pdf'},
                             {'key': 'A', 'folder': 'Legal', 'file': 'POA - Power of Attorney.pdf'},
                             {'key': 'A', 'folder': 'Legal', 'file': 'Bankvollmacht.pdf'}])
        self.assertEqual([d['doc_type'] for d in out], ['proof_of_address', 'power_of_attorney',
                                                        'power_of_attorney'])

    def test_emails_and_corresp_folders_are_correspondence(self):
        out = self.run_docs([{'key': 'A', 'folder': 'Misc', 'file': 'Re update.msg'},
                             {'key': 'A', 'folder': 'Corresp. with client', 'file': 'Note 12.pdf'}])
        self.assertEqual([d['doc_type'] for d in out], ['correspondence', 'correspondence'])
        self.assertEqual([d['upload'] for d in out], [False, True], "emails stay in the archive")
        self.assertEqual(out[1]['odoo_folder'], 'CORRCL')

    def test_odoo_targets(self):
        t = m2_ai.odoo_target
        self.assertEqual(t({'key': 'KP-1-ENT', 'folder': 'KYC', 'file': 'Passport.pdf', 'doc_type': 'passport_id'})[:2],
                         ('PASSPORT', None))
        self.assertEqual(t({'key': 'KP-1-CON', 'folder': 'KYC', 'file': 'POA.pdf',
                            'doc_type': 'proof_of_address'})[0], 'POA_IND')
        self.assertEqual(t({'key': 'KP-1-ENT', 'folder': 'Bank', 'file': 'Kontoauszug 03.pdf',
                            'doc_type': 'bank'})[0], 'BANKST')
        self.assertEqual(t({'key': 'KP-1-ENT', 'folder': 'Corporate documents', 'file': 'x.pdf',
                            'doc_type': 'other'})[:2], (None, 'CORPDOC'))
        self.assertEqual(t({'key': 'KP-1-ENT', 'folder': 'Payment orders/2019', 'file': 'x.pdf',
                            'doc_type': 'other'})[1], 'PAYORD')
        self.assertEqual(t({'key': 'KP-1-CON', 'folder': 'Payment orders', 'file': 'x.pdf',
                            'doc_type': 'other'})[1], None, "no such folder for a person: workspace root")
        self.assertEqual(t({'key': 'KP-1-ENT', 'folder': 'QB', 'file': 'Co.qbw', 'doc_type': 'system_file'})[2],
                         False)

    def test_accounting_software_files(self):
        out = self.run_docs([{'key': 'A', 'folder': 'QB 2017/X', 'file': n} for n in
                             ('Lovina Ltd.qbw', 'Lovina Ltd.qbw.TLG', 'segments_1k', 'segments.gen', '_a.cfx')])
        self.assertEqual({d['doc_type'] for d in out}, {'system_file'})


class TestCoverageAcrossHolders(unittest.TestCase):

    @staticmethod
    def row(key, holder, doc_type, folder=''):
        return {'key': key, 'holder': holder, 'folder': folder, 'file': 'x.pdf', 'doc_type': doc_type,
                'confidence': 0.9}

    def test_mandate_is_judged_over_all_holders(self):
        rows = [self.row('KP-00031-ENT', 'NOVA-PH', 'incorporation'),
                self.row('KP-31-ENT', 'NOVA-DUBAI', 'register_extract'),      # same mandate, other padding
                self.row('KP-31-ENT', 'NOVA-DUBAI', 'passport_id', 'KYC'),
                self.row('KP-00040-CON', 'ORBIT-CH', 'passport_id'),
                self.row('KP-00001-ADM', 'ORBIT-CH', 'other'),
                self.row('KP-00133-DIR', 'NOVA-PH', 'financials'),
                self.row('KP-00009-XYZ', 'NOVA-PH', 'financials')]
        coverage, gaps = m2_ai.document_coverage(rows)
        by = {m2_ai.client_key(c['key']): c for c in coverage}
        ent = by['KP-31-ENT']
        self.assertEqual(ent['holders'], ['NOVA-DUBAI', 'NOVA-PH'])
        self.assertEqual(ent['status'], 'complete')                          # KYC folder in the other holder
        self.assertIn({'need': 'passport_id', 'missing_in': ['NOVA-PH'], 'found_in': ['NOVA-DUBAI']},
                      ent['covered_elsewhere'])
        self.assertEqual(by['KP-40-CON']['missing'], ['proof_of_address', 'kyc_compliance'])
        self.assertEqual(by['KP-1-ADM']['status'], 'internal')
        self.assertEqual(by['KP-133-DIR']['type'], 'person')                  # director mandate
        self.assertEqual(by['KP-9-XYZ']['status'], 'type unknown')
        self.assertEqual([g['key'] for g in gaps], ['KP-00133-DIR', 'KP-00040-CON'])

    def test_acc_is_checked_like_ent(self):
        _cov, gaps = m2_ai.document_coverage([self.row('KP-00016-ACC', 'NOVA-PH', 'financials')])
        self.assertEqual(gaps[0]['missing'], ['incorporation', 'register_extract or articles', 'kyc_compliance',
                                              'passport_id'])

    def test_workbook(self):
        rows = [self.row('KP-00031-ENT', 'NOVA-PH', 'incorporation'), self.row('KP-31-ENT', 'ORBIT-SG', 'articles')]
        coverage, _gaps = m2_ai.document_coverage(rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, m2_ai.COVERAGE_FILE)
            m2_ai.write_coverage_workbook(path, coverage, rows)
            import openpyxl
            wb = openpyxl.load_workbook(path)
            self.assertEqual(wb.sheetnames, ['Mandates', 'Where found', 'Per holder'])
            self.assertEqual(wb['Mandates']['D2'].value, 'NOVA-PH, ORBIT-SG')
            self.assertEqual(wb['Per holder'].max_row, 3)


if __name__ == '__main__':
    unittest.main()


class TestSortingList(unittest.TestCase):
    """The person's sorting list: written per name pattern, read back as rules, applied without the model."""

    def test_round_trip(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            run, config = Path(tmp) / 'run', Path(tmp) / 'm2_config.json'
            run.mkdir()
            config.write_text('{"company_map": {"NOVA-PH": "Nova Services Inc"}}')
            docs = [{'key': 'KP-1-ENT', 'folder': 'Misc', 'file': f'Scan 2019-0{n}.pdf', 'doc_type': 'other',
                     'upload': True, 'odoo_type': None, 'odoo_folder': None} for n in (1, 2, 3)]
            docs.append({'key': 'KP-2-ENT', 'folder': 'Old stuff', 'file': 'notes.txt', 'doc_type': 'other',
                         'upload': True, 'odoo_type': None, 'odoo_folder': None})
            docs.append({'key': 'KP-2-ENT', 'folder': 'Corporate documents', 'file': 'x.pdf', 'doc_type': 'other',
                         'upload': True, 'odoo_type': None, 'odoo_folder': 'CORPDOC'})       # placed: not listed
            (run / m2_ai.SUGGESTIONS_FILE).write_text(json.dumps({'documents': docs}))
            out = m2_ai.write_sorting_workbook(run)
            self.assertEqual((out['patterns'], out['files']), (2, 4))
            book = openpyxl.load_workbook(run / m2_ai.SORTING_FILE)
            ws = book['To sort']
            self.assertEqual(ws['A2'].value, 3)                       # the Scan pattern: three files
            ws['G2'] = '03 Banking / 03 Payment Orders'
            ws['J3'] = 'No'                                           # Old stuff: keep in the archive
            wf = book['By M2 folder']
            self.assertEqual([r[3] for r in wf.iter_rows(min_row=2, values_only=True)], ['Misc', 'Old stuff'])
            wf['E2'] = '04 Finance / 01 Financial Accounting'         # the whole Misc folder ...
            book.save(run / m2_ai.SORTING_FILE)
            result = m2_ai.learn_sorting(run, config)
            self.assertEqual((result['rules'], result['documents_changed']), (3, 4))
            kept = json.loads(config.read_text())
            self.assertEqual(kept['company_map'], {'NOVA-PH': 'Nova Services Inc'})   # the rest of the settings stay
            rows = json.loads((run / m2_ai.SUGGESTIONS_FILE).read_text())['documents']
            self.assertEqual({r['odoo_folder'] for r in rows[:3]}, {'PAYORD'})   # ... but the pattern row wins
            self.assertEqual(m2_ai.odoo_target({'key': 'KP-9-ENT', 'folder': 'Misc', 'file': 'other.pdf',
                                                'doc_type': 'other'}, m2_ai.document_rules(json.loads(
                                                    config.read_text())))[1], 'FINACC')
            self.assertFalse(rows[3]['upload'])
            rules = m2_ai.document_rules(kept)                        # a new file of the same pattern follows
            self.assertEqual(m2_ai.odoo_target({'key': 'KP-9-ENT', 'folder': 'Misc', 'file': 'Scan 2024-11.pdf',
                                                'doc_type': 'other'}, rules)[1], 'PAYORD')

    def test_reference_letter(self):
        self.assertEqual(m2_ai.odoo_target({'key': 'KP-1-CON', 'folder': 'KYC', 'file': 'Bank reference letter.pdf',
                                            'doc_type': 'bank'})[0], 'REFLTR_IND')
