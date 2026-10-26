"""Resumable staged recipe: actual DataTrove CPU execution, exact dedup, API, export."""
import asyncio
from collections import Counter
from contextlib import contextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import time

from .core import (assert_inputs_unchanged, configured_inputs, encoded, exact_signature, file_sha,
                   iter_jsonl, iter_records, normalize, prompt_signature, read_json, require, save_new, sha)
from .judge import health, judge_chunk


@contextmanager
def lock(root):
    path = root / '.run.lock'
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(encoded({'pid': os.getpid(), 'created_ns': time.time_ns()}))
    try:
        yield
    finally:
        path.unlink()


def directory_stage(root, name, fingerprint):
    final, work = root / name, root / '_work' / name
    if final.exists():
        manifest = read_json(final / 'manifest.json')
        require(manifest['fingerprint'] == fingerprint, 'completed stage fingerprint differs')
        for name, info in manifest['files'].items():
            require((final / name).stat().st_size == info['bytes'], 'completed stage output size changed')
        return final, None
    work.mkdir(parents=True, exist_ok=True)
    binding = work / 'binding.json'
    if binding.exists():
        require(read_json(binding)['fingerprint'] == fingerprint, 'unfinished stage fingerprint differs')
    else:
        save_new(binding, {'fingerprint': fingerprint})
    return final, work


def finish_stage(final, work, fingerprint, counts, files):
    manifest = {'fingerprint': fingerprint, 'counts': counts, 'files': files,
                'hashes_computed_at_write': True, 'resume_check': 'identity and byte size, not full rehash'}
    path = work / 'manifest.json'
    if path.exists():
        require(read_json(path) == manifest, 'unfinished publication differs')
    else:
        save_new(path, manifest)
    require(not final.exists(), 'stage final already exists')
    work.rename(final)
    return final


class JSONWriter:
    def __init__(self, root, name):
        import hashlib
        self.final = root / name
        self.path = root / (name + '.partial')
        for old in (self.path, self.final):
            if old.exists():
                old.rename(Path(str(old) + '.interrupted-' + str(time.time_ns())))
        self.file, self.hasher, self.size = self.path.open('xb'), hashlib.sha256(), 0

    def write(self, value):
        raw = encoded(value)
        self.file.write(raw)
        self.hasher.update(raw)
        self.size += len(raw)

    def close(self):
        self.file.close()
        self.path.rename(self.final)
        return self.final.name, {'bytes': self.size, 'sha256': self.hasher.hexdigest()}


def mechanical_stage(root, config, inputs, fingerprint):
    from datatrove.executor.local import LocalPipelineExecutor
    from .steps import MechanicalStep, SFTReader, StageWriter
    final, work = directory_stage(root, '01_mechanical', fingerprint)
    if work is None:
        return final
    tasks = min(len(inputs), config['workers'] * 4)
    pipeline = [SFTReader(inputs, config['max_line_bytes']), MechanicalStep(config['rules']), StageWriter(work)]
    if min(config['workers'], tasks) == 1:
        # DataTrove's public PipelineStep call interface avoids an unnecessary
        # Manager process for one worker (also makes offline deployment smoke portable).
        for rank in range(tasks):
            stats_path = work / ('part-%05d.stats.json' % rank)
            if stats_path.exists():
                saved = read_json(stats_path)
                require(all((work/n).stat().st_size == v['bytes'] for n,v in saved['files'].items()),
                        'completed rank output size changed')
                continue
            data = None
            for block in pipeline:
                data = block(data, rank=rank, world_size=tasks)
            for _ in data:
                pass
    else:
        executor = LocalPipelineExecutor(
            pipeline=pipeline, tasks=tasks, workers=min(config['workers'], tasks),
            logging_dir=str(work / 'datatrove_logs'), start_method='spawn', skip_completed=True)
        executor.run()
    counts, targets, reasons, source_states, files = Counter(), Counter(), Counter(), Counter(), {}
    for rank in range(tasks):
        item = read_json(work / ('part-%05d.stats.json' % rank))
        counts.update(item['rows'])
        targets.update(item['supervised_targets'])
        reasons.update(item['reasons'])
        source_states.update(item['source_states'])
        files.update(item['files'])
    assert_inputs_unchanged(inputs)
    return finish_stage(final, work, fingerprint, {'rows': dict(counts), 'supervised_targets': dict(targets),
                        'reasons': dict(reasons), 'source_states': dict(source_states)}, files)


