# Local Research Factory

An unattended research pipeline that runs entirely on one desktop GPU. Drop topics
into a text file; every morning it searches the web, reads the sources, and writes
a cited markdown report for each one.

**Zero API cost.** No OpenAI key, no Tavily key, no search API. The only thing
leaving the machine is the web search itself — every token of inference happens
locally.

```
n8n (daily 07:00) ──► researcher-bridge ──► GPT Researcher ──► SearxNG (web search)
                                                  │
                                                  └──► Ollama + Qwen3  (host GPU)

                            output ──► files/reports/2026-08-01_<topic>.md
```

| Component | Role | Runs on |
|---|---|---|
| Ollama + Qwen3 30B/8B | writing, summarising, embeddings | Windows host, GPU |
| SearxNG | free metasearch, no API key | Docker |
| GPT Researcher | topic → sourced report | Docker |
| researcher-bridge | REST→WebSocket shim (see below) | Docker |
| n8n | topic loop + scheduling | Docker |

Built and verified on: **Ryzen 5 7600 / 32GB DDR5 / RTX 4070 Ti 12GB**, Windows 11,
Docker Engine inside WSL2 Ubuntu 26.04.

Throughput: **13–22 minutes per topic**. Qwen3-30B doesn't fit in 12GB of VRAM so it
spills into system RAM, but it's a mixture-of-experts model with ~3B active
parameters, which keeps it usable.

---

## What the output looks like

Reports are markdown with inline citations and a reference list. A real excerpt:

> 2026년 7월 30일, 미국 현물 비트코인 ETF 시장은 목요일 하루 동안 **2억 3,310만 달러**의
> 순유입을 기록하며, 3주 만에 가장 큰 일간 유입 규모를 달성했다.
> 이번 일일 유입 중 **블랙록 IBIT**는 **1억 8,340만 달러(78.7%)**를 유입하며 전체 유입의
> 5분의 4를 책임졌다 ([Yellow.com, 2026](...)).

Output language is configurable (`LANGUAGE` in `docker-compose.yml`); this build is
set to Korean.

---

## Quick start

Requirements: Windows 10/11, NVIDIA GPU with recent drivers, ~40GB free disk
(25GB of that is model weights), WSL2.

```powershell
git clone <this repo> C:\local-research-factory
cd C:\local-research-factory
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` is re-runnable and exits partway through when it needs a new shell or a
reboot. Run it again until you see `=== DONE ===`.

Then:

1. Edit `files/topics.txt` — one topic per line, `#` for comments
2. Open http://localhost:5678, create a local n8n account
3. Import `n8n/research-loop.json`
4. Run it once manually, then flip **Active** on for the daily 07:00 schedule

Reports land in `files/reports/`.

| Service | URL |
|---|---|
| GPT Researcher (manual research UI) | http://localhost:8000 |
| n8n (loop + schedule) | http://localhost:5678 |
| SearxNG | http://localhost:8888 |

---

## Six things that were broken, and how they were fixed

The interesting part of this project wasn't wiring the components together — it was
that **almost nothing worked out of the box**. Current upstream images have drifted
from what the tutorials assume. Documenting these because every one of them cost
hours:

### 1. The GPT Researcher image ships without Ollama support

`docker-compose.yml` sets `EMBEDDING=ollama:...` and `SMART_LLM=ollama:...`, but the
official image has no `langchain-ollama` package. Every run died instantly with
`ModuleNotFoundError: No module named 'langchain_ollama'`.

Fixed in `Dockerfile.gpt-researcher`. Version pinning matters: a plain
`pip install langchain-ollama` pulls `langchain-core` 1.x and breaks the image's
pinned langchain 0.3.20 ecosystem. Pin to the 0.2.x line.

### 2. There is no `POST /report/` endpoint

Most tutorials show a REST call to `/report/`. The current image returns **404** — it
only accepts research over a WebSocket at `/ws`, using a `start <json>` text frame.
n8n's HTTP Request node can't speak WebSocket.

`bridge/bridge.py` is a ~100-line service that exposes the REST shape everything
expects and does the WebSocket conversation internally. It reuses the same image, so
there's no extra download.

### 3. The scraper silently failed on every URL — and the model made things up

This one is the reason to be careful with local research agents.

The default scraper resolved to Firecrawl, whose installed version has a different
signature than the code calls:
`FirecrawlClient.scrape() got an unexpected keyword argument 'params'`. Every single
URL failed. Search worked fine and returned real links; the scraper then read none of
them.

