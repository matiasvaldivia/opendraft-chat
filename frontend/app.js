/* ============================================================================
   OpenDraft Chat — Frontend app
   ============================================================================ */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const chatInner = $("#chat-inner");
const input = $("#input");
const btnSend = $("#btn-send");
const btnNew = $("#btn-new-chat");
const btnCancel = $("#btn-cancel");
const topbarTitle = $("#topbar-title");
const topbarActions = $("#topbar-actions");
const historyList = $("#history-list");

// Pipeline phase config (mirrors the 6 stages the engine reports)
const PHASES = [
    { id: "research",  emoji: "🔍", label: "Research" },
    { id: "structure", emoji: "📋", label: "Outline" },
    { id: "writing",   emoji: "✍️",  label: "Writing" },
    { id: "compiling", emoji: "🔧", label: "Compile" },
    { id: "exporting", emoji: "📄", label: "Export" },
    { id: "completed", emoji: "✅", label: "Done" },
];

const state = {
    currentSessionId: null,
    eventSource: null,
    startedAt: null,
    completed: false,
};

// -- Utilities --------------------------------------------------------------
function escapeHtml(s) {
    return String(s ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function formatTime(iso) {
    if (!iso) return "";
    try {
        const d = new Date(iso);
        return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch { return ""; }
}

function formatDuration(seconds) {
    if (!seconds || seconds < 0) return "—";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    if (m === 0) return `${s}s`;
    return `${m}m ${s.toString().padStart(2, "0")}s`;
}

function autoscroll() {
    const chat = $("#chat");
    requestAnimationFrame(() => {
        chat.scrollTop = chat.scrollHeight;
    });
}

// -- Empty state ------------------------------------------------------------
function renderEmpty() {
    chatInner.innerHTML = `
        <div class="empty">
            <div class="empty-hero">📚</div>
            <div class="empty-title">What should I research?</div>
            <div class="empty-desc">
                I'll run 19 specialized agents to draft an academic paper with citations verified against
                CrossRef, OpenAlex, Semantic Scholar and arXiv. The whole thing takes 10–20 minutes.
            </div>
            <div class="examples">
                <button class="example-chip" data-example="The impact of transformer architectures on clinical NLP between 2020 and 2025">
                    <strong>🧬 Clinical NLP</strong><br>
                    The impact of transformer architectures on clinical NLP between 2020 and 2025
                </button>
                <button class="example-chip" data-example="A systematic review of microplastic pollution in freshwater ecosystems">
                    <strong>🌊 Microplastics</strong><br>
                    A systematic review of microplastic pollution in freshwater ecosystems
                </button>
                <button class="example-chip" data-example="Carbon capture technologies: economic viability and deployment barriers">
                    <strong>🌱 Carbon capture</strong><br>
                    Carbon capture technologies: economic viability and deployment barriers
                </button>
                <button class="example-chip" data-example="Decentralized finance and the future of monetary policy: a literature review">
                    <strong>💱 DeFi</strong><br>
                    Decentralized finance and the future of monetary policy: a literature review
                </button>
                <button class="example-chip" data-example="Quantum error correction: progress, challenges and applications to cryptography">
                    <strong>⚛️ Quantum</strong><br>
                    Quantum error correction: progress, challenges and applications to cryptography
                </button>
                <button class="example-chip" data-example="Mental health effects of remote work: a meta-analysis of post-pandemic studies">
                    <strong>🧠 Remote work</strong><br>
                    Mental health effects of remote work: a meta-analysis of post-pandemic studies
                </button>
            </div>
        </div>
    `;
    $$(".example-chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            input.value = chip.dataset.example;
            input.focus();
            autoResize();
        });
    });
}

// -- Auto-resize textarea ---------------------------------------------------
function autoResize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
}
input.addEventListener("input", autoResize);

