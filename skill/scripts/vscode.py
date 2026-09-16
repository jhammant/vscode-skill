#!/usr/bin/env python3
"""Open the files a Claude Code session is working on in VS Code.

Reads the session transcript (~/.claude/projects/*/<session>.jsonl plus its
subagent transcripts) to find every file created or edited, then opens the
right ones in VS Code: grouped into their project's window, jumping to the
line that last changed.

    vscode.py                 smart default: files made/edited this session
    vscode.py last [N]        the N most recently edited files (default 1)
    vscode.py all             everything touched, including scratch/tmp files
    vscode.py README '*.py'   files matching a path fragment or glob
    vscode.py --list          show candidates without opening anything
    vscode.py --diff          side-by-side diff of what this session changed

Exit codes: 0 opened/listed, 2 nothing to open, 3 a selector matched nothing,
4 a selector matched too many files, 5 VS Code not found, 6 no transcript.
No dependencies beyond the Python 3.9+ standard library.
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIMIT = 8
EXIT_OK, EXIT_NOTHING, EXIT_NO_MATCH, EXIT_AMBIGUOUS, EXIT_NO_CODE, EXIT_NO_TRANSCRIPT = 0, 2, 3, 4, 5, 6

CODE_CANDIDATES = [
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    "~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders",
    "/usr/share/code/bin/code",
    "/snap/bin/code",
]
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".next", ".cache", "target", ".pytest_cache", ".mypy_cache", "Pods", "DerivedData"}


@dataclass
class Touched:
    path: str
    ops: set = field(default_factory=set)       # created, edited, bash, read
    first: str = ""                               # timestamp of first touch
    last: str = ""                                # timestamp of last touch
    writes: int = 0
    line: int | None = None                       # line of the most recent change
    baseline: str | None = None                   # content before the session's first edit
    has_baseline: bool = False

    @property
    def written(self) -> bool:
        return bool(self.ops & {"created", "edited", "bash"})


# --------------------------------------------------------------------------- transcript

def find_transcripts(session: str | None, transcript: str | None, cwd: str) -> list[str]:
    """Main transcript first, then any subagent transcripts for the same session."""
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
    if transcript:
        main = str(Path(transcript).expanduser())
        return [main] + sorted(glob.glob(str(Path(main).with_suffix("") / "subagents" / "*.jsonl")))
    if session:
        hits = glob.glob(str(projects / "*" / f"{session}.jsonl"))
        if hits:
            main = hits[0]
            return [main] + sorted(glob.glob(str(Path(main).with_suffix("") / "subagents" / "*.jsonl")))
    # No session id: newest transcript for this working directory.
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    hits = sorted(glob.glob(str(projects / slug / "*.jsonl")), key=os.path.getmtime, reverse=True)
    if hits:
        main = hits[0]
        return [main] + sorted(glob.glob(str(Path(main).with_suffix("") / "subagents" / "*.jsonl")))
    return []


def change_line(patch) -> int | None:
    """First changed line (new-file numbering) of a structuredPatch."""
    if not patch or not isinstance(patch, list) or not isinstance(patch[0], dict):
        return None
    hunk = patch[0]
    line = hunk.get("newStart")
    if not isinstance(line, int):
        return None
    for text in hunk.get("lines") or []:
        if text[:1] in "+-":
            return max(line, 1)
        line += 1
    return max(hunk.get("newStart", 1), 1)


def collect(transcripts: list[str]) -> dict[str, Touched]:
    files: dict[str, Touched] = {}

    def touch(path: str, op: str, ts: str) -> Touched:
        path = os.path.normpath(path)
        t = files.get(path)
        if t is None:
            t = files[path] = Touched(path=path, first=ts)
        t.ops.add(op)
        if ts >= t.last:
            t.last = ts
        if op != "read":
            t.writes += 1
        return t

    records = []
    for order, name in enumerate(transcripts):
        try:
            with open(name, errors="replace") as fh:
                for idx, raw in enumerate(fh):
                    if '"tool_use"' not in raw and "toolUseResult" not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    records.append((rec.get("timestamp") or "", order, idx, rec))
        except OSError:
            continue
    records.sort(key=lambda r: (r[0], r[1], r[2]))

    for ts, _, _, rec in records:
        result = rec.get("toolUseResult")
        if isinstance(result, dict) and result.get("filePath") and (
                "structuredPatch" in result or result.get("type") in ("create", "update")):
            created = result.get("type") == "create"
            t = touch(result["filePath"], "created" if created else "edited", ts)
            if not t.has_baseline:
                original = result.get("originalFile")
                if created:
                    t.baseline, t.has_baseline = "", True
                elif isinstance(original, str):
                    t.baseline, t.has_baseline = original, True
            t.line = 1 if created else (change_line(result.get("structuredPatch")) or t.line)
            continue

        message = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(message, dict):
            continue
        cwd = rec.get("cwd") or os.getcwd()
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input") or {}
            if name == "NotebookEdit" and args.get("notebook_path"):
                touch(args["notebook_path"], "edited", ts)
            elif name == "Read" and args.get("file_path"):
                touch(args["file_path"], "read", ts)
            elif name == "Bash" and isinstance(args.get("command"), str):
                for path in bash_writes(args["command"], cwd):
                    touch(path, "bash", ts)
    return files


# --------------------------------------------------------------------------- bash parsing

HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_]\w*)\1([^\n]*)\n(?:.*?\n)?[ \t]*\2[ \t]*(?=\n|$)", re.S)
FD_REDIRECT = re.compile(r"\d*>&\d+|(?<![\w/.-])\d>>?[ \t]*[^\s;&|]+|&>>?[ \t]*[^\s;&|]+")


def bash_writes(command: str, cwd: str) -> list[str]:
    """Best-effort: files a shell command wrote (redirects, tee, sed -i, cp/mv/touch)."""
    command = HEREDOC.sub(r" \3", command)          # heredoc bodies hold arbitrary text
    command = FD_REDIRECT.sub(" ", command)          # 2>/dev/null, 2>&1, &>log
    command = command.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []

    out: list[str] = []
    here = cwd

    def resolve(p: str) -> str:
        return os.path.normpath(os.path.join(here, os.path.expanduser(p)))

    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= set(";&|()"):
            segments.append([])
        else:
            segments[-1].append(tok)

    for seg in segments:
        if not seg:
            continue
        # Redirect targets: > file, >> file
        words = []
        i = 0
        while i < len(seg):
            tok = seg[i]
            if tok in (">", ">>", ">|") and i + 1 < len(seg):
                target = seg[i + 1]
                if not target.startswith("/dev/"):
                    out.append(resolve(target))
                i += 2
                continue
            if tok in ("<", "<<", "<<<") and i + 1 < len(seg):
                i += 2
                continue
            words.append(tok)
            i += 1
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)                      # FOO=bar cmd ...
        while words and words[0] in ("sudo", "command", "env", "time", "nohup"):
            words.pop(0)
        if not words:
            continue
        cmd, rest = os.path.basename(words[0]), words[1:]
        plain = [w for w in rest if not w.startswith("-")]
        if cmd == "cd":
            here = resolve(plain[0]) if plain else os.path.expanduser("~")
        elif cmd == "tee":
            out.extend(resolve(w) for w in plain)
        elif cmd in ("sed", "gsed", "perl") and any(
                w.startswith(("-i", "--in-place")) or (cmd == "perl" and re.match(r"^-\w*i", w)) for w in rest):
            out.extend(resolve(w) for w in plain if os.path.isfile(resolve(w)))
        elif cmd in ("cp", "mv", "install") and len(plain) >= 2:
            dest = resolve(plain[-1])
            if os.path.isdir(dest):
                out.extend(os.path.join(dest, os.path.basename(s)) for s in plain[:-1])
            else:
                out.append(dest)
        elif cmd == "touch":
            out.extend(resolve(w) for w in plain)
    return out


# --------------------------------------------------------------------------- selection

def noise_roots() -> tuple[str, ...]:
    """Scratch, temp and Claude-internal locations: hidden unless asked for."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    roots = {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
             tempfile.gettempdir(), str(claude_dir)}
    return tuple(os.path.join(r, "") for r in roots)


