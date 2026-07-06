# Arquitectura de OpenDraft Chat

> Documento técnico: cómo está armado el sistema, qué hace cada componente, cómo interactúan y dónde tocar para extender.

---

## Vista de capas

```
┌──────────────────────────────────────────────────────────────────────┐
│  Capa 4: Navegador                                                   │
│  ─────────────────                                                   │
│  frontend/index.html  ·  style.css  ·  app.js                        │
│  - DOM estático servido por FastAPI                                  │
│  - Vanilla JS, sin framework ni build step                           │
│  - EventSource para recibir progreso en vivo                         │
└──────────────────────────────────────────────────────────────────────┘
                                  │ HTTP + SSE
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Capa 3: API HTTP                                                    │
│  ─────────────────                                                   │
│  backend/server.py  (FastAPI)                                        │
│  - Endpoints REST y SSE                                              │
│  - Sesiones in-memory (en prod → Redis/Postgres)                     │
│  - Mount estático de frontend/                                       │
│  - Lanza generate_draft() en thread aparte                          │
└──────────────────────────────────────────────────────────────────────┘
                                  │ llamada Python
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Capa 2: Lógica de aplicación                                        │
│  ────────────────────────────                                        │
│  opendraft/engine/draft_generator.py  (función generate_draft)       │
│  opendraft/engine/phases/            (research, structure, ...)      │
│  opendraft/engine/utils/             (helpers: api_citations, etc.)  │
│                                                                      │
│  El engine llama al tracker para reportar progreso:                  │
│      ctx.tracker.update_phase(...)                                   │
│      ctx.tracker.log_activity(...)                                   │
│      ctx.tracker.log_source_found(...)                               │
│      ctx.tracker.check_cancellation()                                │
│      ctx.tracker.mark_completed() / mark_failed()                    │
└──────────────────────────────────────────────────────────────────────┘
                                  │ en proceso
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Capa 1: Adaptadores (este proyecto aporta los custom)               │
│  ─────────────────────────────────────────────────                   │
│  opendraft/engine/utils/openai_compat_wrapper.py                     │
│      OpenAICompatWrapper  +  _CompatResponse / Candidate / Part      │
│  backend/server.py  →  LocalTracker                                  │
│      Drop-in replacement de ProgressTracker sin Supabase             │
└──────────────────────────────────────────────────────────────────────┘
                                  │ HTTPS
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Capa 0: Servicios externos                                          │
│  ────────────────────────────                                        │
│  LiteLLM gateway  (https://litellm.aiangela.cloud)                   │
│      └─→ Google Gemini                                               │
│  CrossRef  ·  OpenAlex  ·  Semantic Scholar  ·  arXiv                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Mapa de archivos

```
E:\chat-busqueda\
│
├── README.md                          ← entrypoint para humanos
├── .gitignore                         ← excludes .env, outputs, opendraft/, venv
├── .env.example                       ← template commiteable (sin secretos)
├── start.bat                          ← launcher Windows (auto-clona + setup)
├── setup_litellm.ps1                  ← helper específico para LiteLLM
│
├── docs/                              ← este directorio
│   ├── HOW_IT_WORKS.md                ← vista de usuario, flujo end-to-end
│   └── ARCHITECTURE.md                ← este archivo
│
├── frontend/                          ← Capa 4
│   ├── index.html                     ← shell del chat (sidebar, topbar, composer)
│   ├── style.css                      ← 1206 líneas, Inter font, dark theme
│   └── app.js                         ← SSE consumer, history loader, composer
│
├── backend/                           ← Capa 3 (API)
│   ├── server.py                      ← FastAPI: endpoints, SSE, LocalTracker
│   ├── requirements.txt               ← fastapi, uvicorn, sse-starlette, etc.
│   ├── smoke_test.py                  ← verifica wrapper LiteLLM end-to-end
│   └── outputs/                       ← drafts generados (gitignored)
│
└── opendraft/                         ← engine original (gitignored, auto-cloneado)
    └── engine/
        ├── draft_generator.py         ← orquestador principal (genera_draft)
        ├── config.py                  ← ★ PATCHED: modelos LiteLLM válidos
        ├── phases/                    ← research, structure, citations, compose...
        │   ├── research.py            ← Scout (CrossRef/OpenAlex/S2/arXiv)
        │   ├── structure.py           ← diseña el outline
        │   ├── citations.py           ← compila referencias
        │   ├── compose.py             ← escribe cada sección
        │   ├── validate.py            ← QA de coherencia
        │   └── compile.py             ← ensambla y exporta
        ├── prompts/                   ← templates de instrucciones por agente
        ├── utils/
        │   ├── agent_runner.py        ← ★ PATCHED: setup_model() respeta AI_PROVIDER
        │   ├── openai_compat_wrapper.py  ★ NUEVO (este proyecto)
        │   ├── api_citations/         ← clientes de CrossRef, OpenAlex, S2
        │   │   ├── crossref.py
        │   │   ├── openalex.py        ← ★ PATCHED: lee OPENALEX_EMAIL
        │   │   ├── semantic_scholar.py ← ya soportaba API key via env
        │   │   ├── base.py            ← pool de User-Agents rotativos
        │   │   └── orchestrator.py    ← coordina queries en paralelo
        │   ├── progress_tracker.py    ← Supabase-based (no lo usamos en local)
        │   ├── gemini_client.py       ← wrapper de google.genai SDK
        │   └── ...                    ← más utilities
        ├── config.py                  ← ★ PATCHED: lee .env en múltiples paths
        └── requirements.txt
