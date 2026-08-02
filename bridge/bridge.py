"""
REST -> WebSocket bridge for gpt-researcher, with optional translation.

WHY THIS EXISTS
---------------
The bundle's n8n workflow posts to  http://gpt-researcher:8000/report/  but the
current gptresearcher/gpt-researcher image has no such REST endpoint (404).
That image drives research only over  ws://gpt-researcher:8000/ws , and n8n's
HTTP Request node cannot speak WebSocket.

This service exposes the REST shape the workflow already expects and does the
WebSocket conversation internally.

    POST /report
    {"task": "...", "report_type": "research_report", "report_source": "web",
     "translate_to": "English"}          <- optional

    -> 200 {"report": "<markdown>", "report_translated": "<markdown or null>",
            "elapsed_sec": 123, "chars": 4567}

WHY TRANSLATE INSTEAD OF RESEARCHING TWICE
------------------------------------------
Running the whole pipeline once per language would double the wall time (13-22
min per topic) AND produce two reports built from independently-fetched sources,
so their numbers would not agree. Translating the finished report costs a couple
of minutes and guarantees both versions carry identical figures and citations.

The report is split on top-level markdown headings before translation. A whole
report is ~6k characters, which is uncomfortably close to the 16k context window
once the prompt and the output are counted; per-section chunks stay well clear.

It runs on the gpt-researcher-ollama:local image, which already ships
`websockets` and `aiohttp`, so no extra image is pulled.
"""
import asyncio
import json
import logging
import os
import re
import time

import websockets
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

GPTR_WS = os.environ.get("GPTR_WS", "ws://gpt-researcher:8000/ws")
PORT = int(os.environ.get("BRIDGE_PORT", "8100"))
RECV_TIMEOUT = int(os.environ.get("BRIDGE_RECV_TIMEOUT", "3600"))

# Translation goes through host Ollama directly, not through gpt-researcher.
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "qwen3:8b")
TRANSLATE_TIMEOUT = int(os.environ.get("TRANSLATE_TIMEOUT", "900"))


# --------------------------------------------------------------------------
# research
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# translation
# --------------------------------------------------------------------------

def split_sections(md: str, max_chars: int = 3000) -> list:
    """Split markdown on top-level '## ' headings, further splitting long sections."""
    parts = re.split(r"\n(?=## )", md)
    out = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        # Long section: split on blank lines, packing up to max_chars.
        buf = ""
        for para in p.split("\n\n"):
            if buf and len(buf) + len(para) + 2 > max_chars:
                out.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            out.append(buf)
    return [s for s in out if s.strip()]


TRANSLATE_PROMPT = """Translate the markdown below into {target}.

Rules:
- Output ONLY the translated markdown. No preamble, no commentary, no code fences.
- Preserve the markdown structure exactly: heading levels, lists, tables, bold.
- Do NOT translate or alter URLs, numbers, dates, percentages, currency amounts,
  ticker symbols, or product names. Copy them character for character.
- Keep citation markers such as ([Source, 2026](https://...)) exactly as they are,
  translating only the visible source name if it is not a proper noun.
- If a segment is already in {target}, return it unchanged.

Markdown to translate:
---
{chunk}
---"""


async def translate_chunk(session: ClientSession, chunk: str, target: str) -> str:
    payload = {
        "model": TRANSLATE_MODEL,
        "prompt": TRANSLATE_PROMPT.format(target=target, chunk=chunk),
        "stream": False,
        "think": False,          # qwen3 emits <think> blocks otherwise
        "options": {"temperature": 0.2},
    }
    async with session.post(f"{OLLAMA_URL}/api/generate", json=payload) as r:
        r.raise_for_status()
        data = await r.json()
    text = (data.get("response") or "").strip()
    # Strip any stray reasoning block or fence the model added anyway.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text).strip()
    return text


async def translate_markdown(md: str, target: str) -> str:
    sections = split_sections(md)
    log.info("translating %d sections to %s with %s", len(sections), target, TRANSLATE_MODEL)
    started = time.time()
    timeout = ClientTimeout(total=TRANSLATE_TIMEOUT)
    results = []
    async with ClientSession(timeout=timeout) as session:
        for i, sec in enumerate(sections, 1):
            try:
                results.append(await translate_chunk(session, sec, target))
            except Exception as e:
                log.warning("section %d/%d failed (%s) - keeping original",
                            i, len(sections), type(e).__name__)
                results.append(sec)
    log.info("translation done in %.0fs", time.time() - started)
    return "\n\n".join(results)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

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
    translate_to = (body.get("translate_to") or "").strip()

    try:
        result = await run_research(task, report_type, report_source)
    except Exception as e:
        log.exception("research failed")
        return web.json_response({"error": f"{type(e).__name__}: {e}", "task": task}, status=500)

    if not result["report"]:
        return web.json_response({"error": "no report content produced", "task": task}, status=502)

    result["report_translated"] = None
    if translate_to:
        try:
            result["report_translated"] = await translate_markdown(result["report"], translate_to)
            result["translated_chars"] = len(result["report_translated"])
        except Exception as e:
            # Translation is a bonus; never fail the whole report over it.
            log.exception("translation failed")
            result["translation_error"] = f"{type(e).__name__}: {e}"

    return web.json_response(result)


async def handle_translate(request: web.Request) -> web.Response:
    """Standalone translation, useful for back-filling existing reports."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    text = body.get("text") or ""
    target = (body.get("target") or "English").strip()
    if not text:
        return web.json_response({"error": "text is required"}, status=400)
    try:
        out = await translate_markdown(text, target)
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
    return web.json_response({"translated": out, "chars": len(out)})


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "upstream": GPTR_WS,
        "ollama": OLLAMA_URL,
        "translate_model": TRANSLATE_MODEL,
    })


def main():
    app = web.Application(client_max_size=8 * 1024 ** 2)
    app.router.add_post("/report", handle_report)
    app.router.add_post("/report/", handle_report)
    app.router.add_post("/translate", handle_translate)
    app.router.add_get("/health", handle_health)
    log.info("bridge listening on :%d -> %s (translate via %s / %s)",
             PORT, GPTR_WS, OLLAMA_URL, TRANSLATE_MODEL)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
