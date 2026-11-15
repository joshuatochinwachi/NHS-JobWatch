"""Unit tests for NHS-JobWatch CLI Step 3 & Step 4 offline commands."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jobwatch.cli import cmd_find_chat_id, cmd_send_sample_alert
from jobwatch.config import DEFAULT_DB_PATH
from jobwatch.storage import Storage


class TestCliOffline(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.throwaway_db = os.path.join(self.tmp_dir, "throwaway.db")
        # Initialize throwaway db with schema
        s = Storage(db_path=self.throwaway_db)
        s.close()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_send_sample_alert_refuses_default_db(self):
        """send-sample-alert strictly refuses to run on production default database (data/jobwatch.db)."""
        args = MagicMock()
        with patch.dict(os.environ, {
            "JOBWATCH_DB_PATH": DEFAULT_DB_PATH,
            "TELEGRAM_BOT_TOKEN": "mock_token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "1001,2002",
        }):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = cmd_send_sample_alert(args)
            self.assertEqual(ret, 1)
            self.assertIn("Refusing to run on production default database", stderr_buf.getvalue())

    def test_send_sample_alert_refuses_when_db_path_not_set(self):
        """send-sample-alert strictly refuses to run if JOBWATCH_DB_PATH environment variable is unset."""
        args = MagicMock()
        env = os.environ.copy()
        env.pop("JOBWATCH_DB_PATH", None)
        with patch.dict(os.environ, env, clear=True):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = cmd_send_sample_alert(args)
            self.assertEqual(ret, 1)
            self.assertIn("JOBWATCH_DB_PATH environment variable is not set", stderr_buf.getvalue())

    def test_send_sample_alert_sends_max_two_chats(self):
        """send-sample-alert caps recipients to at most 2 chats even if more are in ALLOWED_CHAT_IDS."""
        args = MagicMock()
        # Mock successful urlopen response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 999}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {
            "JOBWATCH_DB_PATH": self.throwaway_db,
            "TELEGRAM_BOT_TOKEN": "mock_token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "1001,2002,3003,4004",  # 4 allowed IDs
        }):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                ret = cmd_send_sample_alert(args)
                self.assertEqual(ret, 0)
                # Must call urlopen exactly twice (first 2 chats only)
                self.assertEqual(mock_urlopen.call_count, 2)

    def test_live_baseline_refuses_when_db_path_not_set(self):
        """live-baseline strictly refuses to run if JOBWATCH_DB_PATH is unset."""
        from jobwatch.cli import cmd_live_baseline
        args = MagicMock()
        args.scope = "Departments=9"
        args.max_requests = 10
        args.force = False

        env = os.environ.copy()
        env.pop("JOBWATCH_DB_PATH", None)
        with patch.dict(os.environ, env, clear=True):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = cmd_live_baseline(args)
            self.assertEqual(ret, 1)
            self.assertIn("JOBWATCH_DB_PATH must be explicitly set", stderr_buf.getvalue())

    def test_live_baseline_refuses_unknown_scope(self):
        """live-baseline refuses an unknown or invalid scope."""
        from jobwatch.cli import cmd_live_baseline
        args = MagicMock()
        args.scope = "invalid_scope_name"
        args.max_requests = 10
        args.force = False

        with patch.dict(os.environ, {"JOBWATCH_DB_PATH": self.throwaway_db}):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = cmd_live_baseline(args)
            self.assertEqual(ret, 1)
            self.assertIn("Unknown scope", stderr_buf.getvalue())

    def test_live_baseline_refuses_already_baselined_scope_without_force(self):
        """live-baseline refuses an already baselined scope unless --force is passed."""
        from jobwatch.cli import cmd_live_baseline
        s = Storage(db_path=self.throwaway_db)
        now = "2026-11-15T12:00:00Z"
        s.ensure_scope_metadata("Departments=9")
        with s.conn:
            s.conn.execute(
                "UPDATE scope_metadata SET is_baselined = 1, baseline_run_id = 1, baselined_at = ? WHERE scope_key = 'Departments=9';",
                (now,),
            )
        s.close()

        args = MagicMock()
        args.scope = "Departments=9"
        args.max_requests = 10
        args.force = False

        with patch.dict(os.environ, {"JOBWATCH_DB_PATH": self.throwaway_db}):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = cmd_live_baseline(args)
            self.assertEqual(ret, 1)
            self.assertIn("already baselined", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()