// -- Send / Cancel ----------------------------------------------------------
btnSend.addEventListener("click", sendMessage);
btnCancel.addEventListener("click", cancelCurrent);
btnNew.addEventListener("click", () => {
    closeStream();
    state.currentSessionId = null;
    state.completed = false;
    topbarTitle.textContent = "New research";
    topbarActions.style.display = "none";
    btnSend.disabled = false;
    input.value = "";
    autoResize();
    renderEmpty();
    input.focus();
});

input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

async function sendMessage() {
    const topic = input.value.trim();
    if (!topic || btnSend.disabled) return;
    if (state.currentSessionId && !state.completed) return; // already running

    state.startedAt = Date.now();
    state.completed = false;

    const paperType = $("#opt-paper-type").value;
    const outputType = $("#opt-output-type").value;
    const language = $("#opt-language").value;
    const citationStyle = $("#opt-citation").value;

    // Clear and build user/assistant message pair
    chatInner.innerHTML = "";
    renderUserMessage(topic, { paperType, outputType, language, citationStyle });
    const statusEl = renderStatusPanel(topic);
    autoscroll();

    btnSend.disabled = true;
    topbarTitle.textContent = topic;
    topbarActions.style.display = "flex";
    const badge = document.getElementById("topbar-badge");
    if (badge) badge.style.display = "inline-flex";

    try {
        const res = await fetch("/api/chat/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                topic,
                paper_type: paperType,
                output_type: outputType,
                language,
                citation_style: citationStyle,
            }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
        const data = await res.json();
        state.currentSessionId = data.session_id;
        openStream(data.session_id, statusEl);
        loadHistory();
    } catch (err) {
        showError(statusEl, `Failed to start: ${err.message}`);
        btnSend.disabled = false;
        topbarActions.style.display = "none";
    }
}

async function cancelCurrent() {
    if (!state.currentSessionId) return;
    try {
        await fetch(`/api/chat/${state.currentSessionId}/cancel`, { method: "POST" });
    } catch {}
}

// -- Renderers --------------------------------------------------------------
function renderUserMessage(topic, opts) {
    const modeLabels = {
        research_paper: "Research paper",
        bachelor: "Bachelor",
        master: "Master",
        phd: "PhD",
    };
    const langNames = { en: "English", es: "Spanish", pt: "Portuguese", fr: "French", de: "German", it: "Italian", zh: "Chinese", ja: "Japanese", ko: "Korean", ru: "Russian", ar: "Arabic" };
    const mode = opts.outputType === "expose" ? "Research expose" : "Full draft";

    const html = `
        <div class="msg user">
            <div class="msg-avatar">M</div>
            <div class="msg-body">
                <div class="msg-author">You</div>
                <div class="msg-content"><p>${escapeHtml(topic)}</p></div>
                <div style="font-size:11px;color:var(--text-3);margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
                    <span>${escapeHtml(modeLabels[opts.paperType])}</span>
                    <span>·</span>
                    <span>${escapeHtml(mode)}</span>
                    <span>·</span>
                    <span>${escapeHtml(langNames[opts.language] || opts.language)}</span>
                    <span>·</span>
                    <span>${escapeHtml(opts.citationStyle.toUpperCase())}</span>
                </div>
            </div>
        </div>
    `;
    chatInner.insertAdjacentHTML("beforeend", html);
}