def is_noise(path: str) -> bool:
    return path.startswith(noise_roots())


def split_line(arg: str) -> tuple[str, int | None]:
    m = re.match(r"^(.*?):(\d+)(?::\d+)?$", arg)
    if m and m.group(1):
        return m.group(1), int(m.group(2))
    return arg, None


def matches(path: str, pattern: str, cwd: str) -> bool:
    rel = os.path.relpath(path, cwd) if path.startswith(os.path.join(cwd, "")) else path
    if any(ch in pattern for ch in "*?["):
        return (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path, pattern)
                or fnmatch.fnmatch(os.path.basename(path), pattern))
    return pattern.lower() in rel.lower() or pattern.lower() in path.lower()


def repo_files(root: str, cap: int = 20000) -> list[str]:
    try:
        res = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"],
                             capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            return [os.path.join(root, p) for p in res.stdout.splitlines()[:cap]]
    except (OSError, subprocess.TimeoutExpired):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        found.extend(os.path.join(dirpath, f) for f in filenames)
        if len(found) >= cap:
            break
    return found


def git_root(path: str, cache: dict) -> str | None:
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    if directory in cache:
        return cache[directory]
    root = None
    try:
        res = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            root = res.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    cache[directory] = root
    return root


def git_changes(cwd: str) -> list[str]:
    root = git_root(cwd, {})
    if not root:
        return []
    res = subprocess.run(["git", "-C", root, "status", "--porcelain", "-uall"],
                         capture_output=True, text=True, timeout=15)
    paths = []
    for line in res.stdout.splitlines():
        entry = line[3:].split(" -> ")[-1].strip('"')
        full = os.path.join(root, entry)
        if os.path.isfile(full):
            paths.append(full)
    return sorted(paths, key=os.path.getmtime)


