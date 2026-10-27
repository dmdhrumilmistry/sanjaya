"""Read Claude Code sessions and turn them into portable handoff packets ("patra").

A patra is a single Markdown file: a JSON header (machine readable) followed by a
human/LLM readable briefing. It carries no account credentials and does not depend
on Claude Code's internal session format on the receiving side, so it can be resumed
by any Claude: another Claude Code install, another device, another account, or a
plain claude.ai chat.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1
HEADER_START = "<!-- sanjaya:patra"
HEADER_END = "-->"
BODY_MARKER = "<!-- sanjaya:body -->"

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}

REDACTIONS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "[REDACTED:anthropic-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:api-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED:google-key]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED:private-key]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)(['\"]?)[^\s'\"]{6,}\3"),
     r"\1\2\3[REDACTED]\3"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]{10,}"), r"\1[REDACTED]"),
]

SYSTEM_TAG_RE = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|command-args)>"
    r"[\s\S]*?</\1>"
)


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def encode_project_dir(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def project_dir_for(cwd: str) -> Path | None:
    projects = claude_home() / "projects"
    direct = projects / encode_project_dir(cwd)
    if direct.is_dir():
        return direct
    if not projects.is_dir():
        return None
    for d in projects.iterdir():
        for f in sorted(d.glob("*.jsonl"))[:3]:
            if _session_cwd(f) == cwd:
                return d
    return None


def _session_cwd(path: Path) -> str | None:
    try:
        with path.open() as fh:
            for i, line in enumerate(fh):
                if i > 50:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def list_sessions(cwd: str) -> list[Path]:
    d = project_dir_for(cwd)
    if not d:
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def find_session(cwd: str, session_id: str | None) -> Path:
    if session_id and Path(session_id).is_file():
        return Path(session_id)
    sessions = list_sessions(cwd)
    if session_id:
        sessions = [s for s in sessions if s.stem.startswith(session_id)]
    if not sessions:
        raise FileNotFoundError(
            f"No Claude Code session found for {cwd}"
            + (f" matching '{session_id}'" if session_id else "")
        )
    return sessions[0]


@dataclass
class Session:
    session_id: str
    cwd: str | None = None
    git_branch: str | None = None
    started: str | None = None
    ended: str | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    files_edited: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)

    @property
    def first_prompt(self) -> str:
        return next((t for r, t in self.turns if r == "user"), "")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _clean(text: str) -> str:
    return SYSTEM_TAG_RE.sub("", text).strip()


def _add_unique(items: list[str], value: str) -> None:
    if value in items:
        items.remove(value)
    items.append(value)


def parse_session(path: Path) -> Session:
    s = Session(session_id=path.stem)
    with path.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("isSidechain"):
                continue
            ts = rec.get("timestamp")
            if ts:
                s.started = s.started or ts
                s.ended = ts
            s.cwd = rec.get("cwd") or s.cwd
            s.git_branch = rec.get("gitBranch") or s.git_branch
            kind = rec.get("type")
            msg = rec.get("message") or {}
            if kind == "user" and not rec.get("isMeta"):
                content = msg.get("content")
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    continue
                text = _clean(_text_of(content))
                if text:
                    s.turns.append(("user", text))
            elif kind == "assistant":
                content = msg.get("content") or []
                text = _clean(_text_of(content))
                if text:
                    s.turns.append(("assistant", text))
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name, inp = block.get("name"), block.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if name in EDIT_TOOLS and fp:
                        _add_unique(s.files_edited, fp)
                    elif name in READ_TOOLS and fp:
                        _add_unique(s.files_read, fp)
                    elif name == "Bash" and inp.get("command"):
                        s.commands.append(inp["command"])
                    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        s.todos = inp["todos"]
    return s


def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_state(cwd: str, include_diff: bool = False) -> dict | None:
    if not cwd or _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    state = {
        "remote": _git(cwd, "remote", "get-url", "origin"),
        "branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git(cwd, "rev-parse", "HEAD"),
        "status": _git(cwd, "status", "--short"),
        "unpushed": _git(cwd, "log", "--oneline", "@{u}..HEAD"),
    }
    if include_diff:
        state["diff"] = _git(cwd, "diff", "HEAD")
    return state


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _rel(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path


def build_patra(
    session: Session,
    notes: str = "",
    turns: int = 12,
    turn_chars: int = 1500,
    include_diff: bool = False,
    do_redact: bool = True,
) -> str:
    cwd = session.cwd or os.getcwd()
    git = git_state(cwd, include_diff)
    lines: list[str] = []
    add = lines.append

    title = session.first_prompt.splitlines()[0][:80] if session.first_prompt else "Claude session"
    add(f"# Handoff: {title}")
    add("")
    add("> This is a Sanjaya handoff packet. It carries the context of a Claude session so another")
    add("> Claude session (any device, any account) can continue the work. If you are the Claude")
    add("> receiving it: read everything below, verify the current state of the repo, briefly confirm")
    add("> your understanding to the user, then continue from **Next steps**.")
    add("")

    if notes.strip():
        add("## Briefing from the previous session")
        add("")
        add(notes.strip())
        add("")

    add("## Original request")
    add("")
    add(_truncate(session.first_prompt or "(none recorded)", 4000))
    add("")

    if session.todos:
        add("## Task list (last known state)")
        add("")
        marks = {"completed": "x", "in_progress": "~", "pending": " "}
        for t in session.todos:
            mark = marks.get(t.get("status", "pending"), " ")
            add(f"- [{mark}] {t.get('content') or t.get('subject') or t}")
        add("")
        pending = [t for t in session.todos if t.get("status") != "completed"]
        if pending and not notes.strip():
            add("## Next steps")
            add("")
            for t in pending:
                add(f"- {t.get('content') or t}")
            add("")

    if session.files_edited:
        add("## Files changed in the session")
        add("")
        for f in session.files_edited:
            add(f"- `{_rel(f, cwd)}`")
        add("")
    if session.files_read:
        add("## Files consulted")
        add("")
        for f in session.files_read[-25:]:
            add(f"- `{_rel(f, cwd)}`")
        add("")
    if session.commands:
        add("## Recent shell commands")
        add("")
        add("```bash")
        for c in session.commands[-15:]:
            add(_truncate(c, 300))
        add("```")
        add("")

    if git:
        add("## Repository state at handoff")
        add("")
        add(f"- remote: `{git['remote'] or '(none)'}`")
        add(f"- branch: `{git['branch']}`")
        add(f"- commit: `{git['commit']}`")
        if git["unpushed"]:
            add("- **unpushed commits** (receiver will not have these unless pushed):")
            add("```")
            add(git["unpushed"])
            add("```")
        if git["status"]:
            add("- **uncommitted changes** (receiver will not have these unless included as a diff):")
            add("```")
            add(_truncate(git["status"], 3000))
            add("```")
        if git.get("diff"):
            add("")
            add("### Uncommitted diff")
            add("")
            add("```diff")
            add(git["diff"])
            add("```")
        add("")

    recent = session.turns[-turns:] if turns > 0 else []
    if recent:
        add(f"## Recent conversation (last {len(recent)} turns)")
        add("")
        for role, text in recent:
            add(f"**{role}:**")
            add("")
            add(_truncate(text, turn_chars))
            add("")

    body = "\n".join(lines).rstrip() + "\n"
    if do_redact:
        body = redact(body)

    header = {
        "sanjaya": FORMAT_VERSION,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "from_host": socket.gethostname(),
        "session_id": session.session_id,
        "session_started": session.started,
        "session_ended": session.ended,
        "cwd": cwd,
        "git": {k: v for k, v in (git or {}).items() if k in ("remote", "branch", "commit") and v} or None,
        "redacted": do_redact,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    return f"{HEADER_START}\n{json.dumps(header, indent=2)}\n{HEADER_END}\n{BODY_MARKER}\n{body}"


def parse_patra(text: str) -> tuple[dict, str, bool]:
    """Return (header, body, checksum_ok). Accepts plain Markdown without a header too."""
    if not text.startswith(HEADER_START):
        return {}, text, True
    head, _, rest = text.partition(f"\n{HEADER_END}\n")
    header = json.loads(head[len(HEADER_START):])
    body = rest.split(BODY_MARKER + "\n", 1)[-1]
    ok = hashlib.sha256(body.encode()).hexdigest() == header.get("sha256")
    return header, body, ok


def resume_prompt(body: str) -> str:
    return (
        "You are taking over work from another Claude session via a Sanjaya handoff packet.\n"
        "Read the packet, check the repository's current state against it, summarize in 3-5 "
        "bullets what you understand the goal and remaining work to be, then continue with the "
        "next steps. Ask the user only if something is genuinely ambiguous.\n\n"
        "<handoff>\n" + body + "</handoff>\n"
    )


def compare_git(header: dict, cwd: str) -> list[str]:
    want = header.get("git") or {}
    if not want:
        return []
    have = git_state(cwd)
    if not have:
        return [f"Packet came from git repo {want.get('remote')} but {cwd} is not a git repo."]
    warnings = []
    if want.get("remote") and have["remote"] and _norm_remote(want["remote"]) != _norm_remote(have["remote"]):
        warnings.append(f"Remote differs: packet {want['remote']} vs here {have['remote']}")
    if want.get("branch") and want["branch"] != have["branch"]:
        warnings.append(f"Branch differs: packet `{want['branch']}` vs here `{have['branch']}`"
                        f" (try: git fetch && git checkout {want['branch']})")
    if want.get("commit") and want["commit"] != have["commit"]:
        warnings.append(f"Commit differs: packet {want['commit'][:10]} vs here {have['commit'][:10]}")
    return warnings


def _norm_remote(url: str) -> str:
    url = re.sub(r"^(https?://|git@|ssh://git@)", "", url.strip()).replace(":", "/")
    url = re.sub(r"^[^@/]+@", "", url)  # strip credentials or proxy users
    return url.removesuffix(".git").lower()
