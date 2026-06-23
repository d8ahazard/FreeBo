"""Enabot cloud client — signed REST for the EBO Air 2 / Max (cloud-controlled models).

The `x-ebo-sign` request signature was reverse-engineered + VERIFIED byte-for-byte against live captures
(see docs/AIR2_CLOUD.md):

    signString = METHOD & encodedPath & sortedQuery & sortedSignHeaders & bodyHash
      sortedSignHeaders = "k=v&k=v" of {x-ebo-app-type=2, x-ebo-sign-nonce, x-ebo-sign-timestamp,
                                        x-ebo-sign-version=2}, keys sorted
      bodyHash          = "" if no body, else SHA-256 hex (lowercase) of the raw JSON body
    x-ebo-sign = Base64( HMAC-SHA256( signString, headerAccessKeySecret ) )

The secret (`EBO_SIGN_SECRET`) is captured from the app (native `libeboSignature.so`), never committed.
This module is the foundation for Air 2 cloud control; the RTM/Agora transport (drive + video) builds on it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import random
import string
import time
from typing import Any, Optional
from urllib.parse import urlsplit

_NONCE_ALPHABET = string.ascii_letters + string.digits


def random_nonce(n: int = 8) -> str:
    return "".join(random.choice(_NONCE_ALPHABET) for _ in range(n))


def sign_headers(method: str, path: str, query: str, body_json: str, secret: str,
                 *, ts: Optional[str] = None, nonce: Optional[str] = None,
                 app_version: str = "2.1.1.1", os_version: str = "16") -> dict[str, str]:
    """Build the signed request headers (verified against captured traffic)."""
    ts = ts or str(int(time.time()))
    nonce = nonce or random_nonce()
    sign_hdrs = {"x-ebo-sign-nonce": nonce, "x-ebo-sign-timestamp": ts,
                 "x-ebo-sign-version": "2", "x-ebo-app-type": "2"}
    header_string = "&".join(f"{k}={sign_hdrs[k]}" for k in sorted(sign_hdrs))
    body_hash = hashlib.sha256(body_json.encode()).hexdigest() if body_json else ""
    sign_string = f"{method}&{path}&{query}&{header_string}&{body_hash}"
    mac = hmac.new(secret.encode(), sign_string.encode(), hashlib.sha256).digest()
    sign = base64.b64encode(mac).decode()
    return {**sign_hdrs, "x-ebo-sign": sign, "x-platform": "Android",
            "x-app-version": app_version, "x-os-version": os_version}


class EboCloud:
    def __init__(self, host: Optional[str] = None, secret: Optional[str] = None,
                 session_cookie: Optional[str] = None):
        self.host = (host or os.environ.get("EBO_CLOUD_HOST", "https://ebox-us.enabotserverintl.com")).rstrip("/")
        self.secret = secret or os.environ.get("EBO_SIGN_SECRET", "")
        # The user login session is a `sessionid` cookie (captured from the app's CookieJar).
        self.session_cookie = session_cookie or os.environ.get("EBO_SESSION_COOKIE", "")

    def _headers(self, method: str, url: str, body_json: str) -> dict[str, str]:
        parts = urlsplit(url)
        # query must be the sorted "k=v&k=v" the server signs; for now pass the raw query (single param cases
        # match; multi-param needs TreeMap sort — handle when we hit one).
        h = sign_headers(method, parts.path, parts.query, body_json, self.secret)
        if self.session_cookie:
            h["Cookie"] = f"sessionid={self.session_cookie}"
        return h

    async def create_session(self, robot_id: int) -> dict:
        """POST robots/session -> the live Agora RTC (video) + RTM (control) connection params for this robot.
        Returns a UI-ready dict; tokens are short-lived so the UI fetches this fresh each time."""
        status, body = await self.request("POST", "/api/v1/ebox/robots/session", json_body={"robot_id": robot_id})
        if status != 200 or not isinstance(body, dict) or body.get("code") != 200:
            return {"ok": False, "status": status, "body": body}
        d = body.get("data", {})
        rtc_token = d.get("app_rtc_token", "")
        # Agora token v006 = "006" + 32-char appId + payload.
        app_id = rtc_token[3:35] if rtc_token.startswith("006") and len(rtc_token) > 35 else ""
        # The app's RTM "login" (id 101003) carries userId = the account's ebo_id (suffix of app_rtm_uid).
        app_rtm = d.get("app_rtm_uid", "") or ""
        ebo_id = os.environ.get("EBO_EBO_ID", "") or (app_rtm.rsplit("_", 1)[-1] if "_" in app_rtm else "")
        return {
            "ok": True, "app_id": app_id, "sid": d.get("sid"), "ebo_id": ebo_id,
            "rtc": {"channel": d.get("rtc_channel"), "uid": d.get("app_rtc_uid"), "token": rtc_token,
                    "robot_uid": d.get("robot_rtc_uid")},
            "rtm": {"uid": d.get("app_rtm_uid"), "token": d.get("app_rtm_token"),
                    "robot_uid": d.get("robot_rtm_uid")},
        }

    async def request(self, method: str, path: str, *, query: str = "", json_body: Any = None) -> tuple[int, Any]:
        import httpx
        import json as _json
        body_json = "" if json_body is None else _json.dumps(json_body, separators=(",", ":"))
        url = f"{self.host}{path}" + (f"?{query}" if query else "")
        headers = self._headers(method, url, body_json)
        async with httpx.AsyncClient(timeout=15) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            else:
                r = await c.request(method, url, headers={**headers, "Content-Type": "application/json"},
                                    content=body_json or None)
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, r.text


async def _selftest():
    """Live check: authenticated cloud calls, ending with the control-session creation (which returns the
    RTM/Agora transport params needed to drive)."""
    import autobot.config  # noqa: F401  (triggers .env load)
    c = EboCloud()
    if not c.secret:
        print("no EBO_SIGN_SECRET in env (.env)"); return
    robot_id = int(os.environ.get("EBO_ROBOT_ID", "0"))

    status, body = await c.request("GET", "/api/v1/ebox/robots/robot")
    print(f"GET robots/robot -> {status}: {str(body)[:200]}\n")

    # The control session: this response carries the realtime (RTM/Agora) connection info.
    status, body = await c.request("POST", "/api/v1/ebox/robots/session", json_body={"robot_id": robot_id})
    print(f"POST robots/session({robot_id}) -> {status}:\n{body}\n")

    status, body = await c.request("GET", "/api/v1/ebox/robots/robot_members", query=f"")
    # app_token (Agora) — device_id is the app's; reuse the captured one if present
    dev = os.environ.get("EBO_DEVICE_ID", "")
    if dev:
        status, body = await c.request("GET", "/api/v2/users/app_token", query=f"device_id={dev}")
        print(f"GET users/app_token -> {status}: {str(body)[:300]}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_selftest())