# --------------------------------------------------------------------------- opening

def find_code() -> str | None:
    override = os.environ.get("VSCODE_BIN")
    if override:
        return override
    for name in ("code", "code-insiders"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in CODE_CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def window_root(path: str, cwd: str, cache: dict) -> str | None:
    root = git_root(path, cache)
    if root and root not in (os.path.expanduser("~"), "/"):
        return root
    home = os.path.expanduser("~")
    if cwd not in (home, "/") and path.startswith(os.path.join(cwd, "")):
        return cwd
    return None


def safe_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path.strip("/"))


def build_commands(items: list[tuple[str, int | None, Touched | None]], args, cwd: str,
                   code: str) -> tuple[list[list[str]], set[str]]:
    """Return VS Code invocations (in run order) and the paths opened as diffs."""
    cache: dict = {}
    diffed: set[str] = set()
    window_flag = ["--new-window"] if args.new_window else (["--reuse-window"] if args.here else [])

    groups: dict[str | None, list[tuple[str, int | None, Touched | None]]] = {}
    for item in items:                                   # items arrive oldest -> newest
        root = None if args.here else window_root(item[0], cwd, cache)
        groups.setdefault(root, []).append(item)
    # Open the group holding the newest file last so its window ends up in front.
    ordered = sorted(groups.items(), key=lambda kv: items.index(kv[1][-1]))

    commands: list[list[str]] = []
    for root, group in ordered:
        if args.folder:
            if root:
                commands.append([code, *window_flag, root])
            continue
        if args.diff:
            if root:
                commands.append([code, *window_flag, root])
            plain = []
            for path, line, touched in group:
                left = baseline_file(path, touched, args)
                if left is None:
                    plain.append(f"{path}:{line or 1}")
                else:
                    commands.append([code, "--reuse-window", "--diff", left, path])
                    diffed.add(path)
            if plain:
                commands.append([code, "--reuse-window", "--goto", *plain])
            continue
        commands.extend([code, *window_flag, p] for p, _, _ in group if os.path.isdir(p))
        group = [item for item in group if not os.path.isdir(item[0])]
        if not group:
            continue
        # VS Code makes the first file the active tab, so pass the newest first.
        targets = [f"{p}:{line}" if line else p for p, line, _ in reversed(group)]
        head = [root] if root else []
        commands.append([code, *window_flag, *head, "--goto", *targets])
    return commands, diffed


