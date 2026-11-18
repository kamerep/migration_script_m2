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

    def test_alias_samples_must_fit(self):
        base = {'confidence': 0.8, 'provider': 'ollama', 'reason': ''}
        self.assertIsNone(m2_ai.check_alias({**base, 'value': 'email'}, 'Remark', ['call back']))
        self.assertIsNotNone(m2_ai.check_alias({**base, 'value': 'email'}, 'Mail', ['a@b.ch']))
        self.assertLessEqual(m2_ai.check_alias({**base, 'value': 'name'}, 'Order', ['1', '2'])['confidence'], 0.3)
        self.assertLessEqual(m2_ai.check_alias({**base, 'value': 'mandate_no'}, 'Ref', ['AP-1'])['confidence'], 0.4)
        # 'mandate_no' must not be read as a date field
        self.assertIsNotNone(m2_ai.check_alias({**base, 'value': 'mandate_no'}, 'Date', ['x']))


class TestDocuments(unittest.TestCase):

    def test_rules_then_model_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'summary.json').write_text(json.dumps({'documents': [
                {'key': 'AP-1', 'folder': 'KYC', 'file': 'Passport J Doe.pdf'},
                {'key': 'AP-1', 'folder': '', 'file': 'scan_0001.pdf'},
                {'key': 'AP-1', 'folder': '', 'file': 'Mietvertrag Zug.pdf'},
                {'key': 'AP-1', 'folder': '', 'file': 'Schreiben Kunde Mai.pdf'}]}))
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


if __name__ == '__main__':
    unittest.main()
