"""A second chatter for testing on one PC, in a terminal:

    python -m server.tryout --name Sam            # joins #Global
    python -m server.tryout --name Sam --room help

Type a line and press Enter to send it; Ctrl+C to quit. Makes a new
identity every run, so start the server with --dev (see README.md).
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

from websockets.asyncio.client import connect

from .core import PROTOCOL_VERSION


def show(msg):
    kind = msg.get("type")
    if kind == "history":
        for m in msg["messages"]:
            show({"type": "message", "message": m})
    elif kind == "message":
        m = msg["message"]
        when = datetime.fromtimestamp(m["ts"]).strftime("%H:%M")
        text = "(message deleted)" if m["deleted"] else m["text"]
        print(f"[{when}] {m['author']['name']} #{m['author']['tag']}: {text}")
    elif kind == "deleted":
        print(f"(message {msg['id']} was deleted)")
    elif kind == "error":
        print(f"! {msg['code']}: {msg['message']}")


async def main():
    ap = argparse.ArgumentParser(prog="python -m server.tryout")
    ap.add_argument("--url", default="ws://localhost:8765")
    ap.add_argument("--name", default="Tester")
    ap.add_argument("--room", default="global")
    args = ap.parse_args()

    async with connect(args.url) as ws:
        await ws.send(json.dumps({"type": "hello", "v": PROTOCOL_VERSION}))
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            show(welcome)
            return
        await ws.send(json.dumps({"type": "set_name", "name": args.name}))
        await ws.send(json.dumps({"type": "join", "room": args.room}))
        print(f"Connected as {args.name} #{welcome['user']['tag']} in #{args.room}. Type to send.")

        async def reader():
            async for frame in ws:
                show(json.loads(frame))

        async def writer():
            loop = asyncio.get_running_loop()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    return
                if line.strip():
                    await ws.send(json.dumps({"type": "send", "room": args.room, "text": line.rstrip("\n")}))

        await asyncio.gather(reader(), writer())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
