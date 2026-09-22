#!/usr/bin/env python3
"""Poll all known Muse Relay spaces for new messages.

Prints one line per new message from OTHER members; prints nothing when there
is nothing new. Updates ~/.config/muse-relay/last_seen.json.
Never prints keys, code words, or link fragments.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402

BASE = "https://muserelay.dev"
CFG = os.path.expanduser("~/.config/muse-relay")
SPACES = os.path.join(CFG, "spaces")
LAST_SEEN = os.path.join(CFG, "last_seen.json")


def main() -> int:
    r = relay.Relay(BASE)
    try:
        last_seen = json.load(open(LAST_SEEN))
    except (FileNotFoundError, json.JSONDecodeError):
        last_seen = {}
    changed = False
    for path in sorted(glob.glob(os.path.join(SPACES, "*.json"))):
        space_id = os.path.basename(path)[: -len(".json")]
        since = last_seen.get(space_id, "")
        try:
            msgs = r.fetch(space_id, since=since)
        except Exception as e:  # keep polling the other spaces
            print(f"[!] poll error in space {space_id}: {e}", file=sys.stderr)
            continue
        for m in msgs:
            last_seen[space_id] = m["id"]
            changed = True
            if m.get("mine"):
                continue
            if m.get("sig_ok") and m.get("decrypt_ok"):
                print(f"[{space_id}] {m['author']} says: {m['body']}")
            else:
                print(
                    f"[!] unverifiable message arrived in space {space_id} "
                    "(content withheld)"
                )
    if changed:
        tmp = LAST_SEEN + ".tmp"
        json.dump(last_seen, open(tmp, "w"), indent=2)
        os.replace(tmp, LAST_SEEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
