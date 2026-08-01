"""
REST -> WebSocket bridge for gpt-researcher.

WHY THIS EXISTS
---------------
The bundle's n8n workflow posts to  http://gpt-researcher:8000/report/  but the
current gptresearcher/gpt-researcher image has no such REST endpoint (404).
That image drives research only over  ws://gpt-researcher:8000/ws , and n8n's
HTTP Request node cannot speak WebSocket.

This tiny service exposes the REST shape the workflow already expects and does
the WebSocket conversation internally.

    POST /report
    {"task": "...", "report_type": "research_report", "report_source": "web"}
    ->  200 {"report": "<markdown>", "elapsed_sec": 123, "chars": 4567}

It runs on the gpt-researcher-ollama:local image, which already ships
`websockets` and `aiohttp`, so no extra image is pulled.
"""
import asyncio
import json
import logging
import os
import time

import websockets
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

GPTR_WS = os.environ.get("GPTR_WS", "ws://gpt-researcher:8000/ws")
PORT = int(os.environ.get("BRIDGE_PORT", "8100"))
# A single report takes minutes; allow a generous ceiling.
RECV_TIMEOUT = int(os.environ.get("BRIDGE_RECV_TIMEOUT", "3600"))


async def run_research(task: str, report_type: str, report_source: str) -> dict:
    request = {
        "task": task,
        "report_type": report_type,
        "report_source": report_source,
        "source_urls": [],
        "document_urls": [],
        "tone": "Objective",
        "agent": "Auto Agent",
        "query_domains": [],
    }

    started = time.time()
    chunks = []
    paths = None

    log.info("research start: %s", task[:100])
    async with websockets.connect(GPTR_WS, max_size=None, ping_interval=20, ping_timeout=60) as ws:
        await ws.send("start " + json.dumps(request, ensure_ascii=False))

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("timed out waiting for a message")
                break

            if raw == "pong":
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            mtype = msg.get("type")
            if mtype == "report":
                out = msg.get("output", "")
                if out:
                    chunks.append(out)
            elif mtype == "path":
                paths = msg.get("output", msg)
                break

    report = "".join(chunks)
    elapsed = time.time() - started
    log.info("research done in %.0fs, %d chars", elapsed, len(report))
    return {"report": report, "elapsed_sec": round(elapsed), "chars": len(report), "paths": paths}


async def handle_report(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    task = (body.get("task") or "").strip()
    if not task:
        return web.json_response({"error": "task is required"}, status=400)

    report_type = body.get("report_type") or "research_report"
    report_source = body.get("report_source") or "web"

    try:
        result = await run_research(task, report_type, report_source)
    except Exception as e:
        log.exception("research failed")
        return web.json_response({"error": f"{type(e).__name__}: {e}", "task": task}, status=500)

    if not result["report"]:
        return web.json_response({"error": "no report content produced", "task": task}, status=502)

    return web.json_response(result)


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "upstream": GPTR_WS})


def main():
    app = web.Application(client_max_size=1024 ** 2)
    app.router.add_post("/report", handle_report)
    app.router.add_post("/report/", handle_report)
    app.router.add_get("/health", handle_health)
    log.info("bridge listening on :%d -> %s", PORT, GPTR_WS)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