```

Los archivos marcados con ★ son los que este proyecto modificó o creó.

---

## Contratos entre capas

### Frontend → Backend (HTTP)

| Método | Path | Body | Response |
|---|---|---|---|
| `GET` | `/` | — | `index.html` |
| `GET` | `/static/*` | — | archivos estáticos |
| `GET` | `/health` | — | `{status, sessions, ...}` |
| `GET` | `/api/config` | — | paper_types, languages, citation_styles |
| `POST` | `/api/chat/start` | `{topic, paper_type, language, output_type, blurb?, author_name?, institution?, citation_style}` | `{session_id}` |
| `GET` | `/api/chat/{id}/stream` | — | `text/event-stream` (SSE) |
| `GET` | `/api/chat/{id}/status` | — | snapshot de la sesión |
| `POST` | `/api/chat/{id}/cancel` | — | `{ok: true}` |
| `GET` | `/api/chat/{id}/download/{md\|pdf\|docx\|zip\|tex}` | — | `application/...` (file) |
| `GET` | `/api/chat/history?limit=N` | — | `[{session_id, topic, status, ...}]` |

### Backend → Frontend (SSE)

Eventos con `event:` line + JSON en `data:`. Ver [HOW_IT_WORKS.md](./HOW_IT_WORKS.md#eventos-que-manda-el-backend-sse) para la lista completa.

### Engine → Tracker (en proceso)

El engine espera que el tracker tenga esta interfaz (es la que `LocalTracker` implementa):

```python
tracker.log_activity(message, event_type, phase)
tracker.update_phase(phase, progress_percent, sources_count, chapters_count, details)
tracker.log_source_found(title, authors, year, source_type, doi, url, verified)
tracker.check_cancellation()                  # raises si fue cancelado
tracker.send_heartbeat()                      # no-op en local
tracker.set_current_chapter(index, total, title)
tracker.set_outline(chapters)
tracker.clear_current_chapter()
tracker.update_research(sources_count, phase_detail)
tracker.update_writing(chapters_count, chapter_name, total_chapters)
tracker.update_formatting()
tracker.update_exporting(export_type)
tracker.mark_completed()
tracker.mark_failed(error_message)
tracker.upload_milestone_file(path, name, content_type)  # local devuelve "local://..."
```

### Wrapper → LLM (HTTPS)

```
POST {OPENAI_BASE_URL}/v1/chat/completions
Authorization: Bearer {OPENAI_API_KEY}
Content-Type: application/json

{
  "model": "gemini-3.1-flash-lite",
  "messages": [{"role": "user", "content": "<prompt>"}],
  "temperature": 0.7,
  "max_tokens": 8192
}
```

El wrapper acepta cualquier endpoint que respete este contrato. Eso incluye LiteLLM, OpenAI, OpenRouter, Ollama, vLLM, etc.

---

## Decisiones de diseño

### 1. `LocalTracker` en vez de Supabase

El `ProgressTracker` original escribe cada update a Supabase. Eso requiere URL + service key y agrega latencia. Para una app local (un solo usuario) eso es overkill.

`LocalTracker` mantiene todo en memoria y publica a una `queue.Queue` thread-safe. El endpoint SSE lee de esa queue. Sin DB, sin red, sin auth.

**Trade-off:** si el server se cae, perdés el progreso de la sesión en curso. La historia queda en memoria hasta el próximo restart.

**Migrar a Redis/Postgres en prod:** reemplazar el `queue.Queue` por un Redis pub/sub o similar. El engine no se entera.

### 2. Worker thread para `generate_draft`

`generate_draft()` es bloqueante y tarda 10-20 min. Si lo llamara en el event loop de FastAPI con `await`, podría (con `asyncio.to_thread`), pero el SSE necesita emitir en vivo y el ciclo de vida de la sesión es complicado.

**Decisión:** thread separado. El `LocalTracker._events` queue es la fuente de verdad, el SSE endpoint la drena.

**Trade-off:** un thread por sesión. Para 1000+ sesiones simultáneas hay que pasar a un job queue (Celery, RQ, Modal).

### 3. `OpenAICompatWrapper` vs parchar el SDK de Gemini

Originalmente opendraft usa `google.genai`. Para enrutar a LiteLLM había dos opciones:

a. **Configurar google.genai con un custom endpoint** — complicado, depende de la versión del SDK, no es un patrón soportado oficialmente.

b. **Wrapper OpenAI-compatible** — LiteLLM expone `/v1/chat/completions` estándar. Solo necesitamos imitar la interfaz que el engine espera (`generate_content(prompt) -> response.text`).

**Decidí b** porque es portable: si mañana LiteLLM se cae, podés apuntar a OpenAI directo, Ollama, o lo que sea, sin tocar el engine.

### 4. El engine como dependencia vendoreada, no fork

`opendraft/` está clonado pero commiteado como ignorado. Si lo metiera como fork:
- Mantener un parche a largo plazo (merge hell cuando openpdeft actualice)
- Complica los git diffs

**Decidí vendor+ignore:** los cambios al engine son 4 líneas chicas (`config.py`, `agent_runner.py`, `openalex.py`). Se documentan en este README. Si openpdeft hace cambios incompatibles, hay que ajustar el wrapper.

### 5. Inter font + CSS plano, sin framework

La UI usa vanilla JS + CSS plano. Cero React, cero build step.

**Por qué:** el chat es chico. El bundle es ~50KB total. Cualquier framework suma peso y complejidad. Si crece (preview de PDF, drag-drop de topics, etc.) se puede meter Preact sin trauma.

---

## Puntos de extensión

### Quiero cambiar el comportamiento del LLM (modelo, temperatura, etc.)

Editá `opendraft/.env`:
```env
OPENAI_MODEL=gemini-3.1-flash-lite    # o golondrina-kimi, gpt-4, etc.
OPENAI_MAX_OUTPUT_TOKENS=16384        # subir para papers más largos
```

Para cambios más finos (temperature, top_p, etc.), editá `openai_compat_wrapper.py:create_openai_compat_client`.

### Quiero agregar un nuevo modelo válido a opendraft

Editá `opendraft/engine/config.py` línea ~69 (lista `valid_openai_models`):
```python
valid_openai_models = [
    'gpt-4.1-nano',
    'gemini-3.1-flash-lite',
    'golondrina-kimi',
    'my-new-model',           # ← acá
]
```

### Quiero agregar un endpoint nuevo

Editá `backend/server.py`. Ejemplo:
```python
@app.get("/api/chat/{session_id}/bibliography")
async def get_bibliography(session_id: str):
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404)
    path = OUTPUTS_DIR / session_id / "research" / "bibliography.json"
    if not path.exists():
        raise HTTPException(404, "Bibliography not generated yet")
    return FileResponse(path)
```

No olvides reiniciar el server.

### Quiero cambiar la UI

- **Tema/colores** → `frontend/style.css` sección `:root` (línea 9-50) — todos los tokens están ahí
- **Layout** → `frontend/style.css` secciones `LAYOUT`, `SIDEBAR`, `CHAT AREA`
- **Componentes** → `frontend/index.html` estructura + `frontend/app.js` para lógica
- **Copy/mensajes** → buscar los strings en `app.js` (ej. "Draft ready", "Generating…")

### Quiero agregar un agente nuevo al pipeline

Hay que tocar `opendraft/engine/phases/` y agregarlo al orquestador `draft_generator.py`. No es trivial — el engine tiene una secuencia estricta.

**Alternativa más fácil:** agregar un "pre-processor" en `backend/server.py:run_generation` que corre antes de `generate_draft()`. Por ejemplo, traducir el topic, expandir el abstract, etc.

### Quiero cambiar la forma de los eventos SSE

Editar `LocalTracker` en `backend/server.py` y el consumer en `frontend/app.js:openStream`. Ambos lados tienen que coincidir.

---

## Limitaciones conocidas

- **Sesiones in-memory** — si el server cae, se pierde. Para producción usar Redis o Postgres.
- **Worker thread por sesión** — no escala a miles. Para escala usar Celery/RQ o Modal.
- **OpenAlex polite email** — sin email, tenés rate limit bajo. Es gratis y no verifica.
- **Semantic Scholar API key** — sin key, 1 req/seg (lento). Con key, 100 req/seg. Es gratis, tarda 2 min sacarla.
- **weasyprint en Windows** — necesita GTK runtime o MSYS2. Si no podés instalar, exportás solo DOCX/MD/LaTeX.

---

## Testing

Smoke test del wrapper LiteLLM:
```bash
$env:PYTHONPATH = "E:\chat-busqueda\opendraft\engine;E:\chat-busqueda\opendraft"
cd E:\chat-busqueda\opendraft\engine
python E:\chat-busqueda\backend\smoke_test.py
```

Debería mostrar:
- provider: openai
- model: gemini-3.1-flash-lite
- type: OpenAICompatWrapper
- response.text: "LiteLLM is a lightweight Python library..."

Si ves eso, todo el stack de LLM está OK.

Para testear el pipeline end-to-end, andá a la UI y mandá un topic simple tipo `"Microplastic pollution"`. Si termina con un result-card y podés descargar el PDF, todo el stack está OK.