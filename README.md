# Sanjaya

**Hand Claude session context and tasks to another session, device, or account.**

> In the Mahabharata, Sanjaya was granted divine sight by Vyasa so he could see the distant
> battlefield of Kurukshetra and narrate all of it to the blind king Dhritarashtra.
> This tool does the same for your Claude sessions: it sees everything a session did and
> relays it, faithfully, to a Claude that was not there.

## Why

Built-in options only cover part of the problem:

| Option | Cross-device | Cross-account | Survives Claude Code upgrades | Works in claude.ai chat |
|---|---|---|---|---|
| `claude --teleport` | yes | no (same claude.ai user) | yes | no |
| Cross-session messaging | limited | no | yes | no |
| Copying `~/.claude/projects/*.jsonl` | yes | yes | no (format changes) | no |
| **Sanjaya** | **yes** | **yes** | **yes** | **yes** |

Sanjaya does not move Claude Code's internal session files. Instead it builds a **patra**
(letter): one portable Markdown file containing the goal, a briefing, the task list, files
changed, recent commands, git state (and optionally the uncommitted diff), and the recent
conversation. Any Claude can pick it up: another Claude Code install, a different
account, a teammate's machine, a cloud session, or a plain claude.ai chat window.

## Install

Requires Python 3.9+. No dependencies.

```bash
pipx install git+https://github.com/<you>/sanjaya    # or: pip install --user .
sanjaya install-commands                              # adds /handoff and /takeover to Claude Code
```

Without installing: `PYTHONPATH=path/to/sanjaya python3 -m sanjaya ...`

## Usage

### From inside Claude Code (best quality)

```
/handoff            # Claude writes a briefing, then packs it with the session data
/handoff --gist     # ...and uploads it as a secret gist
/takeover <file|url|gist-id>   # on the receiving side
```

`/handoff` asks the current Claude to write the briefing (goal, done, current state,
decisions and why, next steps, gotchas), because the session that did the work knows best
what matters.

### From the shell

```bash
sanjaya list                          # sessions for this project
sanjaya pack                          # latest session -> handoff-<id>.sanjaya.md
sanjaya pack -s 1a2b --diff           # specific session, include uncommitted diff
sanjaya pack --notes notes.md         # add your own briefing
sanjaya pack --gist                   # upload as secret gist, prints URL
sanjaya pack --encrypt -o h.enc       # passphrase-encrypted (AES-256, via openssl)

sanjaya receive handoff-1a2b.sanjaya.md            # prints a resume prompt
sanjaya receive https://gist.github.com/u/abc123 --launch   # starts `claude` with it
```

Or just open the `.sanjaya.md` file and paste it into any Claude chat.

### Transports

The packet is a plain file, so move it however you like: gist, Slack, email, scp, USB,
a shared drive, a git commit. For another account or another person, use `--gist`
(secret gist, readable by anyone with the link) together with `--encrypt` and share
the passphrase separately.

## Safety

- **Secrets are redacted by default** (Anthropic/OpenAI/GitHub/Slack/AWS/Google keys,
  private keys, `password=`/`token=` assignments, bearer headers). Use `--no-redact` to disable.
  Redaction is pattern based; review packets before sharing outside your team.
- Packets carry a SHA-256 checksum; `receive` warns if one was modified or truncated.
- `receive` compares the packet's git remote/branch/commit with your checkout and warns
  about mismatches.
- The resume prompt tells the receiving Claude to treat the packet as context, verify it
  against the real repo, and not blindly follow it.
- Uncommitted or unpushed work is flagged in the packet; push it or use `--diff`.

## Packet format

```
<!-- sanjaya:patra
{ "sanjaya": 1, "created": ..., "session_id": ..., "cwd": ...,
  "git": {"remote": ..., "branch": ..., "commit": ...}, "redacted": true, "sha256": ... }
-->
<!-- sanjaya:body -->
# Handoff: ...
```

Plain Markdown without a header is also accepted by `receive`.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