def baseline_file(path: str, touched: Touched | None, args) -> str | None:
    """Write the pre-session (or git HEAD) version of a file somewhere VS Code can diff it."""
    content = None
    label = "before"
    root = git_root(path, {})
    if touched is not None and touched.has_baseline and touched.baseline:
        content = touched.baseline
    elif (touched is None or not touched.has_baseline) and root:
        rel = os.path.relpath(path, root)
        res = subprocess.run(["git", "-C", root, "show", f"HEAD:{rel}"],
                             capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            content, label = res.stdout, "HEAD"
    if content is None:
        return None
    # Inside the repo's .git dir the copy sits in the trusted workspace (no VS Code
    # "untrusted files" prompt) yet is invisible to git and the Explorer. Worktrees
    # have a .git file, not a dir, so they fall back to the temp dir.
    if root and os.path.isdir(os.path.join(root, ".git")):
        folder = Path(root, ".git", "claude-vscode", safe_name(os.path.relpath(os.path.dirname(path), root)))
    else:
        folder = (Path(tempfile.gettempdir()) / "claude-vscode" / (args.session or "nosession")
                  / safe_name(os.path.dirname(path)))
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    left = folder / f"{stem} ({label}){ext}"
    left.write_text(content)
    return str(left)


# --------------------------------------------------------------------------- main

def describe(path: str, cwd: str) -> str:
    if path.startswith(os.path.join(cwd, "")):
        return os.path.relpath(path, cwd)
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("selectors", nargs="*",
                   help="last [N] | all | read | path[:line] | path fragment | glob")
    p.add_argument("--list", action="store_true", help="print candidates, open nothing")
    p.add_argument("--json", action="store_true", help="with --list: machine-readable output")
    p.add_argument("--diff", action="store_true", help="diff each file against its pre-session version")
    p.add_argument("--folder", action="store_true", help="open only the project folder(s)")
    p.add_argument("--read", action="store_true", help="also include files only read this session")
    p.add_argument("--here", action="store_true", help="open in the last active window, no folder")
    p.add_argument("--new-window", action="store_true", help="force a new window")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="max files to open (default 8)")
    p.add_argument("--dry-run", action="store_true", help="print the VS Code commands instead of running them")
    p.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    p.add_argument("--transcript", help="explicit transcript path (testing)")
    p.add_argument("--cwd", default=os.getcwd())
    return p.parse_args(argv)


KEYWORDS = {"last", "all", "read", "diff", "folder", "here", "new"}