**The pipeline did not report an error.** It produced a polished, well-structured
report with ten footnotes — entirely fabricated. It cited URLs that don't exist
(`fidelity.com/ibit`), attributed BlackRock's IBIT to Fidelity, and invented an SEC
rule requiring a $10,000 minimum retail investment.

Fixed with `SCRAPER=bs` (BeautifulSoup). After the fix the same query pulled 4–5 pages
per sub-query and context size went from **2 characters to 52,108**.

**How to check your own runs:**

```bash
docker compose logs gpt-researcher | grep "Context size"
```

Tens of thousands means it read sources. Single digits means the model is improvising.

### 4. `LANGUAGE` defaults to english and overrides your prompt

Appending "write the report in Korean" to the topic does nothing — the config wins,
and the model will even write a paragraph explaining that it was instructed to use
English. Set `LANGUAGE` in `docker-compose.yml` instead.

### 5. n8n blocks filesystem access by default

The workflow's Read/Write File nodes fail immediately with
`Access to the file is not allowed.` Needs `N8N_RESTRICT_FILE_ACCESS_TO=/files`.

### 6. PowerShell 5.1 corrupts the SearxNG config on non-English Windows

`Get-Content` reads BOM-less files using the ANSI codepage. On Korean Windows
(cp949), round-tripping `searxng/settings.yml` through
`Get-Content` → `Set-Content -Encoding UTF8` mangles the comments into mojibake, adds
a BOM, and merges lines — which comments out `secret_key:` and leaves SearxNG
crash-looping on a YAML parse error.

`setup.ps1` here uses `[System.IO.File]::ReadAllText/WriteAllText` with an explicit
UTF-8-no-BOM encoding.

---

## A note on Docker Desktop

This runs on **Docker Engine inside WSL2**, not Docker Desktop.

Docker Desktop was installed first and could never be made to start. Launching it
once before WSL2 existed left behind half-created AF_UNIX socket files that Windows
marks as reparse points — undeletable by Docker itself, by `Remove-Item`, and by
`cmd del`. Its backend crashed on startup every time trying to remove one. Reinstall
and factory reset both failed.

Running Docker natively in WSL2 sidesteps the entire class of problem. Two
consequences:

- Containers can't reach the Windows host via `host.docker.internal` automatically.
  `docker-compose.yml` maps it to the WSL gateway IP. **Check yours** with
  `wsl -d Ubuntu -- ip route show default` and update the `extra_hosts` line — the
  value in this repo is machine-specific.
- Ollama needs `OLLAMA_HOST=0.0.0.0` plus a firewall rule allowing inbound 11434 from
  the private WSL subnet (`172.16.0.0/12`). The LAN IP does *not* work; Windows
  Firewall drops WSL→host traffic to it.

WSL2 also tears down its VM when no process is running inside the distro, which kills
dockerd and every container. A logon-triggered scheduled task running
`wsl -d Ubuntu -u root -- sleep infinity` keeps it alive. Without it the daily
schedule silently never fires.

---

## Caveats

**Do not trust figures or citations without opening the source.** Even with scraping
working, a 30B local model summarising a dozen pages will occasionally garble a
number or attribute a quote to the wrong entity. Treat reports as a well-organised
starting point, not as a verified document. Section 3 above is what happens in the
worst case.

**Search engines block aggressively.** SearxNG queries a dozen engines; some will
return 403 or a CAPTCHA redirect at any given moment. This is normal — the aggregate
still returns results. If you're on a VPN, expect more blocks.

**Tuning.** Too slow: switch `SMART_LLM` to `ollama:qwen3:14b`, which fits entirely in
12GB and runs 2–3× faster at somewhat lower quality. Want more depth: change
`report_type` to `detailed_report` in the workflow (2–3× slower).

---

## Security

Everything binds to localhost. **Do not port-forward 8000/5678/8888/8100** — none of
these services have authentication. If you need remote access, use a VPN like
Tailscale.

The firewall rule for Ollama is scoped to the private WSL subnet only; it does not
expose the model to your LAN or the internet.

---

## License

The components are their own: Ollama, SearxNG, GPT Researcher, n8n (community
edition), and Qwen3 (Apache 2.0) are all open source. The glue in this repo —
`bridge/`, the Dockerfile, the compose file, the workflow, and `setup.ps1` — is MIT.
