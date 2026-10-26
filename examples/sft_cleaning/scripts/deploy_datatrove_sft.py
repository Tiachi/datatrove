"""Shared guarded Linux deployment, used by both the fork and its pinned bootstrap."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parents[1]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def direct_env(environment):
    env = environment.copy()
    disabled = []
    for key in list(env):
        if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy') or key.startswith('PIP_'):
            disabled.append(key)
            env.pop(key)
    env['PIP_CONFIG_FILE'] = os.devnull
    env['PIP_DISABLE_PIP_VERSION_CHECK'] = '1'
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Git per-child config overrides persistent proxy configuration without changing it.
    n = int(env.get('GIT_CONFIG_COUNT', '0'))
    for key in ('http.proxy', 'https.proxy', 'http.https://github.com.proxy'):
        env['GIT_CONFIG_KEY_' + str(n)] = key
        env['GIT_CONFIG_VALUE_' + str(n)] = ''
        n += 1
    env['GIT_CONFIG_COUNT'] = str(n)
    return env, disabled


def execute(command, env, *, capture=False, cwd=None, timeout=300, check=True):
    if not capture:
        print('[sft-deploy] STEP ' + json.dumps({'program': Path(command[0]).name,
              'operation': command[1:4]}, ensure_ascii=False), flush=True)
    result = subprocess.run(command, env=env, cwd=cwd, timeout=timeout,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None,
                            text=True, encoding='utf-8', errors='replace')
    if check and result.returncode:
        raise RuntimeError('command failed: ' + Path(command[0]).name + ' status=' + str(result.returncode))
    return result.stdout.strip() if capture else result.returncode


def validate(config, release):
    require(config['repository'] == 'https://github.com/Tiachi/datatrove.git', 'unexpected fork repository')
    require(config['branch'] == 'codex/sft-cleaning-v1', 'unexpected implementation branch')
    require(release['repository'] == 'Tiachi/datatrove' and release['branch'] == config['branch'], 'fork release identity mismatch')
    require(len(release['commit']) == 40 and all(x in '0123456789abcdef' for x in release['commit']), 'release SHA not published')
    require(config['upstream_commit'] == release['upstream_commit'], 'upstream baseline mismatch')
    paths = {key: Path(config[key]).expanduser().resolve() for key in ('checkout', 'venv', 'config_file', 'launcher', 'receipts')}
    require(len(set(paths.values())) == len(paths), 'deployment targets must be distinct')
    require(not any(paths[k] == paths[k].parent for k in paths), 'filesystem root is not a valid deployment target')
    require(not any(paths[k].is_relative_to(paths['checkout']) for k in paths if k != 'checkout'),
            'venv/config/runtime outputs must be outside the code checkout')
    require(not paths['checkout'].is_relative_to(paths['venv']), 'code cannot be inside venv')
    url = urlparse(config['pip_index_url'])
    require(url.scheme == 'https' and url.hostname and not url.username and not url.password and not url.query,
            'pip index must be credential-free HTTPS')
    return paths


def create_if_absent(path, raw, *, exact_existing=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.is_file() and not path.is_symlink(), 'unsafe existing deployment file')
        if exact_existing:
            require(path.read_bytes() == raw, 'existing launcher differs; preserve and inspect it')
        return False
    with path.open('xb') as f:
        f.write(raw)
    return True


def deploy(config, release, *, dry_run=False):
    paths = validate(config, release)
    env, disabled = direct_env(os.environ)
    # Presence only, never proxy URLs/passwords.
    persistent = execute(['git', 'config', '--get-regexp', '.*proxy.*'], env, capture=True, check=False, timeout=15)
    print('[sft-deploy] NETWORK ' + json.dumps({'personal_proxy': False, 'child_proxy_env_disabled': bool(disabled),
          'persistent_git_proxy_present': bool(persistent), 'global_config_modified': False}), flush=True)
    if dry_run:
        print('[sft-deploy] PLAN ' + json.dumps({'paths': {k: str(v) for k,v in paths.items()},
              'commit': release['commit'], 'data_downloads': 0, 'production_api_calls': 0}), flush=True)
        return
    require(sys.platform.startswith('linux'), 'actual deployment targets Linux; use --dry-run on other platforms')
    code = paths['checkout']
    code.parent.mkdir(parents=True, exist_ok=True)
    stage = code.parent / ('.' + code.name + '.deploy-stage')
    if not code.exists():
        if not stage.exists():
            execute(['git', 'clone', '--filter=blob:none', '--sparse', '--single-branch', '--no-tags',
                     '--branch', config['branch'], config['repository'], str(stage)], env)
        require((stage / '.git').is_dir(), 'incomplete clone stage retained; inspect before restarting')
        clone = stage
    else:
        require((code / '.git').is_dir(), 'existing checkout is not an independent git repository')
        clone = code
    require(execute(['git', '-C', str(clone), 'remote', 'get-url', 'origin'], env, capture=True) == config['repository'],
            'existing checkout origin differs')
    require(not execute(['git', '-C', str(clone), 'status', '--porcelain'], env, capture=True), 'dirty checkout; no stash/reset/clean')
    require(execute(['git', '-C', str(clone), 'branch', '--show-current'], env, capture=True) == config['branch'], 'checkout branch differs')
    execute(['git', '-C', str(clone), 'fetch', '--filter=blob:none', 'origin', config['branch']], env)
    require(execute(['git', '-C', str(clone), 'rev-parse', 'FETCH_HEAD'], env, capture=True) == release['commit'],
            'remote branch differs from reviewed release SHA; update reviewed deployment lock first')
    execute(['git', '-C', str(clone), 'merge', '--ff-only', 'FETCH_HEAD'], env)
    execute(['git', '-C', str(clone), 'sparse-checkout', 'set', 'src', 'examples/sft_cleaning'], env)
    require(execute(['git', '-C', str(clone), 'rev-parse', 'HEAD'], env, capture=True) == release['commit'], 'checkout SHA mismatch')
    execute(['git', '-C', str(clone), 'merge-base', '--is-ancestor', config['upstream_commit'], 'HEAD'], env)
    if clone == stage:
        require(not code.exists(), 'checkout appeared during deployment')
        stage.rename(code)
    venv = paths['venv']
    python = venv / 'bin/python'
    if not python.exists():
        require(not venv.exists(), 'existing incomplete venv retained; inspect rather than overwrite')
        execute([sys.executable, '-m', 'venv', str(venv)], env)
    execute([str(python), '-c', 'import sys; assert sys.version_info >= (3,10)'], env)
    app = code / 'examples/sft_cleaning'
    pip = [str(python), '-m', 'pip', '--disable-pip-version-check', '--no-input', 'install', '--no-cache-dir',
           '--timeout', '30', '--retries', '2', '--index-url', config['pip_index_url']]
    execute(pip + ['-e', str(code)], env, timeout=600)
    execute(pip + ['--only-binary=:all:', '-r', str(app / 'requirements-client.txt')], env, timeout=600)
    if config['install_parquet']:
        execute(pip + ['--only-binary=:all:', '-r', str(app / 'requirements-io.txt')], env, timeout=600)
    initialized = create_if_absent(paths['config_file'], (app / 'config/server.example.json').read_bytes())
    launcher = ('#!/usr/bin/env bash\nif [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 2; fi\n'
                'export SFT_CLEAN_PYTHON=' + shlex.quote(str(python)) + '\n'
                'export SFT_CLEAN_CONFIG=' + shlex.quote(str(paths['config_file'])) + '\n'
                'bash ' + shlex.quote(str(app / 'run.sh')) + ' "$@"\n').encode('utf-8')
    create_if_absent(paths['launcher'], launcher, exact_existing=True)
    paths['launcher'].chmod(0o755)
    env['PYTHONPATH'] = str(app)
    if config['run_offline_tests']:
        execute([str(python), '-B', '-m', 'unittest', 'discover', '-s', str(app/'tests'), '-v'], env, timeout=180)
    execute([str(python), '-B', '-m', 'sft_cleaner', '--config', str(paths['config_file']), 'doctor'], env)
    freeze = execute([str(python), '-m', 'pip', 'freeze'], env, capture=True)
    paths['receipts'].mkdir(parents=True, exist_ok=True)
    receipt = {'status': 'DEPLOY_PASS_NOT_PRODUCTION_RUN', 'repository': config['repository'],
        'branch': config['branch'], 'commit': release['commit'], 'upstream_commit': config['upstream_commit'],
        'paths': {k:str(v) for k,v in paths.items()}, 'config_created': initialized,
        'installed_packages': freeze.splitlines(), 'production_api_calls': 0, 'data_or_model_downloads': 0,
        'offline_tests_run': config['run_offline_tests']}
    save_path = paths['receipts'] / ('deploy-' + str(time.time_ns()) + '.json')
    save_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('[sft-deploy] DEPLOY_PASS ' + json.dumps({'commit': release['commit'], 'launcher': str(paths['launcher']),
          'config': str(paths['config_file']), 'receipt': str(save_path), 'production_started': False}), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=PROJECT/'config/deployment.json')
    p.add_argument('--release', type=Path, default=PROJECT/'config/fork_release.json')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--from-current-fork', action='store_true',
                   help='Deploy the already-reviewed current fork checkout; no self-referential embedded release SHA')
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.from_current_fork:
        env, _ = direct_env(os.environ)
        current = execute(['git', '-C', str(PROJECT), 'rev-parse', '--show-toplevel'], env, capture=True)
        require(execute(['git', '-C', current, 'remote', 'get-url', 'origin'], env, capture=True) == config['repository'],
                '--from-current-fork requires the actual Tiachi/datatrove checkout')
        config['checkout'] = current
        release = {'repository': 'Tiachi/datatrove', 'branch': config['branch'],
            'commit': execute(['git', '-C', current, 'rev-parse', 'HEAD'], env, capture=True),
            'upstream_commit': config['upstream_commit']}
    else:
        release = json.loads(args.release.read_text(encoding='utf-8'))
    deploy(config, release, dry_run=args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('[sft-deploy] FAIL ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
