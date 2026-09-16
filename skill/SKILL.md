---
name: vscode
description: >-
  Open the files Claude is creating or editing in VS Code. By default it opens the files changed
  in this session, each in its project's window at the line that last changed. Arguments steer
  it: "last", "last 3", "all", "diff", "folder", a file name, path fragment or glob, or a
  plain-English description ("the tests", "the migration"). Use for "/vscode", "open that in
  vscode", "open what you changed", "show me the files", "let me see the diff in vscode", "open
  the readme in code".
---

# /vscode: open this session's files in VS Code

Arguments: `$ARGUMENTS`

Helper: `~/.claude/skills/vscode/scripts/vscode.py` (Python 3, standard library only). It reads
this session's transcript (`$CLAUDE_CODE_SESSION_ID`, including subagent transcripts) to find
every file that was created or edited, then runs VS Code's `code` CLI for you.

It detects:

- **Edits:** `Write`, `Edit`, `MultiEdit` and `NotebookEdit` calls that succeeded.
- **Shell writes (best effort):** `> file`, `>> file`, `tee`, `sed -i`, `perl -i`, `cp`/`mv`
  destinations and `touch`, with `cd` tracked. Heredoc bodies are ignored.

It cannot see files written by a program you ran, like a Python script, a code generator or
`npm install`. If you made files that way, name them explicitly (see below).

## 1. Turn the arguments into one command

`H=~/.claude/skills/vscode/scripts/vscode.py`

| The user said | Run |
|---|---|
| nothing | `python3 $H` |
| `last`, `last 3` | `python3 $H last` / `python3 $H last 3` |
| `all` | `python3 $H all` (every file, including scratch and temp files, no cap) |
| `diff`, "show me what you changed" | `python3 $H diff` (combine with the others: `diff last`, `diff README`) |
| `folder`, "open the project" | `python3 $H folder` |
| a name, path, `path:line` or glob: `README`, `src/app.ts:40`, `'*.test.ts'` | `python3 $H README` (quote globs) |
| `read`, "the file you were looking at" | `python3 $H read last` (includes files that were only read) |
| `here`, "in my current window" | add `here` (uses the last active window and skips the folder) |
| `new`, "in a new window" | add `new` |

Keywords come first and can be combined, for example `diff last 2` or `here read config`. Put
file names and globs after them.

**Plain English** ("the tests", "the auth changes", "what you did for the first bug"): don't
pass the words through. Run `python3 $H all --list` to see every file touched this session.
Choose the paths the user means from the list and the conversation, then run
`python3 $H <path> <path>`. Paths outside the session also work: the helper opens existing
paths directly and searches the repo for fragments. Ask only if two readings are equally
plausible.

## 2. Handle the result

| Exit | Meaning | Do |
|---|---|---|
| 0 | Opened | Tell the user in one short list: path and line for each file, plus the window if more than one project was involved. If the output lists files that were "also touched but not opened", mention the count in one line. |
| 2 | Nothing was created or edited this session, and there are no uncommitted git changes | Say so. Offer the files that were read, which the helper prints, or open what the user clearly means. |
| 3 | The selector matched nothing | Pick the intended file from the printed candidates if it's obvious. Otherwise tell the user what exists. |
| 4 | The selector matched too many repo files | Narrow it using context, or show the short list and ask. |
| 5 | VS Code's `code` CLI not found | Tell the user to run **Shell Command: Install 'code' command in PATH** from VS Code's Command Palette, or to set `VSCODE_BIN`. |
| 6 | No transcript (session id unset) | Pass the files you know you edited in this conversation explicitly. |

Summarise the output. Never paste raw `--json` output or file contents: this skill only opens
files.

## What the default does

1. **Candidates:** files created or edited this session that still exist, newest last.
   - Hidden unless asked for by name or with `all`: temp and scratchpad files (`/tmp`,
     `/private/tmp`, `/var/folders`) and Claude's own files under `~/.claude` (memory, plans).
   - If nothing was edited, it falls back to the repo's uncommitted git changes, then to
     scratch files.
2. **Cap:** it opens the 8 newest files (`--limit N` changes this) and lists the rest without
   opening them.
3. **Window:** it groups files by git root. Each group opens with `code <root> --goto
   file:line…`, so VS Code focuses the window that already has that project open, or opens
   one. Files outside any repo that sit under the session's working directory use that
   directory. Other files open on their own. The group holding the newest file opens last so
   its window ends up in front.
4. **Line:** it jumps to the first changed line of the most recent `Edit`, taken from the
   transcript's patch. New files open at line 1, and shell-written files open at the top.
5. **Diff:** the left side is the file's content before this session first touched it (from
   the transcript), or `git HEAD` for files changed only by shell commands. Files created this
   session have no earlier version, so they open normally. The left-hand copy goes in
   `<repo>/.git/claude-vscode/`: that keeps it inside the trusted workspace, so VS Code doesn't
   ask about untrusted files, and git and the Explorer ignore it. Outside a repo, and in git
   worktrees, it goes in `$TMPDIR/claude-vscode/<session>/`.

Other flags: `--list` (show what would open), `--dry-run` (print the `code` commands),
`--read`, `--limit N`, `--session ID`, `--transcript PATH`, `--cwd DIR`.

VS Code may ask "Do you want to allow untrusted files in this workspace?" when opening a file
from outside the project, such as a scratchpad or `/tmp` file. That choice belongs to the user.
Don't click through it and don't change `security.workspace.trust` settings.

If VS Code opens something wrong, run the same arguments with `--list` or `--dry-run` to see
why, and fix the selection. Don't work around it by calling `code` directly.