def stage1_candidates(stage):
    # Only known rank outputs; interrupted copies never enter a new stage.
    candidates = sorted(stage.glob('part-*.candidate.jsonl')) + sorted(stage.glob('part-*.review.jsonl'))
    for path in candidates:
        yield from iter_jsonl(path)


def dedup_stage(root, stage1, config, fingerprint):
    final, work = directory_stage(root, '02_exact_dedup', fingerprint)
    if work is None:
        return final
    scratch = Path(config['scratch_root']).expanduser().resolve() / root.name
    scratch.mkdir(parents=True, exist_ok=True)
    existing = list(scratch.glob('exact-*.sqlite*'))
    require(len(existing) <= 100 and sum(p.stat().st_size for p in existing) < config['scratch_max_bytes'],
            'scratch budget exceeded; preserve and inspect exact-index files')
    db_path = scratch / ('exact-%d.sqlite' % time.time_ns())
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE keys (kind TEXT, signature TEXT, owner TEXT, PRIMARY KEY(kind,signature)) WITHOUT ROWID')
    writers = {k: JSONWriter(work, k + '.jsonl') for k in ('retained', 'duplicates', 'holdout_matches')}
    counts, rows = Counter(), 0
    try:
        for item in config['holdout_files']:
            dataset = {'id': 'holdout/reference', 'domain': 'holdout', 'adapter': 'auto', 'supervision': 'all_assistant',
                       'field_map': item.get('field_map', {})}
            source = {'path': item['path'], 'format': item.get('format', 'jsonl'), 'split': 'test', 'dataset': dataset}
            for index, original in iter_records(source, config['max_line_bytes']):
                value = normalize(original, source, index)
                require(value['state'] == 'candidate', 'unreadable holdout QA; configure its field_map')
                conn.execute('INSERT OR IGNORE INTO keys VALUES (?,?,?)', ('holdout', prompt_signature(value), value['id']))
        conn.commit()
        assert_inputs_unchanged(config['holdout_files'])
        for value in stage1_candidates(stage1):
            rows += 1
            hit = conn.execute('SELECT owner FROM keys WHERE kind=? AND signature=?',
                               ('holdout', prompt_signature(value))).fetchone()
            signature = exact_signature(value)
            duplicate = conn.execute('SELECT owner FROM keys WHERE kind=? AND signature=?', ('sample', signature)).fetchone()
            if hit:
                state, owner = 'holdout_matches', hit[0]
            elif duplicate:
                state, owner = 'duplicates', duplicate[0]
            else:
                state, owner = 'retained', value['id']
                conn.execute('INSERT INTO keys VALUES (?,?,?)', ('sample', signature, owner))
            record = value if state == 'retained' else {'id': value['id'], 'owner': owner, 'reason': state,
                        'source': value['source'], 'source_path': value['source_path'], 'source_row': value['source_row']}
            writers[state].write(record)
            counts[state] += 1
            if rows % 5000 == 0:
                conn.commit()
                require(sum(p.stat().st_size for p in scratch.glob('exact-*.sqlite*')) < config['scratch_max_bytes'],
                        'scratch index budget exceeded')
                print('[sft-clean] DEDUP_PROGRESS ' + json.dumps({'rows': rows, 'retained': counts['retained']}), flush=True)
        conn.commit()
        require(sum(p.stat().st_size for p in scratch.glob('exact-*.sqlite*')) < config['scratch_max_bytes'], 'scratch budget exceeded')
    finally:
        conn.close()
        for writer in writers.values():
            if not writer.file.closed:
                writer.file.close()
    files = {}
    for writer in writers.values():
        # close() is idempotent for the underlying file but only publishes once.
        name, info = writer.close()
        files[name] = info
    counts['holdout_files_configured'] = len(config['holdout_files'])
    initial = read_json(stage1 / 'manifest.json')['counts']['rows']
    require(rows == initial.get('candidate', 0) + initial.get('review', 0), 'dedup input count does not close')
    require(rows == counts['retained'] + counts['duplicates'] + counts['holdout_matches'], 'dedup outputs do not close')
    return finish_stage(final, work, fingerprint, dict(counts), files)


