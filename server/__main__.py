"""Runs the Buddy Network server, or changes someone's role.

    python -m server --dev                 # testing on this PC: ws://localhost:8765
    python -m server --behind-proxy --db /var/lib/buddy-network/network.db

    python -m server make-owner <user id> [--db ...]    # you: the owner
    python -m server set-role <user id> user|mod|admin|owner [--db ...]
    python -m server give-tag <user id> <1-99> [--db ...]   # a staff tag: #1, #2...

Roles are otherwise managed in Buddy (the owner makes admins, admins make
mods). Run the role commands with the server stopped, or restart it after - nothing is
cached, but it's the simple rule. Your user id is under Account in Buddy.

GIF search needs a GIPHY API key in the environment (see server/README.md):
GIPHY_API_KEY, and optionally GIPHY_RATING (g, pg, pg-13 - the default - or r)
and GIPHY_CALLS_PER_HOUR (default 90, under the free key's 100).

Needs Python 3.11+ and `pip install -r server/requirements.txt`.
"""

import argparse
import asyncio
import logging
import os
import sys

from .store import Store

from .admin import ROLES


def set_role_command(argv) -> int:
    ap = argparse.ArgumentParser(prog="python -m server set-role")
    ap.add_argument("user_id")
    ap.add_argument("role", choices=ROLES)
    ap.add_argument("--db", default="buddy_network.db")
    args = ap.parse_args(argv)
    store = Store(args.db)
    try:
        user = store.user(args.user_id.strip().lower())
        if user is None:
            print(f"No user {args.user_id} in {args.db}. Copy the ID from Account in Buddy.")
            return 1
        store.set_role(user["id"], args.role)
        from .common import tag_of
        print(f"{user['name'] or '(no name yet)'} #{tag_of(user['id'])} is now {args.role}.")
        return 0
    finally:
        store.close()


def give_tag_command(argv) -> int:
    """Gives someone a staff number as their tag (#1-#99): their ID moves
    into the staff range, and everything of theirs moves with it. Restart
    the server afterwards; their Buddy reconnects with the same identity
    and is told the new ID."""
    import secrets
    from .common import staff_id, tag_of
    ap = argparse.ArgumentParser(prog="python -m server give-tag")
    ap.add_argument("user_id")
    ap.add_argument("number", type=int, help="1-99: the tag they'll show (#1, #2...)")
    ap.add_argument("--db", default="buddy_network.db")
    args = ap.parse_args(argv)
    store = Store(args.db)
    try:
        user = store.user(args.user_id.strip().lower())
        if user is None:
            print(f"No user {args.user_id} in {args.db}. Copy the ID from Account in Buddy.")
            return 1
        prefix = staff_id(args.number, lambda n: "")
        holder = store.db.execute("SELECT id, name FROM users WHERE id LIKE ?", (prefix + "%",)).fetchone()
        if holder is not None:
            print(f"#{args.number} already belongs to {holder['name']} ({holder['id']}).")
            return 1
        new = staff_id(args.number, secrets.token_hex)
        store.change_user_id(user["id"], new)
        print(f"{user['name'] or '(no name yet)'} is now #{tag_of(new)} (ID {new}). "
              "Restart the server: systemctl restart buddy-network")
        return 0
    finally:
        store.close()


def run_command(argv) -> int:
    ap = argparse.ArgumentParser(prog="python -m server", description="Buddy Network chat server")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="buddy_network.db", help="SQLite database file")
    ap.add_argument("--behind-proxy", action="store_true",
                    help="running behind Caddy: read the client's address from X-Forwarded-For")
    ap.add_argument("--dev", action="store_true",
                    help="testing on one PC: no daily limit on new identities per address")
    args = ap.parse_args(argv)

    from .core import NetworkCore
    from .gifs import GiphySettings
    from .net import run

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # websockets' own INFO lines aren't needed, and keeping it quiet keeps
    # anything address-like out of the log.
    logging.getLogger("websockets").setLevel(logging.ERROR)

    store = Store(args.db)
    try:
        core = NetworkCore(store, limit_new_accounts=not args.dev, giphy=GiphySettings.from_environment(os.environ))
        asyncio.run(run(core, args.host, args.port, args.behind_proxy))
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["make-owner"]:
        return set_role_command([*argv[1:2], "owner", *argv[2:]])
    if argv[:1] == ["set-role"]:
        return set_role_command(argv[1:])
    if argv[:1] == ["give-tag"]:
        return give_tag_command(argv[1:])
    return run_command(argv)


if __name__ == "__main__":
    sys.exit(main())
