import copy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest

from sft_cleaner.core import (configured_inputs, exact_signature, iter_records, mechanical,
                              normalize, prompt_signature)

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT / 'config/server.example.json').read_text(encoding='utf-8'))


def source(mode='all_assistant', split='train'):
    return {'path': '/fixture/input.jsonl', 'split': split, 'config': '', 'format': 'jsonl',
            'dataset': {'id': 'fixture/sft', 'domain': 'math', 'supervision': mode, 'adapter': 'auto'}}


class CoreTests(unittest.TestCase):
    def test_sharegpt_and_messages_have_same_dedup_signature(self):
        a = normalize({'conversations': [{'from': 'human', 'value': '1+1?'}, {'from': 'gpt', 'value': '2'}]}, source(), 0)
        b = normalize({'messages': [{'role': 'user', 'content': '1+1?'}, {'role': 'assistant', 'content': '2'}]}, source(), 1)
        self.assertEqual(a['state'], 'candidate')
        self.assertEqual(exact_signature(a), exact_signature(b))
        self.assertEqual(prompt_signature(a), prompt_signature(b))
        self.assertIn('conversations', a['original'])

    def test_chat_last_turn_only_and_context_preserved(self):
        row = {'messages': [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'},
                            {'role': 'user', 'content': 'c'}, {'role': 'assistant', 'content': 'd', 'reasoning_content': 'reason'}],
               'metadata': {'train_turns': [False, False, False, True]}}
        before = copy.deepcopy(row)
        item = normalize(row, source('split_aware_chat_last', 'chat'), 0)
        self.assertEqual(item['state'], 'candidate')
        self.assertEqual(item['train_turns'], [False, False, False, True])
        self.assertEqual(len(item['messages']), 4)
        self.assertEqual(item['messages'][-1]['reasoning_content'], 'reason')
        self.assertEqual(row, before)
        row['metadata']['train_turns'][1] = True
        bad = normalize(row, source('split_aware_chat_last', 'chat'), 0)
        self.assertEqual(bad['state'], 'deferred')
        self.assertIn('chat_last_turn_mask_conflict', bad['issues'])

    def test_instruction_following_split_does_not_force_last_only(self):
        row = {'messages': [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'},
                            {'role': 'user', 'content': 'c'}, {'role': 'assistant', 'content': 'd'}]}
        item = normalize(row, source('split_aware_chat_last', 'instruction_following'), 0)
        self.assertEqual(item['train_turns'], [False, True, False, True])

    def test_fineweb_plain_content_is_deferred_not_fabricated(self):
        s = source()
        s['dataset'].update(id='openbmb/Ultra-FineWeb-L3', adapter='explicit_qa_only')
        item = normalize({'content': 'Original text. Q: implied question? A: possible answer', 'style': 'blog'}, s, 0)
        self.assertEqual(item['state'], 'deferred')
        explicit = normalize({'context': 'Document context', 'qa_pairs': [{'question': 'q', 'answer': 'a'}]}, s, 1)
        self.assertEqual(explicit['state'], 'candidate')
        self.assertIn('Document context', explicit['messages'][0]['content'])

    def test_missing_original_prompt_deferred_but_actual_empty_rejected(self):
        for value, state in [(None, 'deferred'), ('', 'rejected')]:
            item = normalize({'messages': [{'role': 'user', 'content': value}, {'role': 'assistant', 'content': 'a'}]}, source(), 0)
            self.assertEqual(item['state'], state)

    def test_empty_answer_rejected_and_unknown_proof_schema_deferred(self):
        self.assertEqual(normalize({'question': 'q', 'answer': ''}, source(), 0)['state'], 'rejected')
        self.assertEqual(normalize({'formal_statement': 'theorem t : True', 'lean_proof': 'by trivial'}, source(), 0)['state'], 'deferred')

    def test_explicit_field_map_handles_specialized_proof(self):
        s = source()
        s['dataset']['field_map'] = {'question': 'formal_statement', 'answer': 'lean_proof', 'context': 'lean_header'}
        item = normalize({'formal_statement': 'theorem t : True', 'lean_proof': 'by trivial', 'lean_header': 'import Mathlib'}, s, 0)
        self.assertEqual(item['state'], 'candidate')
        self.assertIn('import Mathlib', item['messages'][0]['content'])

    def test_tool_chain_ids_preserved_and_incomplete_chain_deferred(self):
        row = {'tools': [{'name': 'compute'}], 'messages': [
            {'role': 'user', 'content': 'compute'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'x', 'function': {'name': 'compute', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'x', 'content': '2'}, {'role': 'assistant', 'content': '2'}]}
        item = normalize(row, source(), 0)
        self.assertEqual(item['state'], 'candidate')
        self.assertEqual(item['messages'][1]['tool_calls'][0]['id'], 'x')
        row['messages'][2]['tool_call_id'] = 'wrong'
        self.assertEqual(normalize(row, source(), 0)['state'], 'deferred')

    def test_repetition_only_reviews_and_non_thinking_not_penalized(self):
        ordinary = normalize({'question': '1+1?', 'answer': '2'}, source(), 0)
        self.assertEqual(mechanical(ordinary, config()['rules'])['state'], 'candidate')
        repeat = normalize({'question': 'q', 'answer': 'same words repeat forever ' * 300}, source(), 1)
        self.assertEqual(mechanical(repeat, config()['rules'])['state'], 'review')

    def test_multimodal_and_non_text_blocks_deferred(self):
        item = normalize({'question': 'q', 'answer': 'a', 'images': ['image.jpg']}, source(), 0)
        self.assertEqual(item['state'], 'deferred')

    def test_jsonl_bad_line_accounted_json_array_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'test.jsonl'
            p.write_text('{"question":"q","answer":"a"}\nnot JSON\n', encoding='utf-8')
            s = dict(source(), path=str(p))
            rows = list(iter_records(s))
            self.assertEqual(len(rows), 2)
            self.assertEqual(normalize(rows[1][1], s, 1)['state'], 'rejected')
            p = Path(tmp) / 'test.json'
            p.write_text('[{"question":"q","answer":"a"}]', encoding='utf-8')
            self.assertEqual(len(list(iter_records(dict(s, path=str(p), format='json')))), 1)

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'), 'optional Parquet dependency not installed')
    def test_parquet_reads_structured_qa_without_guessing_web_text(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'qa.parquet'
            pq.write_table(pa.Table.from_pylist([{'question':'q','answer':'a'}]), path)
            s = dict(source(), path=str(path), format='parquet')
            records = list(iter_records(s))
            self.assertEqual(len(records), 1)
            self.assertEqual(normalize(records[0][1], s, 0)['state'], 'candidate')

    def test_catalog_has_nine_sources_no_rl_and_evaluation_split_refused(self):
        cfg = config()
        self.assertEqual(len(cfg['datasets']), 9)
        self.assertFalse(any('rl-' in x['id'].lower() for x in cfg['datasets']))
        self.assertEqual(configured_inputs(cfg, allow_empty=True), [])
        with self.assertRaisesRegex(ValueError, 'No inputs'):
            configured_inputs(cfg)
        cfg['datasets'][0]['enabled'] = True
        cfg['datasets'][0]['files'] = [{'path': '/unused', 'split': 'validation'}]
        with self.assertRaisesRegex(ValueError, 'non-evaluation split'):
            configured_inputs(cfg)

    def test_top_level_loss_mask_not_lost_on_recleaning(self):
        row = {'messages': [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'},
                            {'role': 'user', 'content': 'c'}, {'role': 'assistant', 'content': 'd'}],
               'train_turns': [False, False, False, True]}
        item = normalize(row, source(), 0)
        self.assertEqual(item['train_turns'], row['train_turns'])
        unmasked = normalize({k:v for k,v in row.items() if k!='train_turns'}, source(), 1)
        self.assertNotEqual(exact_signature(item), exact_signature(unmasked))

    def test_flat_reasoning_and_final_answer_both_preserved(self):
        item = normalize({'question': 'q', 'answer': 'final', 'reasoning': 'reasoning text'}, source(), 0)
        self.assertEqual(item['messages'][-1]['content'], 'final')
        self.assertEqual(item['messages'][-1]['reasoning_content'], 'reasoning text')

    def test_chat_and_ultradata_config_cannot_silently_use_train_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.jsonl'
            path.write_bytes(b'{}\n')
            for ident in ('nvidia/Nemotron-SFT-Instruction-Following-Chat-v3','openbmb/UltraData-SFT-2605'):
                cfg = config()
                dataset = next(x for x in cfg['datasets'] if x['id']==ident)
                dataset.update(enabled=True, files=[{'path': str(path), 'split': 'train'}])
                with self.assertRaisesRegex(ValueError, 'split must be explicit'):
                    configured_inputs(cfg)


if __name__ == '__main__':
    unittest.main()