def quality_stage(root, stage2, config, fingerprint, retry_uncertain=False):
    final, work = directory_stage(root, '03_quality', fingerprint)
    if work is None:
        return final
    asyncio.run(health(config['api']))  # GET only, no generation; fail before reserving requests.
    writer = JSONWriter(work, 'labels.jsonl')
    counts, scores, usage, technical_reasons = Counter(), Counter(), Counter(), Counter()
    chunk, part = [], 0
    try:
        for envelope in iter_jsonl(stage2 / 'retained.jsonl'):
            chunk.append(envelope)
            if len(chunk) < config['api']['chunk_size']:
                continue
            labels = asyncio.run(judge_chunk(chunk, work / ('part-%07d' % part), config['api'], retry_uncertain))
            for label in labels:
                writer.write(label)
                counts[label['state']] += 1
                if label.get('reason'):
                    technical_reasons[label['reason']] += 1
                if 'label' in label:
                    scores[str(label['label']['quality'])] += 1
                usage.update({k: v for k, v in label.get('usage', {}).items() if type(v) in (int, float)})
            print('[sft-clean] QUALITY_CHUNK ' + json.dumps({'part': part, 'rows': sum(counts.values()), 'states': dict(counts)}), flush=True)
            part += 1
            chunk = []
        if chunk:
            for label in asyncio.run(judge_chunk(chunk, work / ('part-%07d' % part), config['api'], retry_uncertain)):
                writer.write(label)
                counts[label['state']] += 1
                if label.get('reason'):
                    technical_reasons[label['reason']] += 1
                if 'label' in label:
                    scores[str(label['label']['quality'])] += 1
                usage.update({k: v for k, v in label.get('usage', {}).items() if type(v) in (int, float)})
        name, info = writer.close()
    finally:
        writer.file.close()
    expected = read_json(stage2 / 'manifest.json')['counts'].get('retained', 0)
    require(sum(counts.values()) == expected, 'quality labels do not cover retained rows')
    return finish_stage(final, work, fingerprint, {'rows': dict(counts), 'quality_scores': dict(scores),
                        'usage': dict(usage), 'technical_reasons': dict(technical_reasons),
                        'verification': 'text-model-proxy; no execution or fact certification'}, {name: info})


def delivery_stage(root, stage2, stage3, config, fingerprint):
    final, work = directory_stage(root, '04_delivery', fingerprint)
    if work is None:
        return final
    writers = {k: JSONWriter(work, k + '.jsonl') for k in ('selected.original', 'selected.training_view', 'attributes')}
    counts, source_counts, languages, cot, domains = Counter(), Counter(), Counter(), Counter(), Counter()
    labels = iter(iter_jsonl(stage3 / 'labels.jsonl'))
    try:
        for row in iter_jsonl(stage2 / 'retained.jsonl'):
            label = next(labels, None)
            require(label and row['id'] == label['id'], 'labels and source rows not aligned')
            writers['attributes'].write(dict(label, source=row['source'], source_path=row['source_path'],
                source_row=row['source_row'], train_turns=row['train_turns']))
            counts[label['state']] += 1
            if label['state'] != 'candidate':
                continue
            writers['selected.original'].write(row['original'])
            writers['selected.training_view'].write({'id': row['id'], 'messages': row['messages'],
                'train_turns': row['train_turns'], 'source': row['source']})
            source_counts[row['source']] += 1
            domains[row['domain']] += 1
            counts['supervised_targets_in_candidates'] += sum(row['train_turns'])
            languages[label['label']['language']] += 1
            cot[str(label['label']['cot'])] += 1
        require(next(labels, None) is None, 'extra model labels')
        files = dict(writer.close() for writer in writers.values())
    finally:
        for writer in writers.values():
            writer.file.close()
    summary = {'status': 'MODEL_CANDIDATES_NOT_VERIFIED', 'counts': dict(counts), 'source_rows': dict(source_counts),
        'domain_rows': dict(domains), 'language_rows': dict(languages), 'cot_rows': dict(cot),
        'holdout_status': 'exact_context_only' if config['holdout_files'] else 'not_configured',
        'format': 'loss-mask-aware recipe view; trainer integration required; not a UniMoE schema certification',
        'original_answers_rewritten': 0, 'media_reads': 0, 'executable_verification': 'not_performed'}
    save_new(work / 'summary.json', summary)
    readme = '# SFT清洗候选结果\n\n状态：模型文本评审候选，尚未完成领域验真或训练器格式认证。\n\n'
    readme += '| 状态/统计 | 数量 |\n|---|---:|\n' + ''.join('| %s | %s |\n' % (k,v) for k,v in counts.items())
    readme += '\n`selected.training_view.jsonl`包含messages和train_turns；训练器必须按mask计算loss，Chat v3仅最后一条assistant为true。\n'
    readme += '\n`selected.original.jsonl`只供原文回溯，不得忽略mask直接全回合训练。原答案未改写。attributes与02保留行逐行对齐。\n'
    readme += '\n各阶段manifest、拒绝/待复核/延后记录、重复与holdout命中、API检查点均在本run目录。无配置holdout时没有去污染保证。\n'
    (work / 'README.md').write_text(readme, encoding='utf-8')
    for name in ('summary.json', 'README.md'):
        files[name] = {'bytes': (work/name).stat().st_size, 'sha256': file_sha(work/name)}
    return finish_stage(final, work, fingerprint, summary, files)


