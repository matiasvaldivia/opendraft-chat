# OpenDraft Chat

A chat interface for **[OpenDraft](https://github.com/federicodeponte/opendraft)** — the open-source, 19-agent AI engine that drafts academic research papers with citations verified against CrossRef, OpenAlex, Semantic Scholar, and arXiv.

This project wraps OpenDraft as a real-time chat experience: type a research topic, watch the pipeline light up phase by phase (research → outline → writing → compile → export), and download the finished PDF / DOCX / LaTeX / Markdown.

![arch](https://img.shields.io/badge/engine-opendraft-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![stack](https://img.shields.io/badge/stack-fastapi%20%2B%20sse%20%2B%20vanilla%20js-orange)

## Features

- **Chat-style UX** — every generation is a "message" with live progress
- **Server-Sent Events streaming** — see which of the 19 agents is working right now
- **Real-time sources panel** — verified citations appear as the Scout agent finds them
- **Multi-format export** — PDF, DOCX, LaTeX, Markdown, ZIP bundle
- **Cancel button** — stop a generation mid-flight
- **Session history** — every draft stays accessible from the sidebar
- **Configurable** — paper type (research / bachelor / master / PhD), output mode (full / expose), language (11 supported), citation style (APA / IEEE / Chicago / MLA)
- **100% open-source** — MIT license, no telemetry, no external services

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          Browser (vanilla JS)                    │
│   chat UI · SSE subscriber · composer · history sidebar          │
└─────────────────────────────┬────────────────────────────────────┘
                              │  HTTP + SSE
┌─────────────────────────────▼────────────────────────────────────┐
│                FastAPI backend (backend/server.py)               │
│   /api/chat/start  /api/chat/{id}/stream  /api/chat/{id}/download│
│   /api/chat/{id}/cancel  /api/chat/history  /api/config          │
└─────────────────────────────┬────────────────────────────────────┘
                              │  in-process thread + queue
┌─────────────────────────────▼────────────────────────────────────┐
│           LocalTracker (drop-in for Supabase ProgressTracker)    │
│        captures log_activity / update_phase / log_source_found   │
└─────────────────────────────┬────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────┐
│        OpenDraft engine (opendraft/engine/draft_generator.py)     │
│   research → structure → citations → compose → validate → export │
└──────────────────────────────────────────────────────────────────┘
```

The engine's `generate_draft()` is called from a worker thread. A `LocalTracker` mimics the engine's `ProgressTracker` interface but pushes events to an in-memory queue that the SSE endpoint streams to the browser. **No Supabase or external DB required** — everything is local.

## Quick start

### 1. Prerequisites

- **Python 3.10+** (tested on 3.13)
- **Gemini API key** — free at <https://aistudio.google.com/apikey>
- ~10 GB free disk for `weasyprint` system deps (Windows users: see Troubleshooting)

## Quick start

### Prerequisites

- **Python 3.10+** (tested on 3.13)
- **Git** (the launcher auto-clones opendraft)
- **LLM API key** — Gemini direct, or any OpenAI-compatible gateway (LiteLLM, OpenRouter, Ollama, etc.)
- ~10 GB free disk for `weasyprint` system deps (Windows users: see Troubleshooting)

### One-click launch (Windows)

```cmd
start.bat
```

The launcher will, on first run:
1. Clone https://github.com/federicodeponte/opendraft into `opendraft/`
2. Create `opendraft/.env` from `.env.example` and pause so you can paste your keys
3. Create `.venv` and install backend + opendraft dependencies
4. Start the server on http://127.0.0.1:8000

### Manual launch (cross-platform)

```bash
git clone https://github.com/YOU/opendraft-chat.git
cd opendraft-chat

# Clone the vendored engine dependency (or set up a git submodule)
git clone https://github.com/federicodeponte/opendraft.git

# Copy and edit the env file
cp .env.example opendraft/.env
$EDITOR opendraft/.env    # add your OPENAI_API_KEY / GOOGLE_API_KEY etc.

# Create a venv and install
python -m venv .venv
source .venv/bin/activate              # Linux/Mac
# .venv\Scripts\activate               # Windows
pip install -r backend/requirements.txt
pip install -r opendraft/requirements.txt

# Run
cd backend
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

## API reference

The chat backend exposes a tiny HTTP API. Useful for scripting or building alternative frontends.

| Method | Path | Description |
|---|---|---|
| `GET`  | `/health` | Liveness probe + path diagnostics |
| `GET`  | `/api/config` | Paper types, languages, citation styles |
| `POST` | `/api/chat/start` | Kick off a generation, returns `{session_id}` |
| `GET`  | `/api/chat/{id}/stream` | Server-Sent Events: phase / activity / source / result / error / done |
| `GET`  | `/api/chat/{id}/status` | One-shot snapshot |
| `POST` | `/api/chat/{id}/cancel` | Cancel an in-flight generation |
| `GET`  | `/api/chat/{id}/download/{md\|pdf\|docx\|zip\|tex}` | Download artifact |
| `GET`  | `/api/chat/history` | List recent sessions |

### Start a generation

```bash
curl -X POST http://127.0.0.1:8000/api/chat/start \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "The impact of transformer architectures on clinical NLP 2020-2025",
    "paper_type": "research_paper",
    "language": "en",
    "output_type": "full",
    "citation_style": "apa"
  }'
```

```json
{ "session_id": "a1b2c3d4e5f6", "status": "started" }
```

### Stream progress

```bash
curl -N http://127.0.0.1:8000/api/chat/a1b2c3d4e5f6/stream
```

Each SSE event has an `event:` line and a JSON `data:` payload:

```
event: phase
data: {"phase":"research","progress_percent":12,"stage":"querying_crossref",...}

event: source
data: {"source":{"title":"BERT and Clinical NLP","year":2022,...}}

event: result
data: {"artifacts":{"pdf":"backend/outputs/.../thesis.pdf","docx":"..."},"word_count":14823,"citation_count":42}

event: done
data: {"status":"completed"}
```

### Download an artifact

```bash
curl -O http://127.0.0.1:8000/api/chat/a1b2c3d4e5f6/download/pdf
```

## Configuration

### Environment variables

Read from `opendraft/.env`:

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | yes (default provider) | Gemini API key |
| `ANTHROPIC_API_KEY` | no | Claude fallback |
| `OPENAI_API_KEY` | no | OpenAI fallback |
| `AI_PROVIDER` | no | `gemini` (default), `claude`, `openai` |
| `GEMINI_MODEL` | no | e.g. `gemini-2.5-flash` (default), `gemini-2.5-pro` |
| `PORT` | no | Backend port (default `8000`) |
| `HOST` | no | Backend host (default `127.0.0.1`) |

### Changing paper type / language / etc.

Use the dropdowns above the input box in the chat UI. They map directly to the engine's CLI flags:

| UI option | Engine arg |
|---|---|
| Type: Research paper | `--level research_paper` |
| Type: Bachelor | `--level bachelor` |
| Type: Master | `--level master` |
| Type: PhD | `--level phd` |
| Mode: Full draft | (default) |
| Mode: Expose only | `--expose` (~3x faster, no full body) |
| Language | `--lang es` / `de` / `fr` / `zh` etc. (57+ supported) |
| Citations | `--style apa` / `ieee` / `chicago` / `mla` |

## Troubleshooting

### `weasyprint` install fails on Windows

OpenDraft uses WeasyPrint for PDF generation, which needs GTK runtime libraries on Windows.

**Fix:** install MSYS2 + GTK, or use the `pandoc` engine fallback. See <https://doc.weasyprint.org/stable/first_steps.html#windows>.

### `ModuleNotFoundError: No module named 'utils'`

The backend auto-adds `opendraft/engine` to `sys.path`. If you run the server manually from a different directory, you may need:

```bash
cd backend
PYTHONPATH=../opendraft/engine:../opendraft python -m uvicorn server:app
```

### Generation returns "Insufficient citations found"

The engine requires a minimum number of verified citations. Try:
- Broaden the topic
- Switch language to `en` (CrossRef coverage is best)
- Try expose mode first to see if sources exist

### Generation is slow

You're calling real LLM APIs. A full master thesis can take 15-30 minutes and costs ~$0.35-$3 in API fees depending on model. Use **expose mode** for a quick smoke test.

### Cancel doesn't work

The cancel check happens between phase transitions. If the engine is mid-LLM-call, cancellation will fire as soon as that call returns (within ~30s).

## Cost expectations

| Mode | Time | Approx API cost |
|---|---|---|
| Research expose | 3-5 min | $0.05 - $0.15 |
| Research paper | 5-15 min | $0.15 - $0.50 |
| Bachelor thesis | 10-20 min | $0.30 - $1.00 |
| Master thesis | 15-30 min | $0.50 - $2.00 |
| PhD dissertation | 30-60 min | $1.00 - $3.50 |

(Costs based on Gemini Flash. Pro / Claude / GPT-4 models cost 2-10x more.)

## License

MIT. See [LICENSE](./LICENSE).

OpenDraft itself is MIT-licensed by [federicodeponte](https://github.com/federicodeponte/opendraft).

## Credits

- Engine: [federicodeponte/opendraft](https://github.com/federicodeponte/opendraft)
- Citations: CrossRef, OpenAlex, Semantic Scholar, arXiv
- PDF rendering: WeasyPrint
- DOCX export: python-docx