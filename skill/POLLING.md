# Scheduled polling

Default cadence: every 30 minutes. Configurable per space later; for now one
global cadence.

1. List known spaces: `ls ~/.config/muse-relay/spaces/`
2. For each `<space-id>.json`, run `relay.py fetch <space-id> --since <last-id>`
   where `<last-id>` is the newest message id from the previous check. Track
   the newest id per space in `~/.config/muse-relay/last_seen.json`
   (format: `{"<space-id>": "<message-id>", ...}`).
3. If any message is new (id newer than last seen), tell the user:
   `[author's display name] says: <message>` — nothing else. Stay silent when
   there is nothing new.
4. A `[!]` (bad signature/decryption) message is not relayed as content;
   mention only that an unverifiable message arrived in that space.
