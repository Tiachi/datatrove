"""Configuration, typed adapters and conservative rules. No network or media IO."""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re


def require(ok, message):
    if not ok:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for raw in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(raw)
    return h.hexdigest()


def save_new(path, value):
    with Path(path).open('xb') as f:
        f.write(encoded(value))


def read_json(path, limit=8 * 1024 * 1024):
    with Path(path).open('rb') as f:
        raw = f.read(limit + 1)
    require(len(raw) <= limit, 'JSON configuration/report exceeds limit')
    return json.loads(raw)


def configured_inputs(config, allow_empty=False):
    require(type(config['workers']) is int and 1 <= config['workers'] <= 64, 'workers must be 1..64')
    api = config['api']
    require(not any(k in api for k in ('api_key', 'token', 'password')), 'use api_key_env; never embed secrets in config')
    require(type(api['concurrency']) is int and 1 <= api['concurrency'] <= 1024, 'concurrency must be 1..1024')
    require(type(api['chunk_size']) is int and 1 <= api['chunk_size'] <= 2048, 'chunk_size must be 1..2048')
    require(type(api['max_input_chars']) is int and 1 <= api['max_input_chars'] <= 10000000, 'invalid input character bound')
    require(type(api['max_output_tokens']) is int and 32 <= api['max_output_tokens'] <= 2048, 'invalid output token bound')
    require(0 < api['timeout_seconds'] <= 3600, 'invalid API timeout')
    require(type(api['enable_thinking']) is bool and type(api['json_mode']) is bool, 'API flags must be booleans')
    require(type(config['scratch_max_bytes']) is int and config['scratch_max_bytes'] > 0, 'invalid scratch budget')
    require(0 < config['rules']['repetition_ratio'] <= 1, 'invalid repetition ratio')
    require(2 <= config['rules']['ngram_n'] <= 32, 'ngram_n must be 2..32')
    require(1 <= config['api']['reject_max_quality'] < config['api']['keep_min_quality'] <= 10,
            'quality thresholds must satisfy 1 <= reject < keep <= 10')
    result, seen = [], set()
    for dataset in config['datasets']:
        require('/' in dataset['id'] and 'rl-' not in dataset['id'].lower() and
                'longbench' not in dataset['id'].lower(), 'RL/evaluation datasets are outside this SFT recipe')
        if not dataset['enabled']:
            continue
        require(dataset['files'], 'enabled dataset has no explicit files: ' + dataset['id'])
        for item in dataset['files']:
            require(item.get('split') and item['split'] not in ('test', 'validation', 'val', 'dev'),
                    'explicit non-evaluation split required')
            require(not dataset.get('allowed_splits') or item['split'] in dataset['allowed_splits'],
                    'source split must be explicit for thinking mode/chat supervision')
            require(not dataset.get('allowed_configs') or item.get('config') in dataset['allowed_configs'],
                    'source capability config must be explicit')
            path = Path(item['path']).expanduser().resolve()
            require(path.is_file() and path not in seen, 'missing or duplicate input: ' + str(path))
            seen.add(path)
            fmt = item.get('format', path.suffix.lstrip('.')).lower()
            require(fmt in ('jsonl', 'json', 'parquet'), 'input format must be jsonl/json-array/parquet')
            stat = path.stat()
            result.append({'path': str(path), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                           'format': fmt, 'dataset': {k:v for k,v in dataset.items() if k not in ('files','enabled')}, 'split': item['split'],
                           'config': item.get('config', ''), 'revision': item.get('revision', 'unknown')})
    require(allow_empty or result, 'No inputs configured; use add-input with an exact annotation path')
    return result


def assert_inputs_unchanged(inputs):
    for source in inputs:
        stat = Path(source['path']).stat()
        require((stat.st_size, stat.st_mtime_ns) == (source['bytes'], source['mtime_ns']),
                'input changed during run: ' + source['path'])


def iter_records(source, max_bytes=16 * 1024 * 1024):
    path, fmt = source['path'], source['format']
    if fmt == 'jsonl':
        with open(path, 'rb', buffering=1024 * 1024) as f:
            index = 0
            while True:
                raw = f.readline(max_bytes + 1)
                if not raw:
                    break
                require(len(raw) <= max_bytes, 'oversize JSONL line; raise max_line_bytes after inspection')
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    value = {'__parse_failure__': True, 'raw': raw.decode('utf-8', 'replace')}
                yield index, value
                index += 1
    elif fmt == 'json':
        import ijson
        with open(path, 'rb') as f:
            prefix = f.read(256).lstrip()
            require(prefix.startswith(b'['), 'JSON input must be a top-level array; use JSONL otherwise')
            f.seek(0)
            for index, value in enumerate(ijson.items(f, 'item', use_float=True)):
                require(len(encoded(value)) <= max_bytes, 'oversize JSON-array record')
                yield index, value
    else:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError('Parquet input requires the optional pyarrow dependency') from exc
        index = 0
        for batch in pq.ParquetFile(path).iter_batches(batch_size=128):
            for value in batch.to_pylist():
                require(len(encoded(value)) <= max_bytes, 'oversize Parquet record')
                yield index, value
                index += 1