function renderStatusPanel(topic) {
    const html = `
        <div class="msg assistant">
            <div class="msg-avatar">OD</div>
            <div class="msg-body">
                <div class="msg-author">OpenDraft · 19 agents working</div>
                <div class="msg-content">
                    <div class="status-card" data-status-card>
                        <div class="status-row">
                            <div class="status-phase">
                                <span data-phase-emoji>🔍</span>
                                <span data-phase-label>Initializing…</span>
                            </div>
                            <div class="status-percent"><span data-percent>0</span>%</div>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill" data-progress style="width:0%"></div>
                        </div>
                        <div class="pipeline" data-pipeline>
                            ${PHASES.map((p) => `
                                <div class="pipe-step" data-pipe="${p.id}">
                                    <span class="pipe-emoji">${p.emoji}</span>
                                    ${escapeHtml(p.label)}
                                </div>
                            `).join("")}
                        </div>
                        <div class="activity-feed" data-feed>
                            <div class="activity-entry">
                                <span class="activity-icon">⏳</span>
                                <span class="activity-msg">Starting pipeline for "${escapeHtml(topic)}"</span>
                            </div>
                        </div>
                        <div class="sources-panel" data-sources-panel style="display:none">
                            <div class="sources-title">📚 Verified sources (<span data-sources-count>0</span>)</div>
                            <div data-sources-list></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
    chatInner.insertAdjacentHTML("beforeend", html);
    return chatInner.querySelector("[data-status-card]");
}

function showError(statusEl, msg) {
    statusEl.outerHTML = `
        <div class="msg assistant">
            <div class="msg-avatar">OD</div>
            <div class="msg-body">
                <div class="msg-author">OpenDraft</div>
                <div class="msg-content">
                    <div class="error-card">
                        <h4>❌ Could not start generation</h4>
                        <pre>${escapeHtml(msg)}</pre>
                        <p style="margin:8px 0 0;font-size:13px">
                            Make sure <code>GOOGLE_API_KEY</code> is set in <code>opendraft/.env</code> and that the opendraft dependencies are installed.
                        </p>
                    </div>
                </div>
            </div>
        </div>
    `;
    autoscroll();
}

// -- SSE stream -------------------------------------------------------------
function openStream(sessionId, statusEl) {
    closeStream();
    const root = statusEl.closest(".msg");
    const els = {
        card: statusEl,
        phaseEmoji: root.querySelector("[data-phase-emoji]"),
        phaseLabel: root.querySelector("[data-phase-label]"),
        percent: root.querySelector("[data-percent]"),
        progress: root.querySelector("[data-progress]"),
        pipeline: root.querySelector("[data-pipeline]"),
        feed: root.querySelector("[data-feed]"),
        sourcesPanel: root.querySelector("[data-sources-panel]"),
        sourcesList: root.querySelector("[data-sources-list]"),
        sourcesCount: root.querySelector("[data-sources-count]"),
    };

    const es = new EventSource(`/api/chat/${sessionId}/stream`);
    state.eventSource = es;

    let sourcesCount = 0;

    function appendFeed(entry) {
        const el = document.createElement("div");
        el.className = `activity-entry ${escapeHtml(entry.type || "info")}`;
        el.innerHTML = `
            <span class="activity-icon">${entry.icon || "•"}</span>
            <span class="activity-msg">${escapeHtml(entry.message)}</span>
        `;
        els.feed.appendChild(el);
        els.feed.scrollTop = els.feed.scrollHeight;
        // Trim if too long
        while (els.feed.children.length > 80) {
            els.feed.removeChild(els.feed.firstChild);
        }
    }

    function appendSource(src) {
        sourcesCount++;
        els.sourcesCount.textContent = sourcesCount;
        els.sourcesPanel.style.display = "block";
        const year = src.year ? ` (${src.year})` : "";
        const verified = src.verified ? "✓ " : "";
        const html = `
            <div class="source-chip">
                <div>${verified}${escapeHtml(src.title || "Untitled")}${year}</div>
                <div class="src-meta">
                    ${escapeHtml(src.author_str || "")}
                    ${src.doi ? ` · DOI: ${escapeHtml(src.doi)}` : ""}
                </div>
            </div>
        `;
        els.sourcesList.insertAdjacentHTML("beforeend", html);
        if (els.sourcesList.children.length > 30) {
            els.sourcesList.removeChild(els.sourcesList.firstChild);
        }
    }

    function markPipeline(phaseId) {
        const order = PHASES.map(p => p.id);
        const idx = order.indexOf(phaseId);
        if (idx === -1) return;
        $$(".pipe-step", els.pipeline).forEach((step) => {
            const stepIdx = order.indexOf(step.dataset.pipe);
            step.classList.remove("active", "done");
            if (stepIdx < idx) step.classList.add("done");
            else if (stepIdx === idx) step.classList.add("active");
        });
    }

    es.addEventListener("snapshot", (ev) => {
        const s = JSON.parse(ev.data);
        if (s.activity_log) {
            els.feed.innerHTML = "";
            s.activity_log.slice(-30).forEach(appendFeed);
        }
    });

    es.addEventListener("phase", (ev) => {
        const d = JSON.parse(ev.data);
        const phaseDef = PHASES.find(p => p.id === d.phase) || { emoji: "📌", label: d.phase };
        els.phaseEmoji.textContent = phaseDef.emoji;
        els.phaseLabel.textContent = humanizeStage(d.stage || d.phase);
        els.percent.textContent = d.progress_percent;
        els.progress.style.width = d.progress_percent + "%";
        markPipeline(d.phase);
        if (d.activity_entry) appendFeed(d.activity_entry);
        autoscroll();
    });

    es.addEventListener("activity", (ev) => {
        const d = JSON.parse(ev.data);
        if (d.activity_entry) appendFeed(d.activity_entry);
        autoscroll();
    });

    es.addEventListener("source", (ev) => {
        const d = JSON.parse(ev.data);
        appendSource(d.source);
        autoscroll();
    });

    es.addEventListener("result", (ev) => {
        const d = JSON.parse(ev.data);
        renderResult(els.card, d);
        state.completed = true;
    });

    es.addEventListener("cancelled", () => {
        els.card.innerHTML = `
            <div class="status-row">
                <div class="status-phase"><span>⏹️</span><span>Cancelled</span></div>
            </div>
            <div class="activity-entry"><span class="activity-icon">ℹ️</span><span class="activity-msg">Generation cancelled by user.</span></div>
        `;
        state.completed = true;
        enableComposer();
    });

    es.addEventListener("error", (ev) => {
        // EventSource error or server error event
        if (ev.data) {
            try {
                const d = JSON.parse(ev.data);
                showError(els.card, d.error + (d.trace ? "\n\n" + d.trace : ""));
                state.completed = true;
                enableComposer();
                return;
            } catch {}
        }
        // network-level error: don't reset UI; let reconnect try
    });

    es.addEventListener("done", () => {
        closeStream();
        enableComposer();
    });

    es.addEventListener("ping", () => {});
}

function closeStream() {
    if (state.eventSource) {
        try { state.eventSource.close(); } catch {}
        state.eventSource = null;
    }
}

function enableComposer() {
    btnSend.disabled = false;
    topbarActions.style.display = "none";
    const badge = document.getElementById("topbar-badge");
    if (badge) badge.style.display = "none";
}

function humanizeStage(stage) {
    if (!stage) return "Working";
    return String(stage)
        .replace(/_/g, " ")
        .replace(/\b\w/g, c => c.toUpperCase());
}

function renderResult(cardEl, data) {
    const sessionId = state.currentSessionId;
    const elapsed = data.elapsed_seconds || 0;
    const artifacts = data.artifacts || {};
    const hasArtifact = (fmt) => !!artifacts[fmt];

    const dl = (fmt, label, primary = false) =>
        hasArtifact(fmt)
            ? `<a class="btn-download ${primary ? "" : "secondary"}" href="/api/chat/${sessionId}/download/${fmt}" target="_blank" download>
                <span>⬇</span> ${escapeHtml(label)}
            </a>`
            : "";

    cardEl.outerHTML = `
        <div class="msg assistant">
            <div class="msg-avatar">OD</div>
            <div class="msg-body">
                <div class="msg-author">OpenDraft</div>
                <div class="msg-content">
                    <div class="result-card">
                        <div class="result-header">
                            <span style="font-size:24px">🎉</span>
                            <h3>Draft ready</h3>
                        </div>
                        <p style="margin:0 0 14px;color:var(--text-1)">
                            Your research paper has been drafted with verified citations. The full draft, citations and bibliography are available below.
                        </p>
                        <div class="result-stats">
                            <div class="stat-box">
                                <div class="stat-num">${(data.word_count || 0).toLocaleString()}</div>
                                <div class="stat-label">Words</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-num">${(data.citation_count || 0).toLocaleString()}</div>
                                <div class="stat-label">Citations</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-num">${formatDuration(elapsed)}</div>
                                <div class="stat-label">Generation time</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-num">${Object.keys(artifacts).length}</div>
                                <div class="stat-label">Formats</div>
                            </div>
                        </div>
                        <div class="download-row">
                            ${dl("pdf", "PDF", true)}
                            ${dl("docx", "Word", true)}
                            ${dl("md", "Markdown")}
                            ${dl("tex", "LaTeX")}
                            ${dl("zip", "Bundle (.zip)")}
                        </div>
                        <div class="result-actions">
                            <button class="btn-link" onclick="loadHistory();window.scrollTo({top:0,behavior:'smooth'})">↻ Refresh history</button>
                            <button class="btn-link" onclick="navigator.clipboard.writeText(window.location.origin + '/api/chat/${sessionId}/status')">🔗 Copy session link</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
    autoscroll();
}

