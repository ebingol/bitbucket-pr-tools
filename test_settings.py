import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from settings import load_settings

class SettingsTests(unittest.TestCase):
    def test_environment_overrides_config_and_paths_are_relative(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'base_url':'https://example.org','output':'reports'}))
            with patch.dict(os.environ, {'PR_TOOLS_CONFIG':str(path), 'BITBUCKET_BASE_URL':'https://other.example.org'}, clear=True):
                config = load_settings()
            self.assertEqual(config['base_url'], 'https://other.example.org')
            self.assertEqual(config['output'], str(Path(folder).resolve()/'reports'))

    def test_rejects_credentials_in_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('{"token":"secret"}')
            with patch.dict(os.environ, {'PR_TOOLS_CONFIG':str(path)}, clear=True):
                with self.assertRaises(ValueError):
                    load_settings()
