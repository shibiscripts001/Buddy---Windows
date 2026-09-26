# Buddy Network server

The chat server Buddy's **Buddy Network** page connects to. It's a separate
program: not part of buddy.zip or the installer.

## Try it locally

1. Once: `pip install -r server/requirements.txt` (just `websockets`).
2. From the repo folder: `python -m server --dev`
   - It listens on `ws://localhost:8765`.
   - `--dev` lifts the limit of 3 new identities per address per day.
     Every test identity comes from the same address, so without it the fourth one
     would be refused.
   - Messages go in `buddy_network.db` in the current folder. Delete it to
     start over.
3. In Buddy, set Buddy Network's server address (Settings) to
   `ws://localhost:8765`, then open **Buddy Network** (bottom of the rail)
   and turn it on. Clear the address again to go back to the default server.
4. To chat with yourself (a second Buddy can't run on the same PC), open
   another terminal and run `python -m server.tryout --name Sam`. It joins
   #Global as a second person: type a line to send it, and it prints what
   Buddy sends. Add `--room help` for the Help room.

Stop the server with Ctrl+C.

## Making yourself the owner

1. In Buddy, open Buddy Network > Account and copy your ID.
2. `python -m server make-owner <your ID> --db buddy_network.db` (use the
   same `--db` the server runs with). The server can keep running.
3. Turn Buddy Network off and on (or restart Buddy) to pick it up: an
   **Admin** button appears, and you can make other people mods or admins
   there (Staff tab, or click their name). Admins can make mods too.

`python -m server set-role <ID> user` takes a role away again.

## Files

| File | What it does |
|---|---|
| `core.py` | All the rules: identity, names, rooms, sending, deleting, limits. No network code, so `tests/test_network_server.py` drives it directly. |
| `store.py` | SQLite tables: users, rooms, messages. No IP addresses. |
| `net.py` | The WebSocket side: passes each frame to `core.py`. |
| `__main__.py` | `python -m server` options. |
| `social.py` | Buddies, DMs, blocking, deleting an account. |
| `admin.py` | Reports, bans, roles, the admin log. |
| `common.py` | The shapes users, rooms and messages take on the wire. |
| `net.py` also | Answers `GET /announcements.json`: the app announcements behind the orb in every Buddy. |
| `tryout.py` | The terminal chatter from step 4. |

## Running your own server

`deploy/` sets up a fresh Ubuntu 24.04 machine: Caddy in front for https/wss
with a free Let's Encrypt certificate, the server as a systemd service under
its own account (with the extra limits in `deploy/hardening.conf`), a
firewall that lets in only SSH, HTTP and HTTPS, key-only SSH, automatic
security updates and a nightly database backup (the last 7 kept). No IP
addresses are logged by Caddy, the firewall or the server.

You need a domain name (e.g. `chat.example.com`) pointing at the machine, and
SSH access to it as root with a key.

1. Copy the code over, from the repo folder (bash - Git Bash on Windows):
   `bash server/deploy/push.sh root@<server address>`
2. On the server, as root:
   `bash /opt/buddy-network/server/deploy/setup.sh chat.example.com`
   It's safe to run again; every step checks what's already there.
3. In Buddy, set Buddy Network's server address (Settings) to
   `wss://chat.example.com`. The address a fresh install uses is
   `DEFAULT_SERVER_URL` in `app/core/buddy_server.py`.
4. Make yourself the owner (see above) on the server:
   `cd /opt/buddy-network && sudo -u buddynet venv/bin/python -m server make-owner <your ID> --db /var/lib/buddy-network/network.db`

To update the server later, run `push.sh` again: it copies the code and
restarts the service. Everyday commands:

| Command | What it does |
|---|---|
| `systemctl status buddy-network` | Is it running? |
| `journalctl -u buddy-network -n 100` | Its recent log. |
| `systemctl restart buddy-network` | Restart it (after a role command, say). |
| `ls /var/backups/buddy-network` | The nightly backups. |
