import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from sanjaya import cli
from sanjaya.core import (
    build_patra,
    compare_git,
    encode_project_dir,
    find_session,
    parse_patra,
    parse_session,
    redact,
)


def _rec(kind, content, cwd, **extra):
    return {"type": kind, "cwd": cwd, "gitBranch": "main", "timestamp": "2026-10-26T10:00:00Z",
            "message": {"role": kind, "content": content}, **extra}


class SanjayaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "claude"
        self.repo = root / "repo"
        self.repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                                        capture_output=True)
        git("init", "-q", "-b", "main")
        git("remote", "add", "origin", "git@github.com:acme/widgets.git")
        (self.repo / "app.py").write_text("print(1)\n")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        (self.repo / "app.py").write_text("print(2)\n")

        cwd = str(self.repo)
        pdir = self.home / "projects" / encode_project_dir(cwd)
        pdir.mkdir(parents=True)
        records = [
            _rec("user", "Add caching to the API <system-reminder>ignore me</system-reminder>", cwd),
            _rec("assistant", [
                {"type": "text", "text": "I'll start by reading app.py."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": f"{cwd}/app.py"}},
            ], cwd),
            _rec("user", [{"type": "tool_result", "content": "print(1)"}], cwd),
            _rec("assistant", [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/app.py"}},
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "export TOKEN=sk-ant-api03-abcdefghijklmnop && pytest"}},
                {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                    {"content": "Add cache layer", "status": "completed"},
                    {"content": "Write cache tests", "status": "in_progress"},
                ]}},
            ], cwd),
            _rec("assistant", [{"type": "text", "text": "Sidechain noise"}], cwd, isSidechain=True),
            _rec("assistant", [{"type": "text", "text": "Cache added, tests next."}], cwd),
        ]
        self.session_file = pdir / "abcd1234-0000.jsonl"
        self.session_file.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n")
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_parse_session(self):
        s = parse_session(find_session(str(self.repo), None))
        self.assertEqual(s.first_prompt, "Add caching to the API")
        self.assertEqual([r for r, _ in s.turns], ["user", "assistant", "assistant"])
        self.assertEqual(s.files_edited, [f"{self.repo}/app.py"])
        self.assertEqual(s.todos[1]["status"], "in_progress")
        self.assertNotIn("Sidechain noise", str(s.turns))

    def test_patra_roundtrip_redaction_and_git(self):
        s = parse_session(self.session_file)
        text = build_patra(s, notes="## Goal\nShip caching", include_diff=True)
        header, body, ok = parse_patra(text)
        self.assertTrue(ok)
        self.assertEqual(header["git"]["branch"], "main")
        self.assertIn("Ship caching", body)
        self.assertIn("[~] Write cache tests", body)
        self.assertIn("`app.py`", body)
        self.assertIn("+print(2)", body)
        self.assertIn("uncommitted changes", body)
        self.assertNotIn("sk-ant-api03", body)
        _, _, ok = parse_patra(text.replace("Ship caching", "Ship nothing"))
        self.assertFalse(ok)

    def test_redact(self):
        self.assertEqual(redact("password=hunter2hunter"), "password=[REDACTED]")
        self.assertIn("[REDACTED:github-token]", redact("ghp_" + "a" * 36))
        self.assertEqual(redact("plain text"), "plain text")

    def test_compare_git(self):
        header = {"git": {"remote": "https://github.com/acme/widgets", "branch": "feature",
                          "commit": "0" * 40}}
        warnings = compare_git(header, str(self.repo))
        self.assertEqual(len(warnings), 2)  # remote matches after normalisation
        self.assertTrue(any("feature" in w for w in warnings))

    def test_cli_pack_and_receive(self):
        out = Path(self.tmp.name) / "h.sanjaya.md"
        with redirect_stderr(io.StringIO()):
            cli.main(["pack", "--project", str(self.repo), "-o", str(out)])
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("os.getcwd", return_value=str(self.repo)):
                cli.main(["receive", str(out)])
        self.assertIn("<handoff>", buf.getvalue())
        self.assertIn("Add caching to the API", buf.getvalue())

    def test_cli_encrypt_roundtrip(self):
        out = Path(self.tmp.name) / "h.enc"
        with mock.patch.dict(os.environ, {"SANJAYA_PASSPHRASE": "correct horse"}), \
                redirect_stderr(io.StringIO()):
            cli.main(["pack", "--project", str(self.repo), "--encrypt", "-o", str(out)])
            self.assertTrue(out.read_text().startswith(cli.ENCRYPTED_PREFIX))
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["receive", str(out)])
        self.assertIn("Write cache tests", buf.getvalue())

    def test_plain_markdown_is_accepted(self):
        header, body, ok = parse_patra("# just notes\n")
        self.assertEqual((header, body, ok), ({}, "# just notes\n", True))


if __name__ == "__main__":
    unittest.main()