def run(config, mode='mechanical', limit=None, retry_uncertain=False):
    inputs = configured_inputs(config)
    # Holdout file identities also bind the run; no automatic downloads.
    for item in config['holdout_files']:
        p = Path(item['path']).expanduser().resolve()
        require(p.is_file(), 'missing explicit holdout file')
        item['path'] = str(p)
        item['bytes'], item['mtime_ns'] = p.stat().st_size, p.stat().st_mtime_ns
    if limit is not None:
        require(type(limit) is int and limit > 0, 'limit must be positive')
        base, extra = divmod(limit, len(inputs))
        inputs = [dict(x, limit=base+int(i<extra)) for i,x in enumerate(inputs)]
        inputs = [x for x in inputs if x['limit']]
    code = {p.name: file_sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))}
    fingerprint = sha({'config': config, 'inputs': inputs, 'mode': mode, 'limit': limit, 'code': code})
    require(config['run_name'] and all(c.isalnum() or c in '-_' for c in config['run_name']), 'invalid run_name')
    root = Path(config['output_root']).expanduser().resolve() / (config['run_name'] + '-' + mode + '-' + fingerprint[:16])
    for source in inputs:
        require(not Path(source['path']).is_relative_to(root), 'output cannot contain an input file')
    root.mkdir(parents=True, exist_ok=True)
    with lock(root):
        manifest = root / 'run.json'
        info = {'fingerprint': fingerprint, 'config': config, 'inputs': inputs, 'code_sha256': code,
                'mode': mode, 'limit': limit, 'input_hash_scope': 'file identity/size/mtime; not full byte SHA',
                'packages': {name: importlib.metadata.version(name) for name in ('datatrove', 'httpx')},
                'sampling': 'per-file prefix allocation, not a uniform accuracy sample' if limit else 'full configured files'}
        if manifest.exists():
            require(read_json(manifest) == info, 'run manifest differs')
        else:
            save_new(manifest, info)
        stage1 = mechanical_stage(root, config, inputs, fingerprint)
        stage2 = dedup_stage(root, stage1, config, fingerprint)
        assert_inputs_unchanged(config['holdout_files'])
        if mode == 'mechanical':
            result = {'status': 'MECHANICAL_ONLY_NOT_MODEL_SCORED', 'run': str(root), 'stage': str(stage2)}
        else:
            stage3 = quality_stage(root, stage2, config, fingerprint, retry_uncertain)
            assert_inputs_unchanged(inputs)
            result = {'status': 'MODEL_CANDIDATES_NOT_VERIFIED', 'run': str(root),
                      'delivery': str(delivery_stage(root, stage2, stage3, config, fingerprint))}
        assert_inputs_unchanged(inputs)
        assert_inputs_unchanged(config['holdout_files'])
    print('[sft-clean] PASS ' + json.dumps(result), flush=True)
    return result
