# ¿Cómo funciona OpenDraft Chat?

> La versión en una línea: le escribís un tema de investigación en el chat, y un pipeline de 19 agentes de IA redacta un paper académico con citas verificadas contra CrossRef, OpenAlex, Semantic Scholar y arXiv. En 10–20 minutos tenés PDF, Word, LaTeX y Markdown descargables.

Esta guía recorre lo que pasa **de punta a punta** cuando apretás el botón verde de enviar.

---

## El viaje completo (60 segundos)

```
[1] Vos escribís el topic       →  el backend arranca un worker thread
[2] Scout busca papers           →  CrossRef / OpenAlex / Semantic Scholar / arXiv
[3] Scribe resume cada paper     →  el LLM (vía LiteLLM) sintetiza abstracts
[4] Signal detecta gaps          →  identifica huecos en la cobertura
[5] Structure arma el outline    →  capítulos, secciones, word targets
[6] Citations compila referencias →  APA / IEEE / Chicago / MLA
[7] Compose escribe cada sección →  Introducción, Lit Review, Metodología…
[8] Validate hace QA             →  chequea coherencia, citas válidas
[9] Compile ensambla el draft    →  une secciones, agrega bibliografía
[10] Export genera PDF/DOCX/...  →  todo queda en backend/outputs/<id>/
```

Durante todo el proceso, el frontend recibe eventos en vivo (Server-Sent Events) y vas viendo qué agente está trabajando, qué encontró, qué escribió.

---

## Las 6 fases que ves en el pipeline (UI)

El indicador en el chat muestra el progreso en bloques grandes. Cada bloque corresponde a uno o más de los 10 pasos de arriba:

```
┌──────────┬──────────┬─────────┬──────────┬──────────┬──────────┐
│ 🔍       │ 📋       │ ✍️      │ 🔧       │ 📄       │ ✅       │
│ Research │ Outline  │ Writing │ Compile  │ Export   │ Done     │
└──────────┴──────────┴─────────┴──────────┴──────────┴──────────┘
     5%        25%      35-75%      80%       85-95%     100%
```

El bloque activo se ilumina en verde mint, los bloques completados quedan en verde apagado.

---

## ¿Qué pasa cuando apretás Send?

```
┌──────────────────────────────────────────────────────────────────────┐
│  Navegador (JavaScript)                                              │
│  ─────────────────────                                               │
│  1. fetch('POST /api/chat/start', { topic, paper_type, ... })        │
│  2. Renderiza el mensaje del usuario + el panel de status vacío      │
│  3. Abre EventSource('/api/chat/{id}/stream')                        │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Backend FastAPI (backend/server.py)                                 │
│  ──────────────────────────────────                                  │
│  1. Crea un ChatSession con un id único (12 chars)                   │
│  2. Construye un LocalTracker (reemplazo del ProgressTracker)        │
│  3. Lanza generate_draft() en un thread aparte                       │
│  4. Devuelve { session_id } al instante                              │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Opendraft Engine (opendraft/engine/draft_generator.py)              │
│  ───────────────────────────────────────────────────                 │
│  Corre los 19 agentes en pipeline. Cada agente llama:               │
│                                                                      │
│      model.generate_content(prompt)                                  │
│              │                                                       │
│              ▼                                                       │
│      OpenAICompatWrapper (LiteLLM gateway)                           │
│              │                                                       │
│              ▼                                                       │
│      POST https://litellm.aiangela.cloud/v1/chat/completions         │
│      Bearer sk-P5R6uSqsjteUOGAJX6rf0w                                │
│      model: gemini-3.1-flash-lite                                    │
│              │                                                       │
│              ▼                                                       │
│      Respuesta → LiteLLM → Google Gemini → de vuelta                 │
│                                                                      │
│  Entre agente y agente, el engine llama al tracker para reportar:   │
│      ctx.tracker.update_phase('research', 5, ...)                   │
│      ctx.tracker.log_activity('...', event_type='found', ...)        │
│      ctx.tracker.log_source_found(title=..., authors=..., year=...)  │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LocalTracker (backend/server.py)                                    │
│  ────────────────────────────────                                    │
│  Recibe las llamadas del engine y publica eventos en una queue:     │
│      { type: 'phase',      phase: 'research',  percent: 12 }        │
│      { type: 'activity',   message: '...' }                          │
│      { type: 'source',     source: { title, authors, ... } }         │
│  Más el SSE endpoint los lee y los manda al navegador.               │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Navegador (JavaScript)                                              │
│  ─────────────────────                                               │
│  El EventSource recibe eventos y actualiza:                          │
│   - el phase indicator (qué agente corre ahora)                      │
│   - la progress bar (% completado)                                   │
│   - el activity feed (líneas en vivo)                                │
│   - la sources panel (papers que va encontrando)                     │
│  Cuando llega el evento 'result', reemplaza el panel por el         │
│  result-card con botones de descarga PDF/DOCX/LaTeX/MD/ZIP.           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Estructura de archivos generados

Cada generación crea una carpeta única en `backend/outputs/<session_id>/`:

```
backend/outputs/
└── a1b2c3d4e5f6/                          ← session_id
    ├── research/                          ← fase 1: papers encontrados
    │   ├── scout_raw.md                   ←   texto crudo de CrossRef etc.
    │   ├── combined_research.md           ←   todos los abstracts resumidos
    │   ├── research_gaps.md               ←   huecos identificados
    │   └── bibliography.json              ←   base de datos de citas
    ├── drafts/                            ← fase 7: secciones escritas
    │   ├── 01_introduction.md
    │   ├── 02_literature_review.md
    │   ├── 03_methodology.md
    │   ├── 04_results.md
    │   ├── 05_discussion.md
    │   └── 06_conclusion.md
    ├── tools/                             ← prompts para iterar después
    └── exports/                           ← productos finales
        ├── thesis.pdf                     ←  ⬇ esto es lo que descargás
        ├── thesis.docx                    ←  ⬇ y esto
        ├── thesis.md
        ├── thesis.tex
        └── thesis.zip                     ←  todos juntos
