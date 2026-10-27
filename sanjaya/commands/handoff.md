---
description: Package this session's context into a Sanjaya handoff packet for another session, device or account
argument-hint: "[--gist | --encrypt | --diff | -o FILE]"
allowed-tools: Bash(sanjaya pack:*), Bash(git status:*), Bash(git log:*), Write
---

Prepare a handoff so a different Claude session (possibly on another device or another account, with none of this conversation) can continue the work.

1. Write a briefing to `/tmp/sanjaya-notes.md` with these sections, concise and concrete:
   - **Goal** - what the user ultimately wants.
   - **Done so far** - what is finished and verified, with file paths.
   - **Current state** - what is in progress, what is broken or untested right now.
   - **Key decisions and why** - choices made with the user, rejected approaches, constraints and user preferences learned in this session.
   - **Next steps** - an ordered, actionable list for the receiver.
   - **Gotchas** - anything the receiver would waste time rediscovering (env quirks, commands that work, flaky tests).
   Do not include secrets, tokens or credentials.
2. Check `git status`. If there are uncommitted or unpushed changes the receiver will need, tell the user and suggest committing and pushing, or packing with `--diff`.
3. Run: `sanjaya pack --notes /tmp/sanjaya-notes.md $ARGUMENTS`
4. Tell the user exactly what to run on the receiving side (`sanjaya receive <file-or-url> --launch`, or paste the packet into any Claude chat).