// -- History sidebar --------------------------------------------------------
async function loadHistory() {
    try {
        const res = await fetch("/api/chat/history?limit=50");
        if (!res.ok) return;
        const items = await res.json();
        if (!items.length) {
            historyList.innerHTML = `
                <div style="text-align:center;color:var(--text-3);font-size:12px;padding:24px 12px">
                    No previous sessions yet
                </div>
            `;
            return;
        }
        historyList.innerHTML = items.map((s) => `
            <div class="history-item ${s.session_id === state.currentSessionId ? "active" : ""}" data-sid="${escapeHtml(s.session_id)}">
                <div class="history-topic">${escapeHtml(s.topic)}</div>
                <div class="history-meta">
                    <span class="status-dot ${escapeHtml(s.status)}"></span>
                    <span>${escapeHtml(s.status)}</span>
                    <span>·</span>
                    <span>${formatTime(s.created_at)}</span>
                    ${s.word_count ? `<span>· ${s.word_count.toLocaleString()}w</span>` : ""}
                </div>
            </div>
        `).join("");
        $$(".history-item", historyList).forEach((el) => {
            el.addEventListener("click", () => attachToSession(el.dataset.sid, items.find(s => s.session_id === el.dataset.sid)));
        });
    } catch (err) {
        // Silent
    }
}