```

El botón de descarga en el chat usa `GET /api/chat/{id}/download/{pdf|docx|md|tex|zip}` que sirve el archivo desde `exports/`.

---

## Eventos que manda el backend (SSE)

El endpoint `/api/chat/{id}/stream` emite eventos tipados. Cada uno es JSON en una línea:

| Evento | Cuándo | Datos clave |
|---|---|---|
| `snapshot` | Apenas te conectás | Estado actual completo (para reconectar) |
| `phase` | Cuando cambia la fase o el % | `phase`, `progress_percent`, `stage` |
| `activity` | Log de progreso de un agente | `message`, `event_type`, `phase` |
| `source` | Scout encontró un paper | `source: { title, authors, year, doi }` |
| `result` | Generación completa | `artifacts`, `word_count`, `citation_count` |
| `cancelled` | Cancelaste vos | — |
| `error` | Algo explotó | `error`, `trace` |
| `done` | Cierre limpio del stream | `status` final |
| `ping` | Keepalive cada 15s | `ts` |

El navegador mantiene la conexión abierta y va actualizando el DOM sin recargar.

---

## Cancelar una generación

El botón rojo arriba a la derecha llama a `POST /api/chat/{id}/cancel`. Eso pone un flag en el `LocalTracker` que el engine chequea entre agentes:

```python
def check_cancellation(self):
    if self._cancel_requested.is_set():
        raise GenerationCancelled(...)
```

**Importante:** el chequeo es entre agentes, no en medio de un LLM call. Si el Scout está esperando 30s a CrossRef, la cancelación va a llegar después de que ese call termine. Pero el engine no inicia el siguiente agente después de cancelar.

---

## ¿Por qué algunas generaciones fallan?

El Scout agent es la parte más frágil. Tiene 3 cosas que pueden fallar:

1. **Rate limit de Semantic Scholar** → si ves logs con `429 signaled for semantic_scholar`, esperá 15-20 min o sacá una [API key gratis](https://www.semanticscholar.org/product/api)
2. **Topic muy específico** → si el tema es muy nicho, CrossRef no devuelve nada. Probá con uno más mainstream
3. **Citas insuficientes** → el engine pide 7 mínimo. Si Scout solo encuentra 3, falla con "Insufficient citations"

Workarounds:
- Modo **expose** (3x más rápido, menos exigente)
- Bajar el mínimo de citas (edición de 1 línea)
- Cambiar idioma a `en` (CrossRef tiene mejor cobertura)

---

## Costos

Las llamadas a LLM pasan por LiteLLM, que enruta a Google Gemini. Los precios aproximados:

| Tipo de paper | Tiempo | Costo API |
|---|---|---|
| Research expose | 3-5 min | $0.05 - $0.15 |
| Research paper | 5-15 min | $0.15 - $0.50 |
| Bachelor | 10-20 min | $0.30 - $1.00 |
| Master | 15-30 min | $0.50 - $2.00 |
| PhD | 30-60 min | $1.00 - $3.50 |

(Con Gemini Flash. Pro/Claude/GPT cuestan 2-10x más.)

LiteLLM te deja ver el gasto por team/key en el admin panel, así que siempre sabés cuánto llevás gastado.

---

## Próximos pasos

- **Quiero leer la arquitectura técnica** → [ARCHITECTURE.md](./ARCHITECTURE.md)
- **Quiero extender la app** → [ARCHITECTURE.md#puntos-de-extension](./ARCHITECTURE.md#puntos-de-extensión)
- **Quiero saber qué hace cada parte del código** → [ARCHITECTURE.md#mapa-de-archivos](./ARCHITECTURE.md#mapa-de-archivos)