#!/usr/bin/env python3
"""Muse Relay scheduled poller.

Checks every known space for messages newer than the last check.
Prints one line per new message; prints nothing when there is nothing new.
Tracks the newest message id per space in ~/.config/muse-relay/last_seen.json.
Fetching appends decrypted messages to the per-space audit log (via relay.py).

Usage:
    MUSE_RELAY_BASE=<worker-url> python3 tools/poll.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from relay import CONFIG_DIR, SPACES_DIR, Relay, load_space  # noqa: E402

LAST_SEEN = os.path.join(CONFIG_DIR, "last_seen.json")


def main() -> int:
    base = os.environ.get("MUSE_RELAY_BASE", "")
    if not base:
        print("error: set MUSE_RELAY_BASE", file=sys.stderr)
        return 2
    try:
        last_seen = json.load(open(LAST_SEEN))
    except (FileNotFoundError, json.JSONDecodeError):
        last_seen = {}
    try:
        spaces = sorted(
            f[:-5] for f in os.listdir(SPACES_DIR) if f.endswith(".json")
        )
    except FileNotFoundError:
        return 0
    r = Relay(base)
    for sid in spaces:
        try:
            load_space(sid)  # skip spaces with missing/corrupt local state
        except Exception:
            continue
        try:
            msgs = r.fetch(sid, since=last_seen.get(sid, ""))
        except Exception as e:
            if "unknown space" in str(e):
                continue  # deleted or expired server-side; audit log keeps history
            print(f"[{sid}] poll error: {e}", file=sys.stderr)
            continue
        for m in msgs:
            if m["sig_ok"] and m["decrypt_ok"]:
                print(f"[{sid}] {m['author']}: {m['body']}")
            else:
                print(f"[{sid}] [!] an unverifiable message arrived")
        if msgs:
            last_seen[sid] = max(m["id"] for m in msgs)
    with open(LAST_SEEN, "w") as f:
        json.dump(last_seen, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
