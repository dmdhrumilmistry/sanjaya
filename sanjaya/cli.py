"""sanjaya - hand Claude session context and tasks to another session, device or account."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__
from .core import (
    build_patra,
    compare_git,
    find_session,
    list_sessions,
    parse_patra,
    parse_session,
    resume_prompt,
)

ENCRYPTED_PREFIX = "U2FsdGVkX1"  # base64 of OpenSSL's "Salted__" magic


def die(msg: str) -> None:
    print(f"sanjaya: {msg}", file=sys.stderr)
    sys.exit(1)


def _passphrase(confirm: bool) -> str:
    env = os.environ.get("SANJAYA_PASSPHRASE")
    if env:
        return env
    p = getpass.getpass("Passphrase: ")
    if confirm and p != getpass.getpass("Repeat passphrase: "):
        die("passphrases do not match")
    if not p:
        die("empty passphrase")
    return p


def _openssl(data: str, decrypt: bool) -> str:
    if not shutil.which("openssl"):
        die("encryption needs the `openssl` binary on PATH")
    args = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-a", "-A",
            "-pass", "env:SANJAYA_PASSPHRASE"]
    if decrypt:
        args.append("-d")
    env = {**os.environ, "SANJAYA_PASSPHRASE": _passphrase(confirm=not decrypt)}
    out = subprocess.run(args, input=data.encode(), capture_output=True, env=env)
    if out.returncode != 0:
        die("decryption failed (wrong passphrase?)" if decrypt else out.stderr.decode())
    return out.stdout.decode()


def cmd_list(a: argparse.Namespace) -> None:
    sessions = list_sessions(a.project)
    if not sessions:
        die(f"no Claude Code sessions recorded for {a.project}")
    for p in sessions[: a.limit]:
        s = parse_session(p)
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        first = (s.first_prompt.splitlines() or [""])[0][:70]
        print(f"{p.stem[:8]}  {when}  {len(s.turns):>4} turns  {first}")


def cmd_pack(a: argparse.Namespace) -> None:
    try:
        path = find_session(a.project, a.session)
    except FileNotFoundError as e:
        die(str(e))
    notes = ""
    if a.notes == "-":
        notes = sys.stdin.read()
    elif a.notes:
        notes = Path(a.notes).read_text()
    text = build_patra(
        parse_session(path),
        notes=notes,
        turns=a.turns,
        turn_chars=a.turn_chars,
        include_diff=a.diff,
        do_redact=not a.no_redact,
    )
    if a.encrypt:
        text = _openssl(text, decrypt=False) + "\n"

    if a.gist:
        if not shutil.which("gh"):
            die("--gist needs the GitHub CLI (`gh`) installed and logged in")
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / ("handoff.sanjaya.txt" if a.encrypt else "handoff.sanjaya.md")
            f.write_text(text)
            out = subprocess.run(
                ["gh", "gist", "create", str(f), "--desc", "Sanjaya Claude handoff"],
                capture_output=True, text=True,
            )
        if out.returncode != 0:
            die(out.stderr.strip())
        url = out.stdout.strip().splitlines()[-1]
        print(url)
        print(f"\nOn the other machine / account run:\n  sanjaya receive {url}", file=sys.stderr)
        return

    if a.output == "-":
        sys.stdout.write(text)
        return
    out_path = Path(a.output or f"handoff-{path.stem[:8]}.sanjaya.md")
    out_path.write_text(text)
    print(f"Wrote {out_path} ({len(text):,} bytes) from session {path.stem[:8]}", file=sys.stderr)
    print(f"Resume elsewhere with:  sanjaya receive {out_path}", file=sys.stderr)


GIST_RE = re.compile(r"gist\.github(?:usercontent)?\.com/(?:[^/]+/)?([0-9a-f]{20,})")


def _fetch(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    if Path(source).is_file():
        return Path(source).read_text()
    m = GIST_RE.search(source) or re.fullmatch(r"([0-9a-f]{20,})", source)
    if m and "/raw/" not in source:
        data = json.loads(_http(f"https://api.github.com/gists/{m.group(1)}"))
        files = list(data["files"].values())
        f = next((f for f in files if "sanjaya" in f["filename"]), files[0])
        return _http(f["raw_url"]) if f.get("truncated") else f["content"]
    if source.startswith(("http://", "https://")):
        return _http(source)
    die(f"cannot read '{source}' (not a file, URL or gist id)")
    return ""


def _http(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"sanjaya/{__version__}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def cmd_receive(a: argparse.Namespace) -> None:
    text = _fetch(a.source)
    if text.lstrip().startswith(ENCRYPTED_PREFIX):
        text = _openssl(text.strip(), decrypt=True)
    header, body, ok = parse_patra(text)
    if not ok:
        print("sanjaya: WARNING checksum mismatch, the packet was modified or truncated in transit",
              file=sys.stderr)
    if header:
        print(f"Packet from {header.get('from_host')} session {str(header.get('session_id'))[:8]}"
              f" created {header.get('created')}", file=sys.stderr)
        for w in compare_git(header, os.getcwd()):
            print(f"sanjaya: WARNING {w}", file=sys.stderr)

    prompt = resume_prompt(body)
    if a.launch:
        claude = shutil.which("claude")
        if not claude:
            die("`claude` not found on PATH; run without --launch and paste the prompt instead")
        os.execv(claude, [claude, prompt])
    if a.output:
        Path(a.output).write_text(prompt)
        print(f"Wrote resume prompt to {a.output}", file=sys.stderr)
    else:
        sys.stdout.write(prompt)


def cmd_install(a: argparse.Namespace) -> None:
    target = Path(a.dir).expanduser() if a.dir else (
        Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "commands")
    target.mkdir(parents=True, exist_ok=True)
    for name in ("handoff.md", "takeover.md"):
        src = resources.files("sanjaya").joinpath("commands", name).read_text()
        (target / name).write_text(src)
        print(f"Installed /{name[:-3]} -> {target / name}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="sanjaya",
        description="Hand Claude session context and tasks to another session, device or account.",
    )
    p.add_argument("--version", action="version", version=f"sanjaya {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list Claude Code sessions for a project")
    sp.add_argument("--project", default=os.getcwd(), help="project directory (default: cwd)")
    sp.add_argument("-n", "--limit", type=int, default=15)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("pack", help="create a handoff packet from a session")
    sp.add_argument("-s", "--session", help="session id prefix or .jsonl path (default: latest)")
    sp.add_argument("--project", default=os.getcwd(), help="project directory (default: cwd)")
    sp.add_argument("--notes", help="briefing written by Claude or you (file path, or - for stdin)")
    sp.add_argument("--turns", type=int, default=12, help="recent turns to include (default 12)")
    sp.add_argument("--turn-chars", type=int, default=1500, help="max chars per turn")
    sp.add_argument("--diff", action="store_true", help="include uncommitted git diff")
    sp.add_argument("--no-redact", action="store_true", help="do not mask secrets")
    sp.add_argument("--encrypt", action="store_true", help="encrypt with a passphrase (openssl)")
    sp.add_argument("--gist", action="store_true", help="upload as a secret GitHub gist")
    sp.add_argument("-o", "--output", help="output file, or - for stdout")
    sp.set_defaults(func=cmd_pack)

    sp = sub.add_parser("receive", help="turn a packet into a resume prompt")
    sp.add_argument("source", help="packet file, URL, gist URL/id, or - for stdin")
    sp.add_argument("--launch", action="store_true", help="start `claude` with the resume prompt")
    sp.add_argument("-o", "--output", help="write the resume prompt to a file")
    sp.set_defaults(func=cmd_receive)

    sp = sub.add_parser("install-commands", help="install /handoff and /takeover slash commands")
    sp.add_argument("--dir", help="commands dir (default ~/.claude/commands; use .claude/commands per repo)")
    sp.set_defaults(func=cmd_install)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
