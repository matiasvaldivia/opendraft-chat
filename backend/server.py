"""
OpenDraft Chat Backend
======================

FastAPI server that wraps the OpenDraft engine as a chat-friendly HTTP API.

Architecture
------------
- POST /api/chat/start            -> create session, kick off generation in background
- GET  /api/chat/{id}/stream      -> Server-Sent Events for live progress
- GET  /api/chat/{id}/status      -> one-shot snapshot
- POST /api/chat/{id}/cancel      -> cancel an in-flight generation
- GET  /api/chat/{id}/download/*  -> download generated artifacts (md/pdf/docx/zip)
- GET  /api/chat/history          -> list past sessions (in-memory + persisted metadata)
- GET  /                          -> serves the chat UI from ../frontend
- GET  /health                    -> liveness probe

The heavy lifting happens in `generate_draft()` from the OpenDraft engine.
We feed it a `LocalTracker` (defined below) that mimics the engine's
ProgressTracker interface but publishes events to an in-memory queue
instead of writing to Supabase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "opendraft" / "engine"
OPENDRAFT_DIR = ROOT / "opendraft"
FRONTEND_DIR = ROOT / "frontend"
OUTPUTS_DIR = ROOT / "backend" / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# Make the opendraft engine importable
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(OPENDRAFT_DIR))

# Load .env from the opendraft repo if present
ENV_FILE = OPENDRAFT_DIR / ".env"
if ENV_FILE.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-22s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("opendraft-chat")

# ---------------------------------------------------------------------------
# LocalTracker: drop-in replacement for ProgressTracker (no Supabase)
# ---------------------------------------------------------------------------
class LocalTracker:
    """
    Mirrors the ProgressTracker API used by OpenDraft's phase modules but
    keeps everything in memory and publishes events to a thread-safe queue
    so the SSE endpoint can stream them to the browser.
    """

    MAX_ACTIVITY_LOG_SIZE = 200
    PHASE_EMOJIS = {
        "research": "🔍",
        "structure": "📋",
        "writing": "✍️",
        "compiling": "🔧",
        "exporting": "📄",
        "completed": "✅",
        "error": "❌",
    }

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._activity_log: List[Dict[str, Any]] = []
        self._milestone_files: Dict[str, str] = {}
        self._source_data: List[Dict[str, Any]] = []
        self._current_chapter: Optional[Dict[str, Any]] = None
        self._outline: Optional[Dict[str, Any]] = None
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._cancel_requested = threading.Event()

    # -- publisher helpers -------------------------------------------------
    def _publish(self, event: Dict[str, Any]) -> None:
        """Push an event to subscribers. Non-blocking."""
        try:
            self._events.put_nowait(event)
        except queue.Full:
            pass  # drop oldest-style: we never block generation

    # -- ProgressTracker-compatible API -----------------------------------
    def update_phase(
        self,
        phase: str,
        progress_percent: int = 0,
        sources_count: Optional[int] = None,
        chapters_count: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        stage = (details or {}).get("stage", phase)
        activity_details = dict(details or {})
        if sources_count is not None:
            activity_details["sources_count"] = sources_count
        if chapters_count is not None:
            activity_details["chapters_count"] = chapters_count

        entry = self._add_activity_entry(phase, stage, activity_details)

        self._publish({
            "type": "phase",
            "phase": phase,
            "progress_percent": progress_percent,
            "sources_count": sources_count,
            "chapters_count": chapters_count,
            "stage": stage,
            "details": details or {},
            "activity_entry": entry,
            "ts": time.time(),
        })

    def log_activity(self, message: str, event_type: str = "info", phase: Optional[str] = None) -> None:
        entry = {
            "id": f"custom_{event_type}_{int(time.time()*1000)}",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": event_type,
            "message": message,
            "icon": self.PHASE_EMOJIS.get(phase or "research", "📌"),
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > self.MAX_ACTIVITY_LOG_SIZE:
            self._activity_log = self._activity_log[-self.MAX_ACTIVITY_LOG_SIZE:]

        self._publish({
            "type": "activity",
            "message": message,
            "event_type": event_type,
            "phase": phase,
            "activity_entry": entry,
            "ts": time.time(),
        })

    def log_source_found(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        source_type: str = "paper",
        doi: Optional[str] = None,
        url: Optional[str] = None,
        verified: bool = True,
    ) -> None:
        if authors:
            if len(authors) == 1:
                author_str = authors[0]
            elif len(authors) == 2:
                author_str = f"{authors[0]} & {authors[1]}"
            else:
                author_str = f"{authors[0]} et al."
        else:
            author_str = ""

        source_entry = {
            "title": title,
            "authors": authors or [],
            "author_str": author_str,
            "year": year,
            "source_type": source_type,
            "doi": doi,
            "url": url,
            "verified": verified,
        }
        self._source_data.append(source_entry)

        # Also log to activity feed
        year_str = f" ({year})" if year else ""
        message = f"Found: {title}{year_str} — {author_str}"
        self.log_activity(message, event_type="found", phase="research")

        self._publish({
            "type": "source",
            "source": source_entry,
            "ts": time.time(),
        })

    def check_cancellation(self) -> None:
        if self._cancel_requested.is_set():
            raise GenerationCancelled(f"Session {self.session_id} cancelled by user")

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        self.log_activity("Cancellation requested by user", event_type="error", phase="error")

    def _add_activity_entry(self, phase: str, stage: str, details: Dict[str, Any]) -> Dict[str, Any]:
        message = self._format_activity_message(stage, details)
        entry = {
            "id": f"{phase}_{stage}_{int(time.time()*1000)}",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": self._guess_event_type(stage),
            "message": message,
            "icon": self.PHASE_EMOJIS.get(phase, "📌"),
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > self.MAX_ACTIVITY_LOG_SIZE:
            self._activity_log = self._activity_log[-self.MAX_ACTIVITY_LOG_SIZE:]
        return entry

    @staticmethod
    def _format_activity_message(stage: str, details: Dict[str, Any]) -> str:
        templates = {
            "starting_research": "Starting academic research...",
            "querying_crossref": "Querying CrossRef for peer-reviewed papers...",
            "querying_semantic_scholar": "Searching Semantic Scholar...",
            "querying_gemini": "Using AI-powered search...",
            "scout_completed": "Found {sources_count} academic sources",
            "research_complete": "Research phase complete",
            "creating_outline": "Designing thesis structure...",
            "outline_complete": "Thesis outline ready",
            "starting_composition": "Beginning chapter composition...",
            "writing_introduction": "Writing Introduction...",
            "introduction_complete": "Introduction complete",
            "writing_literature_review": "Writing Literature Review...",
            "literature_review_complete": "Literature Review complete",
            "writing_methodology": "Writing Methodology...",
            "methodology_complete": "Methodology complete",
            "writing_results": "Writing Analysis & Results...",
            "results_complete": "Analysis & Results complete",
            "writing_discussion": "Writing Discussion...",
            "discussion_complete": "Discussion complete",
            "writing_conclusion": "Writing Conclusion...",
            "conclusion_complete": "Conclusion complete",
            "writing_appendices": "Writing Appendices...",
            "appendices_complete": "Appendices complete",
            "assembling_draft": "Assembling final thesis...",
            "compiling_citations": "Compiling bibliography...",
            "generating_abstract": "Generating abstract...",
            "compilation_complete": "Compilation complete",
            "exporting_pdf": "Generating PDF...",
            "pdf_complete": "PDF generated",
            "exporting_docx": "Generating Word document...",
            "docx_complete": "Word document generated",
            "creating_zip": "Creating download package...",
            "export_complete": "Export complete",
        }
        template = templates.get(stage, stage.replace("_", " ").title())
        try:
            return template.format(**details)
        except (KeyError, IndexError):
            return template.replace("{sources_count}", str(details.get("sources_count", "?")))

    @staticmethod
    def _guess_event_type(stage: str) -> str:
        s = (stage or "").lower()
        if any(k in s for k in ("query", "search", "starting_research")):
            return "search"
        if any(k in s for k in ("found", "complete", "ready", "generated")):
            return "found"
        if any(k in s for k in ("writing", "composition", "assembling")):
            return "writing"
        if "_complete" in s or "outline_ready" in s:
            return "milestone"
        if any(k in s for k in ("error", "failed")):
            return "error"
        return "info"

    # ========================================================================
    # Compatibility shims for ProgressTracker methods the engine calls but
    # don't apply to a local-only tracker. They publish proper events so the
    # chat UI can show rich progress without needing Supabase.
    # ========================================================================

    def set_current_chapter(self, index: int, total: int, title: str) -> None:
        self._current_chapter = {"index": index, "total": total, "title": title}
        self._publish({
            "type": "phase",
            "phase": "writing",
            "progress_percent": self._current_chapter_index_percent(index, total),
            "chapters_count": index - 1,
            "stage": "writing_chapter",
            "details": {"current_chapter": {"index": index, "total": total, "title": title}},
            "ts": time.time(),
        })

    def set_outline(self, chapters: List[Dict[str, Any]]) -> None:
        self._outline = {"chapters": chapters}
        self._publish({
            "type": "phase",
            "phase": "structure",
            "progress_percent": 30,
            "stage": "outline_ready",
            "details": {"outline": self._outline},
            "ts": time.time(),
        })

    def clear_current_chapter(self) -> None:
        self._current_chapter = None

    @staticmethod
    def _current_chapter_index_percent(index: int, total: int) -> int:
        # 20% reserved for research; 50% span for writing → 70% max
        return min(70, 20 + int(50 * min(index / max(total, 1), 1)))

    def update_research(self, sources_count: int, phase_detail: str = "") -> None:
        details: Dict[str, Any] = {"stage": "scout_completed"}
        if phase_detail:
            details["phase_detail"] = phase_detail
        self.update_phase(
            phase="research",
            progress_percent=20,
            sources_count=sources_count,
            details=details,
        )

    def update_writing(self, chapters_count: int, chapter_name: str = "", total_chapters: int = 7) -> None:
        progress = 20 + int(50 * min(chapters_count / max(total_chapters, 1), 1))
        if chapter_name:
            self.set_current_chapter(
                index=chapters_count + 1,
                total=total_chapters,
                title=chapter_name,
            )
        details: Optional[Dict[str, Any]] = (
            {"current_chapter": chapter_name} if chapter_name else None
        )
        self.update_phase(
            phase="writing",
            progress_percent=progress,
            chapters_count=chapters_count,
            details=details,
        )

    def update_formatting(self) -> None:
        self.update_phase(
            phase="compiling",
            progress_percent=75,
            details={"stage": "formatting_and_citations"},
        )

    def update_exporting(self, export_type: str = "") -> None:
        details: Optional[Dict[str, Any]] = (
            {"export_type": export_type} if export_type else None
        )
        self.update_phase(
            phase="exporting",
            progress_percent=90,
            details=details,
        )

    def mark_completed(self) -> None:
        self._publish({
            "type": "phase",
            "phase": "completed",
            "progress_percent": 100,
            "stage": "generation_complete",
            "ts": time.time(),
        })
        self._publish({
            "type": "milestone",
            "message": "Generation complete!",
            "icon": "🎉",
            "ts": time.time(),
        })

    def mark_failed(self, error_message: Optional[str] = None) -> None:
        msg = error_message or "Unknown error"
        self._publish({
            "type": "activity",
            "message": f"Generation failed: {msg}",
            "event_type": "error",
            "phase": "error",
            "icon": "❌",
            "ts": time.time(),
        })

    def send_heartbeat(self) -> None:
        # No-op in local mode; production used a DB column to track liveness.
        pass

    def upload_milestone_file(self, file_path: str, milestone_name: str, content_type: str = "text/markdown"):
        # Mark as a milestone locally; serves the same purpose as the DB
        # version (front-end can read .milestone_files later) but no upload.
        try:
            path = Path(file_path)
            if path.exists():
                size = path.stat().st_size
                self._milestone_files[milestone_name] = str(path)
                self._publish({
                    "type": "milestone",
                    "milestone": milestone_name,
                    "path": str(path),
                    "size_bytes": size,
                    "ts": time.time(),
                })
                return f"local://{path}"
        except Exception:
            pass
        return None

    def _update_milestone_files(self):
        pass


class GenerationCancelled(Exception):
    """Raised inside generation when user requests cancellation."""


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
@dataclass
class ChatSession:
    id: str
    topic: str
    paper_type: str = "research_paper"
    language: str = "en"
    blurb: Optional[str] = None
    author_name: Optional[str] = None
    institution: Optional[str] = None
    output_type: str = "full"  # 'full' or 'expose'
    citation_style: str = "apa"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    status: str = "pending"  # pending | running | completed | error | cancelled
    progress_percent: int = 0
    current_phase: str = ""
    activity_log: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    artifacts: Dict[str, str] = field(default_factory=dict)  # md / pdf / docx / zip
    word_count: int = 0
    citation_count: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Generation runner (runs in a worker thread)
# ---------------------------------------------------------------------------
def run_generation(session: ChatSession, tracker: LocalTracker) -> None:
    """
    Execute OpenDraft's generate_draft() in a worker thread.
    Updates `session` and pushes events through `tracker`.
    """
    session_dir = OUTPUTS_DIR / session.id
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Import opendraft engine lazily so import errors don't kill the server
        from draft_generator import generate_draft  # type: ignore

        session.status = "running"
        session.started_at = time.time()
        tracker.log_activity(
            f"Initializing 19-agent pipeline for: {session.topic}",
            event_type="milestone",
            phase="research",
        )

        # Map user-facing paper_type -> opendraft academic_level
        academic_level = {
            "research_paper": "research_paper",
            "bachelor": "bachelor",
            "master": "master",
            "phd": "phd",
            "expose": "research_paper",  # expose is output_type, not level
        }.get(session.paper_type, "research_paper")

        pdf_path, docx_path = generate_draft(
            topic=session.topic,
            language=session.language,
            academic_level=academic_level,
            output_dir=session_dir,
            output_type=session.output_type,
            blurb=session.blurb,
            author_name=session.author_name,
            institution=session.institution,
            citation_style=session.citation_style,
            tracker=tracker,
            streamer=None,
            skip_validation=True,
            verbose=False,
        )

        # After generation: discover all artifacts
        exports_dir = session_dir / "exports"
        if exports_dir.exists():
            for f in exports_dir.iterdir():
                if f.is_file():
                    kind = f.suffix.lower().lstrip(".")
                    if kind in ("pdf", "docx", "md", "zip", "tex", "latex"):
                        session.artifacts[kind] = str(f.relative_to(ROOT))

        # Count citations from bibliography
        bib_path = session_dir / "research" / "bibliography.json"
        if bib_path.exists():
            try:
                with open(bib_path, "r", encoding="utf-8") as fh:
                    bib = json.load(fh)
                citations = bib.get("citations", bib) if isinstance(bib, dict) else bib
                if isinstance(citations, list):
                    session.citation_count = len(citations)
            except Exception:
                pass

        # Word count from final MD
        md_files = sorted(
            exports_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ) if exports_dir.exists() else []
        if md_files:
            try:
                text = md_files[0].read_text(encoding="utf-8", errors="ignore")
                session.word_count = len(text.split())
                session.artifacts["md"] = str(md_files[0].relative_to(ROOT))
            except Exception:
                pass

        session.status = "completed"
        session.progress_percent = 100
        session.finished_at = time.time()
        tracker.update_phase("completed", progress_percent=100, details={"stage": "export_complete"})

        tracker._publish({
            "type": "result",
            "session_id": session.id,
            "artifacts": session.artifacts,
            "word_count": session.word_count,
            "citation_count": session.citation_count,
            "elapsed_seconds": session.finished_at - (session.started_at or session.finished_at),
            "ts": time.time(),
        })

    except GenerationCancelled:
        session.status = "cancelled"
        session.finished_at = time.time()
        tracker._publish({"type": "cancelled", "ts": time.time()})

    except Exception as e:
        log.exception("Generation failed")
        session.status = "error"
        session.error = f"{type(e).__name__}: {e}"
        session.finished_at = time.time()
        tracker.log_activity(
            f"Generation failed: {type(e).__name__}: {e}",
            event_type="error",
            phase="error",
        )
        tracker._publish({
            "type": "error",
            "error": session.error,
            "trace": traceback.format_exc(),
            "ts": time.time(),
        })
    finally:
        # Sync tracker state back into session
        session.activity_log = list(tracker._activity_log)
        session.sources = list(tracker._source_data)
        session.progress_percent = 100 if session.status == "completed" else session.progress_percent
        tracker._publish({"type": "__final__", "status": session.status})


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="OpenDraft Chat", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store. For a real product use Redis/Postgres.
SESSIONS: Dict[str, ChatSession] = {}
SESSION_LOCK = threading.Lock()


# -- Pydantic models --------------------------------------------------------
class ChatStartRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=500, description="Research topic or question")
    paper_type: str = Field("research_paper", pattern="^(research_paper|bachelor|master|phd)$")
    language: str = Field("en", min_length=2, max_length=8)
    output_type: str = Field("full", pattern="^(full|expose)$")
    blurb: Optional[str] = Field(None, max_length=1000)
    author_name: Optional[str] = Field(None, max_length=200)
    institution: Optional[str] = Field(None, max_length=200)
    citation_style: str = Field("apa", pattern="^(apa|ieee|chicago|mla)$")


# -- API endpoints ---------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(SESSIONS),
        "engine_dir": str(ENGINE_DIR),
        "frontend_dir": str(FRONTEND_DIR),
        "opendraft_in_path": str(ENGINE_DIR) in sys.path,
    }


@app.get("/api/config")
async def get_config():
    """Expose runtime config to the frontend (model choices, etc.)."""
    return {
        "paper_types": [
            {"id": "research_paper", "label": "Research Paper (5-15 pages)"},
            {"id": "bachelor", "label": "Bachelor Thesis (20-40 pages)"},
            {"id": "master", "label": "Master Thesis (40-80 pages)"},
            {"id": "phd", "label": "PhD Dissertation (80-150 pages)"},
        ],
        "output_types": [
            {"id": "full", "label": "Full draft (10-20 min)"},
            {"id": "expose", "label": "Research expose only (~3x faster)"},
        ],
        "languages": [
            {"id": "en", "label": "English"},
            {"id": "es", "label": "Español"},
            {"id": "pt", "label": "Português"},
            {"id": "fr", "label": "Français"},
            {"id": "de", "label": "Deutsch"},
            {"id": "it", "label": "Italiano"},
            {"id": "zh", "label": "中文"},
            {"id": "ja", "label": "日本語"},
            {"id": "ko", "label": "한국어"},
            {"id": "ru", "label": "Русский"},
            {"id": "ar", "label": "العربية"},
        ],
        "citation_styles": [
            {"id": "apa", "label": "APA"},
            {"id": "ieee", "label": "IEEE"},
            {"id": "chicago", "label": "Chicago"},
            {"id": "mla", "label": "MLA"},
        ],
    }


@app.post("/api/chat/start")
async def start_chat(req: ChatStartRequest):
    """Kick off generation and return the new session id."""
    session_id = uuid.uuid4().hex[:12]

    session = ChatSession(
        id=session_id,
        topic=req.topic.strip(),
        paper_type=req.paper_type,
        language=req.language,
        output_type=req.output_type,
        blurb=req.blurb,
        author_name=req.author_name,
        institution=req.institution,
        citation_style=req.citation_style,
    )

    tracker = LocalTracker(session_id)
    session._tracker = tracker  # type: ignore[attr-defined]

    with SESSION_LOCK:
        SESSIONS[session_id] = session

    # Run in a background thread so the request returns immediately
    worker = threading.Thread(
        target=run_generation,
        args=(session, tracker),
        daemon=True,
        name=f"opendraft-{session_id}",
    )
    worker.start()

    return {"session_id": session_id, "status": "started"}


@app.get("/api/chat/{session_id}/stream")
async def stream_chat(session_id: str, request: Request):
    """Server-Sent Events stream of progress + activity + result."""
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")

    tracker: LocalTracker = session._tracker  # type: ignore[attr-defined]

    async def event_gen():
        # Send the current snapshot immediately so reconnecting clients see state
        snapshot = {
            "type": "snapshot",
            "session_id": session_id,
            "status": session.status,
            "progress_percent": session.progress_percent,
            "current_phase": session.current_phase,
            "activity_log": session.activity_log[-50:],
            "sources": session.sources[-30:],
            "topic": session.topic,
        }
        yield {"event": "snapshot", "data": json.dumps(snapshot)}

        last_keepalive = time.time()
        terminal_sent = False

        while True:
            if await request.is_disconnected():
                break

            try:
                evt = tracker._events.get(timeout=1.0)
            except queue.Empty:
                evt = None

            if evt is not None:
                if evt.get("type") == "__final__":
                    terminal_sent = True
                    yield {"event": "done", "data": json.dumps({"status": session.status})}
                    break

                yield {"event": evt.get("type", "message"), "data": json.dumps(evt)}

            # Keepalive ping every 15s
            if time.time() - last_keepalive > 15:
                last_keepalive = time.time()
                yield {"event": "ping", "data": json.dumps({"ts": time.time()})}

            # If the worker finalized without sending a final event somehow, exit
            if not terminal_sent and session.status in ("completed", "error", "cancelled"):
                # Drain a tiny bit more then close
                await asyncio.sleep(0.3)
                yield {"event": "done", "data": json.dumps({"status": session.status})}
                break

    return EventSourceResponse(event_gen())


@app.get("/api/chat/{session_id}/status")
async def get_status(session_id: str):
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")

    return {
        "session_id": session.id,
        "status": session.status,
        "progress_percent": session.progress_percent,
        "current_phase": session.current_phase,
        "topic": session.topic,
        "error": session.error,
        "artifacts": session.artifacts,
        "word_count": session.word_count,
        "citation_count": session.citation_count,
        "activity_log_count": len(session.activity_log),
        "sources_count": len(session.sources),
        "created_at": session.created_at,
        "started_at": session.started_at,
        "finished_at": session.finished_at,
    }


@app.post("/api/chat/{session_id}/cancel")
async def cancel_chat(session_id: str):
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")

    tracker: LocalTracker = session._tracker  # type: ignore[attr-defined]
    tracker.request_cancel()
    return {"ok": True}


@app.get("/api/chat/{session_id}/download/{fmt}")
async def download(session_id: str, fmt: str):
    fmt = fmt.lower()
    if fmt not in ("md", "pdf", "docx", "zip", "tex"):
        raise HTTPException(400, f"Unsupported format: {fmt}")

    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")

    rel = session.artifacts.get(fmt)
    if not rel:
        raise HTTPException(404, f"No {fmt} artifact for this session")

    path = (ROOT / rel).resolve()
    if not path.exists():
        raise HTTPException(404, f"File missing: {path}")

    media_types = {
        "md": "text/markdown",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "zip": "application/zip",
        "tex": "application/x-tex",
    }
    return FileResponse(
        path,
        media_type=media_types[fmt],
        filename=path.name,
    )


@app.get("/api/chat/history")
async def history(limit: int = 50):
    """Return recent sessions (most recent first)."""
    items = sorted(SESSIONS.values(), key=lambda s: s.created_at, reverse=True)[:limit]
    return [
        {
            "session_id": s.id,
            "topic": s.topic,
            "paper_type": s.paper_type,
            "language": s.language,
            "status": s.status,
            "word_count": s.word_count,
            "citation_count": s.citation_count,
            "created_at": s.created_at,
        }
        for s in items
    ]


# -- Static frontend -------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def root():
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "frontend/index.html not found"}, status_code=500)
else:
    @app.get("/")
    async def root():
        return JSONResponse({"detail": f"frontend dir not found: {FRONTEND_DIR}"}, status_code=500)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    log.info("OpenDraft Chat starting on http://%s:%d", host, port)
    log.info("Frontend: %s", FRONTEND_DIR)
    log.info("Engine:   %s", ENGINE_DIR)
    log.info("Outputs:  %s", OUTPUTS_DIR)
    uvicorn.run("server:app", host=host, port=port, reload=False, log_level="info")