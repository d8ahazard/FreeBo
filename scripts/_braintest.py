import time

import httpx

B = "http://127.0.0.1:8200"
for i in range(8):
    try:
        r = httpx.post(B + "/api/tick", timeout=40).json()
    except Exception as e:
        print(f"#{i} ERR {e}", flush=True)
        time.sleep(2)
        continue
    print(f"#{i} action={r.get('action')!r} spoken={r.get('spoken')!r} "
          f"actions={[a.get('name') for a in (r.get('actions') or [])]} ok={r.get('ok')}", flush=True)
    time.sleep(2)
