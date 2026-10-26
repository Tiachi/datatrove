import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sft_deploy', PROJECT/'scripts/deploy_datatrove_sft.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DeployTests(unittest.TestCase):
    def fixture(self):
        config = json.loads((PROJECT/'config/deployment.json').read_text(encoding='utf-8'))
        release = {'repository': 'Tiachi/datatrove', 'branch': config['branch'],
                   'commit': 'a'*40, 'upstream_commit': config['upstream_commit']}
        return config, release

    def test_proxy_disabled_only_in_child_and_git_override_not_global(self):
        original = {'HTTPS_PROXY': 'private-value', 'HTTP_PROXY': 'private-value', 'ALL_PROXY': 'private-value',
                    'PIP_INDEX_URL': 'private-index', 'KEEP_APPROVED_SETTING': 'yes'}
        result, disabled = d.direct_env(original)
        self.assertEqual(original['HTTPS_PROXY'], 'private-value')
        self.assertNotIn('HTTPS_PROXY', result)
        self.assertNotIn('PIP_INDEX_URL', result)
        self.assertEqual(result['KEEP_APPROVED_SETTING'], 'yes')
        self.assertIn('HTTPS_PROXY', disabled)
        self.assertEqual(result['GIT_CONFIG_VALUE_0'], '')

    def test_expected_repo_release_and_separate_paths(self):
        config, release = self.fixture()
        self.assertEqual(set(d.validate(config, release)), {'checkout','venv','config_file','launcher','receipts'})
        for key, value in [('repository', 'https://github.com/other/datatrove.git'),
                           ('branch', 'main'), ('config_file', config['checkout']+'/config.json')]:
            changed = dict(config, **{key:value})
            with self.assertRaises(ValueError):
                d.validate(changed, release)

    def test_no_self_referential_or_unpublished_release(self):
        config, release = self.fixture()
        release['commit'] = 'PENDING'
        with self.assertRaisesRegex(ValueError, 'release SHA'):
            d.validate(config, release)

    def test_existing_user_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            self.assertTrue(d.create_if_absent(path, b'original'))
            self.assertFalse(d.create_if_absent(path, b'new defaults'))
            self.assertEqual(path.read_bytes(), b'original')
            with self.assertRaisesRegex(ValueError, 'launcher differs'):
                d.create_if_absent(path, b'changed', exact_existing=True)

    def test_no_credentials_in_pip_url(self):
        config, release = self.fixture()
        config['pip_index_url'] = 'https://user:secret@example.com/simple'
        with self.assertRaisesRegex(ValueError, 'credential-free'):
            d.validate(config, release)


if __name__ == '__main__':
    unittest.main()