def main(argv=None) -> int:
    args = parse_args(argv)
    cwd = args.cwd

    transcripts = find_transcripts(args.session, args.transcript, cwd)
    touched = collect(transcripts) if transcripts else {}
    existing = {p: t for p, t in touched.items() if os.path.isfile(p)}
    by_age = sorted(existing.values(), key=lambda t: t.last)          # oldest -> newest

    # Leading keywords steer the selection: last [N], all, read, diff, folder, here, new.
    selectors = list(args.selectors)
    mode, last_n = "smart", None
    while selectors and selectors[0] in KEYWORDS:
        word = selectors.pop(0)
        if word == "last":
            mode, last_n = "last", 1
            if selectors and selectors[0].isdigit():
                last_n = int(selectors.pop(0))
        elif word == "all":
            mode = "all"
        elif word == "new":
            args.new_window = True
        else:
            setattr(args, word, True)

    def pool(include_noise: bool) -> list[Touched]:
        return [t for t in by_age
                if (t.written or args.read) and (include_noise or not is_noise(t.path))]

    chosen: list[tuple[str, int | None, Touched | None]] = []
    source = "session"
    overflow: list[str] = []

    if selectors:
        for sel in selectors:
            raw, line = split_line(sel)
            direct = os.path.normpath(os.path.join(cwd, os.path.expanduser(raw)))
            if os.path.isfile(direct):
                t = existing.get(direct)
                chosen.append((direct, line or (t.line if t else None), t))
                continue
            hits = [t for t in by_age if matches(t.path, raw, cwd)]
            hits = [t for t in hits if t.written] or hits
            if hits:
                chosen.extend((t.path, line or t.line, t) for t in hits)
                continue
            if os.path.isdir(direct):
                chosen.append((direct, None, None))
                continue
            root = git_root(cwd, {}) or cwd
            repo_hits = [p for p in repo_files(root) if matches(p, raw, root)]
            repo_hits = [p for p in repo_hits if raw.lower() in os.path.basename(p).lower()] or repo_hits
            if not repo_hits:
                print(f"No file matches '{sel}' in this session or in {describe(root, cwd)}.")
                print_candidates(pool(include_noise=True), cwd)
                return EXIT_NO_MATCH
            if len(repo_hits) > args.limit:
                print(f"'{sel}' matches {len(repo_hits)} files in {describe(root, cwd)}; be more specific:")
                for p in repo_hits[:20]:
                    print(f"  {describe(p, cwd)}")
                return EXIT_AMBIGUOUS
            chosen.extend((p, line, None) for p in repo_hits)
            source = "repo"
    else:
        candidates = pool(include_noise=(mode == "all"))
        if not candidates and mode != "all":
            changes = git_changes(cwd)
            noisy = pool(include_noise=True)
            if changes:
                source, candidates = "git", [existing.get(p) or Touched(path=p) for p in changes]
            elif noisy:
                source, candidates = "scratch", noisy
        chosen = [(t.path, t.line, t if t.path in existing else None) for t in candidates]

    # De-duplicate, keeping the latest position (newest last).
    seen: dict[str, tuple[str, int | None, Touched | None]] = {}
    for item in chosen:
        seen.pop(item[0], None)
        seen[item[0]] = item
    chosen = list(seen.values())

    limit = last_n or (len(chosen) if mode == "all" else args.limit)
    if len(chosen) > limit:
        overflow = [p for p, _, _ in chosen[:-limit]]
        chosen = chosen[-limit:]

    if not transcripts and not chosen:
        print("No Claude Code transcript found for this session "
              "(CLAUDE_CODE_SESSION_ID unset?) and no uncommitted git changes here.")
        return EXIT_NO_TRANSCRIPT

    if not chosen:
        print("Nothing to open: no files were created or edited in this session"
              + (" and there are no uncommitted git changes." if git_root(args.cwd, {}) else "."))
        read_only = [t for t in by_age if t.ops == {"read"} and not is_noise(t.path)]
        if read_only:
            print("Files read this session (use --read or name one):")
            for t in read_only[-10:][::-1]:
                print(f"  {describe(t.path, args.cwd)}")
        return EXIT_NOTHING

    if args.list:
        return list_output(chosen, overflow, args, source)

    code = find_code()
    if code is None:
        print("VS Code's `code` command was not found. Install it from VS Code: "
              "Command Palette -> 'Shell Command: Install code command in PATH', "
              "or set VSCODE_BIN to the binary.")
        return EXIT_NO_CODE

    commands, diffed = build_commands(chosen, args, args.cwd, code)
    for command in commands:
        if args.dry_run:
            print("$ " + shlex.join(command))
            continue
        res = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            print(f"VS Code exited {res.returncode}: {res.stderr.strip() or res.stdout.strip()}")
            return res.returncode

    verb = "Would open" if args.dry_run else "Opened"
    what = "the project folder for" if args.folder else ""
    origin = {"session": "", "git": " (uncommitted git changes; nothing edited this session)",
              "scratch": " (scratch files; nothing else edited this session)", "repo": ""}[source]
    print(f"{verb} {what + ' ' if what else ''}{len(chosen)} file{'s' if len(chosen) != 1 else ''} "
          f"in VS Code{origin}:")
    for path, line, t in reversed(chosen):
        tag = ", ".join(sorted(t.ops - {'read'} or t.ops)) if t else ""
        how = ("  (diff)" if path in diffed else "  (new, no earlier version)") if args.diff else ""
        print(f"  {describe(path, args.cwd)}{':' + str(line) if line else ''}"
              f"{'  [' + tag + ']' if tag else ''}{how}")
    if overflow and mode != "last":
        print(f"Also touched but not opened ({len(overflow)}; say `all` to open everything):")
        for p in reversed(overflow):
            print(f"  {describe(p, args.cwd)}")
    return EXIT_OK


def print_candidates(pool_items: list[Touched], cwd: str) -> None:
    if not pool_items:
        return
    print("Files touched this session (newest first):")
    for t in pool_items[::-1][:20]:
        print(f"  {describe(t.path, cwd)}  [{', '.join(sorted(t.ops))}]")


def list_output(chosen, overflow, args, source) -> int:
    rows = [{"path": p, "line": line, "ops": sorted(t.ops) if t else [],
             "last": t.last if t else None, "has_baseline": bool(t and t.has_baseline)}
            for p, line, t in reversed(chosen)]
    if args.json:
        print(json.dumps({"source": source, "files": rows, "not_selected": overflow[::-1]}, indent=2))
        return EXIT_OK
    print(f"Would open ({source}), newest first:")
    for row in rows:
        loc = f":{row['line']}" if row["line"] else ""
        print(f"  {describe(row['path'], args.cwd)}{loc}  [{', '.join(row['ops'])}]")
    if overflow:
        print(f"Not selected ({len(overflow)}):")
        for p in reversed(overflow):
            print(f"  {describe(p, args.cwd)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
