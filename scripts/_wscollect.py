"""Temp: connect to the app WS, fire a tick, collect events for a few seconds, summarize."""
import asyncio, json, sys, collections
import httpx, websockets

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0

async def main():
    types = collections.Counter()
    thoughts, toolcalls = [], []
    async with websockets.connect("ws://127.0.0.1:8200/ws", max_size=None) as ws:
        # fire a tick to provoke a fresh reason cycle
        async with httpx.AsyncClient() as c:
            await c.post("http://127.0.0.1:8200/api/tick", timeout=30)
        try:
            end = asyncio.get_event_loop().time() + DUR
            while asyncio.get_event_loop().time() < end:
                raw = await asyncio.wait_for(ws.recv(), timeout=DUR)
                ev = json.loads(raw)
                t = ev.get("type", "?")
                types[t] += 1
                if t == "thought":
                    thoughts.append(ev.get("text", "")[:160])
                elif t == "tool_call":
                    toolcalls.append(f'{ev.get("name")} {json.dumps(ev.get("args"))[:80]}')
        except asyncio.TimeoutError:
            pass
    print("EVENT TYPES:", dict(types))
    print("\nTHOUGHTS:")
    for th in thoughts[:12]:
        print("  -", th)
    print("\nTOOL CALLS:")
    for tc in toolcalls[:12]:
        print("  -", tc)

asyncio.run(main())
