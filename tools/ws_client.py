"""
Minimal WebSocket client for this gpt-researcher image.
This image exposes research ONLY via ws://localhost:8000/ws (no REST /report/).
Protocol: send  'start ' + JSON, then read streamed JSON messages until type == 'path'.
"""
import asyncio
import json
import sys
import time

import websockets

TASK = sys.argv[1] if len(sys.argv) > 1 else "test"
REPORT_TYPE = sys.argv[2] if len(sys.argv) > 2 else "research_report"
OUT = sys.argv[3] if len(sys.argv) > 3 else "/usr/src/app/outputs/_e2e_test.md"

REQUEST = {
    "task": TASK,
    "report_type": REPORT_TYPE,
    "report_source": "web",
    "source_urls": [],
    "document_urls": [],
    "tone": "Objective",
    "agent": "Auto Agent",
    "query_domains": [],
}


async def main():
    url = "ws://localhost:8000/ws"
    started = time.time()
    report_chunks = []
    final_paths = None
    n_msgs = 0
    last_beat = 0.0

    print(f"connecting to {url}", flush=True)
    async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=60) as ws:
        await ws.send("start " + json.dumps(REQUEST, ensure_ascii=False))
        print(f"sent start: {TASK[:80]}", flush=True)

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1800)
            except asyncio.TimeoutError:
                print("TIMEOUT waiting for message", flush=True)
                break

            n_msgs += 1
            if raw == "pong":
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            mtype = msg.get("type")

            if mtype == "report":
                chunk = msg.get("output", "")
                if chunk:
                    report_chunks.append(chunk)
            elif mtype == "path":
                final_paths = msg.get("output", msg)
                print("received final path message", flush=True)
                break
            elif mtype == "logs":
                el = time.time() - started
                if el - last_beat > 30:
                    last_beat = el
                    out = str(msg.get("output", ""))[:110].replace("\n", " ")
                    print(f"[{el:6.0f}s] {out}", flush=True)

    elapsed = time.time() - started
    report = "".join(report_chunks)
    print(f"=== DONE in {elapsed:.0f}s, messages={n_msgs}, report_chars={len(report)} ===", flush=True)
    if final_paths:
        print("paths:", json.dumps(final_paths, ensure_ascii=False)[:400], flush=True)

    if report:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"saved: {OUT}", flush=True)
    else:
        print("NO REPORT CONTENT CAPTURED", flush=True)


asyncio.run(main())
