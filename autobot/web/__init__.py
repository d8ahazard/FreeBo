"""The web layer: a single FastAPI app that serves the UI, exposes the REST + WebSocket API the browser
uses, proxies video (WHEP/HLS) from the local mediamtx, and hosts the agent loop. See docs/ARCHITECTURE.md.
"""
