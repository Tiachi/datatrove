import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from sft_cleaner.core import encoded, normalize, read_json, sha
from sft_cleaner.judge import (PROMPT, decide, endpoint, health, judge_chunk, parse_label, payload)
from sft_cleaner.pipeline import run

ROOT = Path(__file__).resolve().parents[1]
LABEL = {'match': 1, 'error': 0, 'refusal': 0, 'quality': 8, 'cot': 1, 'language': 'en', 'reason': 'coherent'}


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond({'data': [{'id': 'qwen3.8-27b'}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/v1/chat/completions'
        assert body['max_tokens'] == 256 and body['chat_template_kwargs']['enable_thinking'] is False
        Handler.calls += 1
        self.respond({'choices': [{'message': {'content': json.dumps(LABEL)}}],
                      'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}})

    def respond(self, obj):
        raw = encoded(obj)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def cfg(self, root):
        cfg = read_json(ROOT / 'config/server.example.json')
        cfg.update(output_root=str(root/'out'), scratch_root=str(root/'scratch'), workers=1)
        cfg['api'].update(base_url='http://127.0.0.1:%d/v1' % self.server.server_port, chunk_size=2, concurrency=2)
        return cfg

    def row(self):
        s = {'path': '/fixture/q', 'split': 'train', 'config': '',
             'dataset': {'id': 'fixture/sft', 'domain': 'math', 'supervision': 'all_assistant'}}
        return normalize({'question': '1+1?', 'answer': '2'}, s, 0)

    def test_health_and_exact_api_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(asyncio.run(health(self.cfg(Path(tmp))['api']))['status'], 'PASS')
        for bad in ('http://host:80/v1/v1', 'http://user:secret@host:80/v1', 'http://host:80/v1?key=secret'):
            with self.assertRaises(ValueError):
                endpoint({'base_url': bad, 'model': 'model'})

    def test_schema_and_uncertainty_do_not_silently_pass(self):
        self.assertEqual(parse_label(json.dumps(LABEL)), LABEL)
        with self.assertRaises(ValueError):
            parse_label(json.dumps(dict(LABEL, quality=True)))
        with tempfile.TemporaryDirectory() as tmp:
            api = self.cfg(Path(tmp))['api']
            self.assertEqual(decide(self.row(), dict(LABEL, error=2), api), 'review')
            self.assertEqual(decide(self.row(), dict(LABEL, error=1), api), 'rejected')

    def test_successful_chunk_cached_without_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api, row = self.cfg(root)['api'], self.row()
            before = Handler.calls
            first = asyncio.run(judge_chunk([row], root/'part', api))
            second = asyncio.run(judge_chunk([row], root/'part', api))
            self.assertEqual(first, second)
            self.assertEqual(Handler.calls-before, 1)

    def test_unfinished_reserved_requests_need_explicit_recharge_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api, row = self.cfg(root)['api'], self.row()
            part = root/'part'
            part.mkdir()
            key = sha({'input': payload(row), 'api': api, 'prompt': PROMPT})
            (part/'reserved.json').write_bytes(encoded({'requests': {row['id']: key}, 'prompt_sha256': sha(PROMPT)}))
            before = Handler.calls
            with self.assertRaisesRegex(ValueError, 'explicit --retry-uncertain'):
                asyncio.run(judge_chunk([row], part, api))
            self.assertEqual(before, Handler.calls)
            result = asyncio.run(judge_chunk([row], part, api, retry_uncertain=True))
            self.assertEqual(result[0]['state'], 'candidate')
            self.assertEqual(Handler.calls-before, 1)

    def test_oversized_input_not_truncated_or_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = self.cfg(root)['api']
            api['max_input_chars'] = 1
            before = Handler.calls
            result = asyncio.run(judge_chunk([self.row()], root/'part', api))
            self.assertEqual(result[0]['reason'], 'input_too_long_not_truncated')
            self.assertEqual(before, Handler.calls)

    def test_actual_datatrove_end_to_end_stages_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self.cfg(root)
            p = root/'input.jsonl'
            records = [{'question': '1+1?', 'answer': '2'}, {'question': '1+1?', 'answer': '2'},
                       {'question': 'q', 'answer': ''}, {'content': 'unstructured text'},
                       {'question': 'color?', 'answer': 'blue'}]
            p.write_bytes(b''.join(encoded(x) for x in records))
            cfg['datasets'][0].update(enabled=True, files=[{'path': str(p), 'split': 'train'}])
            before = Handler.calls
            result = run(copy.deepcopy(cfg), mode='pilot', limit=10)
            directory = Path(result['run'])
            self.assertEqual(Handler.calls-before, 2)
            stage1 = read_json(directory/'01_mechanical/manifest.json')['counts']['rows']
            self.assertEqual(stage1, {'candidate': 3, 'rejected': 1, 'deferred': 1})
            self.assertEqual(read_json(directory/'02_exact_dedup/manifest.json')['counts']['duplicates'], 1)
            summary = read_json(Path(result['delivery'])/'summary.json')
            self.assertEqual(summary['counts']['candidate'], 2)
            self.assertEqual(summary['original_answers_rewritten'], 0)
            run(copy.deepcopy(cfg), mode='pilot', limit=10)
            self.assertEqual(Handler.calls-before, 2)
            self.assertEqual(p.read_bytes(), b''.join(encoded(x) for x in records))

    def test_holdout_exact_prompt_excluded_without_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self.cfg(root)
            p, holdout = root/'input.jsonl', root/'holdout.jsonl'
            p.write_bytes(encoded({'question':'held question','answer':'training answer'}))
            holdout.write_bytes(encoded({'question':'held question','answer':'different gold'}))
            cfg['datasets'][0].update(enabled=True, files=[{'path':str(p),'split':'train'}])
            cfg['holdout_files'] = [{'path':str(holdout), 'format':'jsonl'}]
            before = Handler.calls
            result = run(cfg, mode='mechanical')
            stats = read_json(Path(result['run'])/'02_exact_dedup/manifest.json')['counts']
            self.assertEqual(stats['holdout_matches'], 1)
            self.assertEqual(stats.get('retained',0), 0)
            self.assertEqual(before, Handler.calls)

    @unittest.skipIf(os.name == 'nt', 'Linux deployment validates multiworker LocalPipelineExecutor; Windows Manager IPC is not required for offline smoke')
    def test_linux_multiworker_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self.cfg(root)
            cfg['workers'] = 2
            files = []
            for i in range(2):
                path = root/('input-%d.jsonl' % i)
                path.write_bytes(encoded({'question': 'question %d' % i, 'answer': 'answer'}))
                files.append({'path':str(path), 'split':'train'})
            cfg['datasets'][0].update(enabled=True, files=files)
            result = run(cfg, mode='mechanical')
            self.assertEqual(read_json(Path(result['stage'])/'manifest.json')['counts']['retained'], 2)


if __name__ == '__main__':
    unittest.main()