async function attachToSession(sid, meta) {
    if (!meta) return;
    closeStream();
    state.currentSessionId = sid;
    state.completed = meta.status === "completed";
    topbarTitle.textContent = meta.topic;

    // Render user message + result card snapshot
    chatInner.innerHTML = "";
    renderUserMessage(meta.topic, {
        paperType: meta.paper_type || "research_paper",
        outputType: "full",
        language: meta.language || "en",
        citationStyle: "apa",
    });
    if (meta.status === "completed") {
        renderResultInline(sid, meta);
    } else {
        const card = renderStatusPanel(meta.topic);
        openStream(sid, card);
    }
    autoscroll();
}

async function renderResultInline(sid, meta) {
    try {
        const res = await fetch(`/api/chat/${sid}/status`);
        const data = await res.json();
        const placeholder = document.createElement("div");
        placeholder.className = "msg assistant";
        placeholder.innerHTML = `
            <div class="msg-avatar">OD</div>
            <div class="msg-body">
                <div class="msg-author">OpenDraft</div>
                <div class="msg-content"><div class="result-card" data-tmp></div></div>
            </div>
        `;
        chatInner.appendChild(placeholder);
        const card = placeholder.querySelector("[data-tmp]");
        renderResult(card, { ...data, elapsed_seconds: (data.finished_at || 0) - (data.started_at || 0) });
    } catch (err) {
        // ignore
    }
}

// -- Init -------------------------------------------------------------------
(async function init() {
    renderEmpty();
    await loadHistory();
    input.focus();
})();