ROLES = {'human': 'user', 'gpt': 'assistant', 'user': 'user', 'assistant': 'assistant',
         'system': 'system', 'developer': 'developer', 'tool': 'tool', 'function': 'tool'}


def text(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    if isinstance(value, list):
        require(all(isinstance(x, dict) and x.get('type') in ('text', 'input_text', 'output_text')
                    and isinstance(x.get('text'), str) for x in value), 'non-text content blocks are outside this recipe')
        return '\n'.join(x['text'] for x in value)
    raise ValueError('content is not text')


def explicit_messages(row, dataset):
    mapping = dataset.get('field_map', {})
    key = mapping.get('messages')
    for name in ([key] if key else ['messages', 'conversations']):
        if name in row and isinstance(row[name], list):
            result = []
            for item in row[name]:
                require(isinstance(item, dict), 'message must be an object')
                role = ROLES.get(item.get('role', item.get('from')))
                require(role is not None, 'unsupported conversation role')
                normalized = copy.deepcopy(item)
                normalized.pop('from', None)
                normalized.pop('value', None)
                normalized['role'] = role
                normalized['content'] = text(item.get('content', item.get('value')))
                if item.get('content', item.get('value')) is None and role == 'user':
                    normalized['_source_content_missing'] = True
                if normalized.get('reasoning_content') is not None:
                    require(isinstance(normalized['reasoning_content'], str), 'reasoning_content must be text')
                result.append(normalized)
            return result
    pairs = row.get(mapping.get('qa_pairs', 'qa_pairs'))
    if isinstance(pairs, list) and pairs:
        result = []
        context = row.get(mapping.get('context', 'context'), '')
        require(isinstance(context, str), 'context must be text')
        for i, pair in enumerate(pairs):
            require(isinstance(pair, dict), 'qa_pairs member must be object')
            q, a = pair.get('question'), pair.get('answer')
            require(isinstance(q, str) and isinstance(a, str), 'qa_pairs needs explicit question/answer strings')
            result += [{'role': 'user', 'content': (context + '\n\n' if i == 0 and context else '') + q},
                       {'role': 'assistant', 'content': a}]
        return result
    q_key, a_key = mapping.get('question'), mapping.get('answer')
    if q_key and a_key:
        q, a = row.get(q_key), row.get(a_key)
        require(isinstance(q, str) and isinstance(a, str), 'configured question/answer fields missing or not text')
        context = row.get(mapping.get('context', 'context'), '')
        require(isinstance(context, str), 'context must be text')
        assistant = {'role': 'assistant', 'content': a}
        reasoning = row.get(mapping.get('reasoning', 'reasoning_content'))
        if reasoning is not None:
            require(isinstance(reasoning, str), 'configured reasoning must be text')
            assistant['reasoning_content'] = reasoning
        return [{'role': 'user', 'content': (context + '\n\n' if context else '') + q}, assistant]
    # Conservative named pairs only. Never split a FineWeb content string on guessed markers.
    for q_key, a_key in [('question', 'answer'), ('query', 'response'), ('instruction', 'output'),
                         ('problem', 'solution')]:
        if isinstance(row.get(q_key), str) and isinstance(row.get(a_key), str):
            q = row[q_key]
            if q_key == 'instruction' and isinstance(row.get('input'), str) and row['input'].strip():
                q += '\n\n' + row['input']
            context = row.get('context', '')
            require(isinstance(context, str), 'context must be text')
            assistant = {'role': 'assistant', 'content': row[a_key]}
            reasoning = row.get('reasoning_content', row.get('reasoning'))
            if reasoning is not None:
                require(isinstance(reasoning, str), 'reasoning needs an explicit text field mapping')
                assistant['reasoning_content'] = reasoning
            return [{'role': 'user', 'content': (context + '\n\n' if context else '') + q}, assistant]
    return None


def normalize(row, source, index):
    dataset = source['dataset']
    ident = sha([dataset['id'], source['path'], index])
    envelope = {'id': ident, 'source': dataset['id'], 'source_path': source['path'], 'source_row': index,
                'source_split': source['split'], 'source_config': source.get('config', ''),
                'source_revision': source.get('revision', 'unknown'), 'domain': dataset['domain'],
                'original': row, 'issues': [], 'state': 'candidate'}
    if not isinstance(row, dict) or row.get('__parse_failure__'):
        return dict(envelope, state='rejected', issues=['invalid_json_or_record'])
    try:
        require(dataset.get('adapter', 'auto') in ('auto', 'explicit_qa_only'), 'unknown adapter; use explicit field_map')
        require(not any(row.get(k) for k in ('images', 'videos', 'audios')), 'media_record_not_text_sft')
        messages = explicit_messages(row, dataset)
        if messages is None:
            return dict(envelope, state='deferred', issues=['explicit_qa_not_available'])
        require(messages and any(m['role'] == 'user' for m in messages), 'empty_question')
        require(not any(m.get('_source_content_missing') for m in messages), 'source_context_missing')
        require(not any(m['role'] == 'user' and not m['content'].strip() for m in messages), 'empty_question')
        assistants = [i for i, m in enumerate(messages) if m['role'] == 'assistant']
        require(assistants, 'empty_answer')
        mode = dataset.get('supervision', 'all_assistant')
        last_only = mode == 'last_assistant' or (mode == 'split_aware_chat_last' and source['split'] == 'chat')
        mask = [i in assistants and (not last_only or i == assistants[-1]) for i in range(len(messages))]
        raw_mask = row.get('train_turns')
        metadata_mask = row.get('metadata', {}).get('train_turns') if isinstance(row.get('metadata'), dict) else None
        require(raw_mask is None or metadata_mask is None or raw_mask == metadata_mask, 'conflicting_train_turns')
        if raw_mask is None:
            raw_mask = metadata_mask
        if raw_mask is not None:
            require(isinstance(raw_mask, list) and len(raw_mask) == len(messages) and
                    all(type(x) is bool for x in raw_mask), 'invalid_train_turns')
            require(not any(x and messages[i]['role'] != 'assistant' for i, x in enumerate(raw_mask)),
                    'nonassistant_train_turn')
            if last_only:
                require(raw_mask == mask, 'chat_last_turn_mask_conflict')
            else:
                mask = raw_mask
        require(any(mask), 'no_supervised_answer')
        first_user = next(i for i,m in enumerate(messages) if m['role'] == 'user')
        require(not any(mask[:first_user]), 'supervised_answer_before_first_question')
        require(messages[-1]['role'] == 'assistant' and mask[-1], 'unfinished_or_unsupervised_final_answer')
        for i, message in enumerate(messages):
            if mask[i]:
                require(message['content'].strip() or message.get('tool_calls'), 'empty_answer')
            if message['role'] == 'assistant' and message.get('tool_calls'):
                require(i + 1 < len(messages) and messages[i + 1]['role'] == 'tool', 'unresolved_tool_call')
                ids = {x.get('id') for x in message['tool_calls'] if isinstance(x, dict)}
                returned, cursor = set(), i + 1
                while cursor < len(messages) and messages[cursor]['role'] == 'tool':
                    returned.add(messages[cursor].get('tool_call_id'))
                    cursor += 1
                require(None not in ids and ids == returned, 'tool_call_result_id_mismatch')
        envelope.update(messages=messages, train_turns=mask)
    except (ValueError, TypeError, KeyError) as exc:
        # Unknown formats and mask conflicts need review; definite empties are rejected.
        reason = str(exc)
        envelope.update(state='rejected' if reason in ('empty_question', 'empty_answer') else 'deferred',
                        issues=[reason])
    return envelope


def mechanical(envelope, rules):
    if envelope['state'] != 'candidate':
        return envelope
    messages = envelope['messages']
    ratios, partial = [], False
    for message in messages:
        for value in (message['content'], message.get('reasoning_content', '')):
            if not isinstance(value, str):
                continue
            partial |= len(value) > 200000
            tokens = re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]', value[:200000])
            n = rules['ngram_n']
            if len(tokens) >= max(n, rules['min_repetition_tokens']):
                grams = [tuple(tokens[i:i+n]) for i in range(len(tokens)-n+1)]
                ratios.append(1 - len(set(grams)) / len(grams))
    ratio = max(ratios, default=0)
    envelope['mechanical'] = {'max_ngram_repetition_ratio': round(ratio, 5),
                              'supervised_assistant_turns': sum(envelope['train_turns']),
                              'repetition_scan_partial': partial,
                              'has_tools': any(m['role'] == 'tool' or m.get('tool_calls') for m in messages)}
    if ratio >= rules['repetition_ratio']:
        envelope['state'] = 'review'
        envelope['issues'].append('possible_repetition')
    return envelope


def exact_signature(envelope):
    return sha({'messages': canonical_messages(envelope['messages']), 'train_turns': envelope['train_turns']})


def canonical_messages(messages):
    keys = {'role', 'content', 'reasoning_content', 'tool_calls', 'tool_call_id', 'name', 'function_call'}
    return [{k: v for k, v in m.items() if k in keys} for m in messages]


def prompt_signature(envelope):
    # Context before the final supervised answer, including previous assistant/tool turns.
    return sha(canonical_messages(envelope['messages'][:-1]))


def iter_jsonl(path):
    with Path(path).open('rb') as f:
        for raw in f:
            require(len(raw) <= 64 * 1024 * 1024, 'intermediate line too large')
            yield json.loads(raw)
