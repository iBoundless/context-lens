import os
import stat
import tempfile
import unittest
from pathlib import Path

try:
    from context_lens.__main__ import load_token
except ImportError:
    load_token = None


class CLITests(unittest.TestCase):
    def test_token_is_private_stable_and_does_not_overwrite_existing(self):
        self.assertIsNotNone(load_token, 'CLI token management is not implemented')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state' / 'token'
            first = load_token(path)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(load_token(path), first)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_symlink_token_is_rejected(self):
        self.assertIsNotNone(load_token, 'CLI token management is not implemented')
        with tempfile.TemporaryDirectory() as tmp:
            target, path = Path(tmp) / 'target', Path(tmp) / 'token'
            target.write_text('x' * 32)
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_token(path)
