import asyncio
import json
import re

import autobot.config  # noqa: F401
from autobot.robot.ebo_cloud import EboCloud


async def main():
    c = EboCloud()
    for path in ("/api/v1/ebox/robots/robot", "/api/v2/users/details"):
        st, body = await c.request("GET", path)
        s = body if isinstance(body, str) else json.dumps(body)
        print(f"\n=== GET {path} -> {st} (len {len(s)}) ===")
        print(s[:500])
        for kw in ("electric", "battery", "power", "percent", "soc", "elec", "capacity", "charg", "online"):
            for m in re.finditer(r'"([^"]*' + kw + r'[^"]*)"\s*:\s*([0-9A-Za-z.\-]+)', s, re.I):
                print("  field", m.group(1), "=", m.group(2))


if __name__ == "__main__":
    asyncio.run(main())
