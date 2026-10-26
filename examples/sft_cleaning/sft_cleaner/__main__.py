"""Small command interface. Deployment never starts production model calls."""
import argparse
import asyncio
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .core import configured_inputs, encoded, iter_records, normalize, read_json, require
from .judge import endpoint, health

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = '/mnt/sfs/ltj/text_cleaning/config/server.json'


def edit_config(path, mutate):
    path = Path(path)
    original = path.read_bytes()
    config = json.loads(original)
    mutate(config)
    stage = path.parent / ('.' + path.name + '.stage-' + str(time.time_ns()))
    with stage.open('xb') as f:
        f.write(encoded(config))
    require(path.read_bytes() == original, 'configuration changed concurrently; edit preserved in stage')
    backup = path.parent / (path.name + '.backup-' + str(time.time_ns()))
    with backup.open('xb') as f:
        f.write(original)
    os.replace(stage, path)
    print('[sft-clean] CONFIG_UPDATED ' + json.dumps({'path': str(path), 'backup': str(backup)}), flush=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=Path(os.environ.get('SFT_CLEAN_CONFIG', DEFAULT_CONFIG)))
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('api-check')
    commands.add_parser('self-test')
    conf = commands.add_parser('configure')
    conf.add_argument('--base-url', required=True)
    conf.add_argument('--model', default='qwen3.8-27b')
    add = commands.add_parser('add-input')
    add.add_argument('--dataset', required=True)
    add.add_argument('--path', type=Path, required=True)
    add.add_argument('--split', default='train')
    add.add_argument('--hf-config', default='')
    add.add_argument('--revision', default='unknown')
    add.add_argument('--format', choices=('jsonl', 'json', 'parquet'))
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--rows-per-file', type=int, default=3)
    for name in ('mechanical', 'pilot', 'run'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--limit', type=int, default=2000 if name == 'pilot' else None)
        cmd.add_argument('--retry-uncertain', action='store_true', help='Authorize possible repeated API charges for this exact unfinished run')
    return p


def main():
    args = parser().parse_args()
    if args.command == 'self-test':
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(ROOT/'tests'), '-v'],
                                env=os.environ.copy(), timeout=180)
        return result.returncode
    if args.command == 'configure':
        endpoint({'base_url': args.base_url, 'model': args.model})
        def mutate(config):
            config['api'].update(base_url=args.base_url.rstrip('/'), model=args.model)
        edit_config(args.config, mutate)
        return 0
    if args.command == 'add-input':
        require(args.path.is_file(), 'input annotation does not exist')
        require(args.split not in ('validation', 'test', 'dev', 'val'), 'evaluation split refused')
        item = {'path': str(args.path.resolve()), 'split': args.split, 'config': args.hf_config,
                'revision': args.revision, 'format': args.format or args.path.suffix.lstrip('.')}
        def mutate(config):
            found = [x for x in config['datasets'] if x['id'] == args.dataset]
            require(len(found) == 1, 'dataset must be explicitly registered in the SFT catalog')
            source = found[0]
            require(not any(x['path'] == item['path'] for x in source['files']), 'input already registered')
            source['enabled'] = True
            source['files'].append(item)
            configured_inputs(config)
        edit_config(args.config, mutate)
        return 0
    config = read_json(args.config)
    if args.command == 'doctor':
        inputs = configured_inputs(config, allow_empty=True)
        try:
            endpoint(config['api'])
            endpoint_configured = True
        except (ValueError, TypeError):
            endpoint_configured = False
        print('[sft-clean] DOCTOR ' + json.dumps({'datatrove': importlib.metadata.version('datatrove'),
            'httpx': importlib.metadata.version('httpx'), 'registered_sources': len(config['datasets']),
            'configured_files': len(inputs), 'api_configured': endpoint_configured,
            'api_tested': False, 'production_ready': bool(inputs) and endpoint_configured,
            'no_downloads_or_model_calls': True}), flush=True)
        return 0
    if args.command == 'api-check':
        print('[sft-clean] API_CHECK ' + json.dumps(asyncio.run(health(config['api']))), flush=True)
        return 0
    if args.command == 'inspect':
        require(1 <= args.rows_per_file <= 10, 'inspect bound must be 1..10')
        for source in configured_inputs(config):
            for i, row in iter_records(source, config['max_line_bytes']):
                if i >= args.rows_per_file:
                    break
                value = normalize(row, source, i)
                print('[sft-clean] INSPECT ' + json.dumps({'source': source['dataset']['id'], 'row': i,
                    'keys': list(row) if isinstance(row, dict) else [], 'state': value['state'], 'issues': value['issues'],
                    'messages': len(value.get('messages', [])), 'train_turns': value.get('train_turns', []),
                    'qa_text_printed': False}), flush=True)
        return 0
    from .pipeline import run
    run(config, args.command, args.limit, args.retry_uncertain)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('[sft-clean] FAIL ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
