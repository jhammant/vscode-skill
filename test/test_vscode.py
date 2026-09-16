"""Tests for skill/scripts/vscode.py. Run: python3 -m pytest test/ (or python3 -m unittest)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill" / "scripts"))
import vscode  # noqa: E402

FAKE_CODE = """#!/bin/sh
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_CODE_LOG"
"""


class Session:
    """Builds a fake transcript, repo and `code` binary in a temp dir."""

    def __init__(self, test: unittest.TestCase):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="vscode-skill-")))
        test.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.transcript = self.base / "projects" / "p" / "sess.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.records = []
        self.clock = 0
        self.log = self.base / "code.log"
        self.code = self.base / "code"
        self.code.write_text(FAKE_CODE)
        self.code.chmod(0o755)
        patches = [
            mock.patch.dict(os.environ, {"VSCODE_BIN": str(self.code), "FAKE_CODE_LOG": str(self.log),
                                         "TMPDIR": str(self.base / "tmp")}),
            # The fixture lives under the system temp dir, so only the fake scratch dir is noise.
            mock.patch.object(vscode, "noise_roots", lambda: (str(self.base / "scratch") + os.sep,)),
            mock.patch.object(tempfile, "tempdir", str(self.base / "tmp")),
        ]
        (self.base / "tmp").mkdir()
        for p in patches:
            p.start()
            test.addCleanup(p.stop)

    def _ts(self):
        self.clock += 1
        return f"2026-09-16T10:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z"

    def file(self, rel, text="one\ntwo\nthree\n", root=None):
        path = (root or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def create(self, path, content=None):
        content = Path(path).read_text() if content is None else content
        self.records.append({"type": "user", "timestamp": self._ts(), "toolUseResult": {
            "type": "create", "filePath": path, "content": content, "structuredPatch": [],
            "originalFile": None}})

    def edit(self, path, original, new_start=2, lines=(" one", "-two", "+TWO")):
        self.records.append({"type": "user", "timestamp": self._ts(), "toolUseResult": {
            "filePath": path, "oldString": "two", "newString": "TWO", "originalFile": original,
            "structuredPatch": [{"oldStart": new_start, "oldLines": 2, "newStart": new_start,
                                 "newLines": 2, "lines": list(lines)}]}})

    def tool(self, name, cwd=None, **args):
        self.records.append({"type": "assistant", "timestamp": self._ts(), "cwd": cwd or str(self.repo),
                             "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}})

    def failed_edit(self, path):
        self.records.append({"type": "user", "timestamp": self._ts(),
                             "toolUseResult": f"Error: String to replace not found in {path}"})

    def write(self, path=None):
        target = Path(path) if path else self.transcript
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(json.dumps(r) for r in self.records) + "\n")
        self.records = []

    def run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = vscode.main([*argv, "--transcript", str(self.transcript), "--cwd", str(self.repo)])
        return code, out.getvalue()

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class CollectTests(unittest.TestCase):
    def test_created_and_edited_files_record_ops_line_and_baseline(self):
        # Arrange
        s = Session(self)
        a = s.file("a.py")
        b = s.file("b.py")
        s.create(a)
        s.edit(b, original="one\ntwo\nthree\n", new_start=10)
        s.write()

        # Act
        files = vscode.collect([str(s.transcript)])

        # Assert
        self.assertEqual(files[a].ops, {"created"})
        self.assertEqual(files[a].line, 1)
        self.assertEqual(files[b].ops, {"edited"})
        self.assertEqual(files[b].line, 11)  # one context line before the change
        self.assertEqual(files[b].baseline, "one\ntwo\nthree\n")

    def test_baseline_is_the_content_before_the_first_edit(self):
        s = Session(self)
        a = s.file("a.py")
        s.edit(a, original="v1\n")
        s.edit(a, original="v2\n")
        s.write()

        files = vscode.collect([str(s.transcript)])

        self.assertEqual(files[a].baseline, "v1\n")
        self.assertEqual(files[a].writes, 2)

    def test_failed_edits_and_reads_are_not_writes(self):
        s = Session(self)
        a = s.file("a.py")
        s.failed_edit(a)
        s.tool("Read", file_path=a)
        s.write()

        files = vscode.collect([str(s.transcript)])

        self.assertEqual(files[a].ops, {"read"})
        self.assertFalse(files[a].written)

    def test_subagent_transcripts_are_included(self):
        s = Session(self)
        main_file = s.file("main.py")
        sub_file = s.file("sub.py")
        s.create(main_file)
        s.write()
        s.create(sub_file)
        s.write(s.transcript.with_suffix("") / "subagents" / "agent-1.jsonl")

        files = vscode.collect(vscode.find_transcripts(None, str(s.transcript), str(s.repo)))

        self.assertIn(main_file, files)
        self.assertIn(sub_file, files)

    def test_bash_writes_are_detected_with_cd(self):
        s = Session(self)
        target = s.file("sub/out.txt")
        s.tool("Bash", command="cd sub && cat > out.txt <<'EOF'\nx > y\nEOF")
        s.write()

        files = vscode.collect([str(s.transcript)])

        self.assertEqual(files[target].ops, {"bash"})


class BashWritesTests(unittest.TestCase):
    CASES = [
        ("cat > out.txt <<'EOF'\nif a > b then\nx >> y\nEOF", ["/w/out.txt"]),
        ("cat <<EOF > notes.md && echo done\nhello > world\nEOF\n", ["/w/notes.md"]),
        ("ls 2>/dev/null; echo hi > /dev/null; grep x f 2>&1 | head", []),
        ("cd /repo && echo x >> log/app.log", ["/repo/log/app.log"]),
        ("cd /repo\necho x > a.txt", ["/repo/a.txt"]),
        ("echo hi | tee -a a.txt b.txt", ["/w/a.txt", "/w/b.txt"]),
        ("cp -r src/a.py dst/b.py; mv x.txt y.txt", ["/w/dst/b.py", "/w/y.txt"]),
        ("touch new.md", ["/w/new.md"]),
        ("python3 -c 'print(1 > 2)' > result.json", ["/w/result.json"]),
        ("git commit -m \"$(cat <<'EOF'\nfeat: it's > great\nEOF\n)\"", []),
        ("node -e \"a => b\" &>/tmp/log", []),
        ("unbalanced 'quote", []),
    ]

    def test_cases(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                self.assertEqual(vscode.bash_writes(command, "/w"), expected)

    def test_sed_in_place_only_counts_existing_files(self):
        base = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base)
        Path(base, "conf.ini").write_text("a=1\n")

        got = vscode.bash_writes("sed -i '' 's/a/b/' conf.ini missing.ini", base)

        self.assertEqual(got, [os.path.join(base, "conf.ini")])


class OpenTests(unittest.TestCase):
    def test_default_opens_session_files_in_repo_window_newest_first(self):
        s = Session(self)
        a = s.file("a.py")
        b = s.file("src/b.py")
        s.create(a)
        s.edit(b, original="old\n", new_start=5)
        s.write()

        code, out = s.run()

        self.assertEqual(code, 0, out)
        self.assertEqual(s.calls(), [[str(s.repo), "--goto", b + ":6", a + ":1"]])
        self.assertIn("src/b.py:6", out)

    def test_scratch_files_are_hidden_by_default_and_shown_with_all(self):
        s = Session(self)
        a = s.file("a.py")
        scratch = s.file("notes.md", root=s.base / "scratch")
        s.create(a)
        s.create(scratch)
        s.write()

        _, default_out = s.run("--list")
        _, all_out = s.run("all", "--list")

        self.assertNotIn("notes.md", default_out)
        self.assertIn("notes.md", all_out)

    def test_default_caps_at_limit_and_reports_the_rest(self):
        s = Session(self)
        paths = [s.file(f"f{i:02d}.py") for i in range(10)]
        for p in paths:
            s.create(p)
        s.write()

        code, out = s.run()

        opened = s.calls()[0][2:]
        self.assertEqual(code, 0)
        self.assertEqual(opened, [p + ":1" for p in reversed(paths[2:])])
        self.assertIn("Also touched but not opened (2", out)

    def test_last_n(self):
        s = Session(self)
        paths = [s.file(f"f{i}.py") for i in range(4)]
        for p in paths:
            s.create(p)
        s.write()

        _, out = s.run("last", "2")

        self.assertEqual(s.calls()[0][2:], [paths[3] + ":1", paths[2] + ":1"])
        self.assertNotIn("not opened", out)

    def test_selector_matches_session_file_by_fragment_and_glob(self):
        s = Session(self)
        readme = s.file("README.md")
        app = s.file("src/app.ts")
        s.create(readme)
        s.create(app)
        s.write()

        s.run("readme")
        s.run("*.ts")

        self.assertEqual(s.calls()[0][-1], readme + ":1")
        self.assertEqual(s.calls()[1][-1], app + ":1")

    def test_explicit_path_with_line(self):
        s = Session(self)
        other = s.file("docs/guide.md")
        s.write()

        code, _ = s.run("docs/guide.md:42")

        self.assertEqual(code, 0)
        self.assertEqual(s.calls()[0][-1], other + ":42")

    def test_selector_falls_back_to_repo_search(self):
        s = Session(self)
        target = s.file("lib/deep/parser.rs")
        s.file("lib/deep/lexer.rs")
        s.write()

        code, _ = s.run("parser")

        self.assertEqual(code, 0)
        self.assertEqual(s.calls()[0][-1], target)

    def test_selector_with_no_match_exits_3(self):
        s = Session(self)
        s.file("a.py")
        s.write()

        code, out = s.run("nonexistent-thing")

        self.assertEqual(code, vscode.EXIT_NO_MATCH)
        self.assertEqual(s.calls(), [])
        self.assertIn("No file matches", out)

    def test_ambiguous_repo_search_exits_4(self):
        s = Session(self)
        for i in range(12):
            s.file(f"tests/test_{i}.py")
        s.write()

        code, _ = s.run("test_")

        self.assertEqual(code, vscode.EXIT_AMBIGUOUS)
        self.assertEqual(s.calls(), [])

    def test_no_edits_falls_back_to_git_changes(self):
        s = Session(self)
        changed = s.file("changed.py")
        s.write()

        code, out = s.run()

        self.assertEqual(code, 0, out)
        self.assertEqual(s.calls()[0][-1], changed)
        self.assertIn("uncommitted git changes", out)

    def test_nothing_to_open_exits_2_and_lists_reads(self):
        s = Session(self)
        read = s.file("seen.py")
        subprocess.run(["git", "-C", str(s.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(s.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], check=True)
        s.tool("Read", file_path=read)
        s.write()

        code, out = s.run()

        self.assertEqual(code, vscode.EXIT_NOTHING)
        self.assertIn("seen.py", out)

    def test_read_keyword_includes_read_files(self):
        s = Session(self)
        read = s.file("seen.py")
        s.tool("Read", file_path=read)
        s.write()

        s.run("read", "last")

        self.assertEqual(s.calls()[0][-1], read)

    def test_diff_writes_baseline_and_invokes_code_diff(self):
        s = Session(self)
        edited = s.file("a.py", text="one\nTWO\n")
        created = s.file("new.py")
        s.edit(edited, original="one\ntwo\n")
        s.create(created)
        s.write()

        code, out = s.run("diff")

        calls = s.calls()
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[0], [str(s.repo)])
        self.assertEqual(calls[1][:2], ["--reuse-window", "--diff"])
        self.assertEqual(Path(calls[1][2]).read_text(), "one\ntwo\n")
        self.assertEqual(calls[1][2], str(s.repo / ".git" / "claude-vscode" / "a (before).py"))
        self.assertEqual(calls[1][3], edited)
        self.assertEqual(calls[2], ["--reuse-window", "--goto", created + ":1"])
        self.assertIn("a.py:3  [edited]  (diff)", out)
        self.assertIn("new.py:1  [created]  (new, no earlier version)", out)

    def test_diff_uses_git_head_for_files_changed_only_by_shell(self):
        s = Session(self)
        tracked = s.file("conf.ini", text="a=1\n")
        subprocess.run(["git", "-C", str(s.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(s.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], check=True)
        Path(tracked).write_text("a=2\n")
        s.tool("Bash", command="sed -i '' 's/1/2/' conf.ini")
        s.write()

        code, out = s.run("diff")

        left = s.calls()[1][2]
        self.assertEqual(code, 0, out)
        self.assertTrue(left.endswith("conf (HEAD).ini"))
        self.assertEqual(Path(left).read_text(), "a=1\n")

    def test_folder_and_here_keywords(self):
        s = Session(self)
        a = s.file("a.py")
        s.create(a)
        s.write()

        s.run("folder")
        s.run("here")

        self.assertEqual(s.calls()[0], [str(s.repo)])
        self.assertEqual(s.calls()[1], ["--reuse-window", "--goto", a + ":1"])

    def test_files_in_two_repos_open_in_two_windows_newest_group_last(self):
        s = Session(self)
        other_repo = s.base / "other"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
        a = s.file("a.py")
        o = s.file("o.py", root=other_repo)
        a2 = s.file("a2.py")
        s.create(a)
        s.create(o)
        s.create(a2)
        s.write()

        s.run()

        roots = [c[0] for c in s.calls()]
        self.assertEqual(roots, [str(other_repo), str(s.repo)])

    def test_missing_vscode_exits_5(self):
        s = Session(self)
        s.create(s.file("a.py"))
        s.write()

        with mock.patch.dict(os.environ, {"VSCODE_BIN": ""}), \
                mock.patch.object(vscode.shutil, "which", return_value=None), \
                mock.patch.object(vscode, "CODE_CANDIDATES", []):
            code, out = s.run()

        self.assertEqual(code, vscode.EXIT_NO_CODE)
        self.assertIn("Install", out)


if __name__ == "__main__":
    unittest.main()
