"""
ARCH Web Dashboard

Serves a web-based dashboard from the existing MCP server.
Replaces the Textual TUI with a browser-based UI at /dashboard.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("arch.web_dashboard")


class DashboardEventBroadcaster:
    """Manages SSE connections to dashboard browser clients."""

    MAX_QUEUE_SIZE = 100

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()

    def add_client(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._clients.add(queue)
        return queue

    def remove_client(self, queue: asyncio.Queue) -> None:
        self._clients.discard(queue)

    def broadcast(self, event_type: str, data: dict) -> None:
        payload = json.dumps(data)
        dead = []
        for q in self._clients:
            try:
                q.put_nowait((event_type, payload))
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._clients.discard(q)

    @property
    def client_count(self) -> int:
        return len(self._clients)


def get_dashboard_routes(
    state,
    token_tracker,
    event_log_path: Optional[Path],
    broadcaster: DashboardEventBroadcaster,
) -> list[Route]:
    """Create Starlette routes for the web dashboard."""

    async def handle_dashboard_page(request: Request) -> Response:
        return HTMLResponse(dashboard_html())

    async def handle_dashboard_sse(request: Request) -> Response:
        queue = broadcaster.add_client()

        async def event_generator():
            try:
                while True:
                    try:
                        event_type, payload = await asyncio.wait_for(
                            queue.get(), timeout=15.0
                        )
                        yield f"event: {event_type}\ndata: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                broadcaster.remove_client(queue)

        from starlette.responses import StreamingResponse
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def handle_dashboard_state(request: Request) -> Response:
        agents = state.list_agents()
        messages = state.get_all_messages()
        decisions = state.get_pending_decisions()
        project = dict(state._state.get("project", {}))
        costs = token_tracker.get_all_usage() if token_tracker else {}
        return JSONResponse({
            "project": project,
            "agents": agents,
            "messages": messages,
            "decisions": decisions,
            "costs": costs,
        })

    async def handle_dashboard_messages(request: Request) -> Response:
        return JSONResponse(state.get_all_messages())

    async def handle_dashboard_events_log(request: Request) -> Response:
        events = []
        if event_log_path and event_log_path.exists():
            try:
                text = event_log_path.read_text()
                for line in text.strip().split("\n"):
                    if line.strip():
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass
        return JSONResponse(events)

    async def handle_dashboard_send(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
        content = body.get("content", "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "Empty message"}, status_code=400)
        state.add_message("user", "archie", content)
        return JSONResponse({"ok": True})

    return [
        Route("/dashboard", handle_dashboard_page),
        Route("/api/dashboard/events", handle_dashboard_sse),
        Route("/api/dashboard/state", handle_dashboard_state),
        Route("/api/dashboard/messages", handle_dashboard_messages),
        Route("/api/dashboard/events-log", handle_dashboard_events_log),
        Route("/api/dashboard/send", handle_dashboard_send, methods=["POST"]),
    ]


def dashboard_html() -> str:
    """Return the complete single-page dashboard HTML."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARCH Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

:root {
  --bg: #0f1117;
  --panel: #1a1d28;
  --panel-border: #2a2d3a;
  --text: #e0e0e0;
  --text-dim: #888;
  --accent: #4a9eff;
  --green: #4caf50;
  --yellow: #ffc107;
  --red: #f44336;
  --orange: #ff9800;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* Header */
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--panel-border);
  min-height: 48px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--accent);
}
.header-project {
  font-size: 14px;
  color: var(--text-dim);
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 13px;
  color: var(--text-dim);
}
.connection-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.connection-dot.connected { background: var(--green); }
.connection-dot.disconnected { background: var(--red); }
.help-btn {
  background: none;
  border: 1px solid var(--panel-border);
  color: var(--text-dim);
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}
.help-btn:hover { border-color: var(--accent); color: var(--accent); }

/* Main Layout */
.main {
  flex: 1;
  display: grid;
  grid-template-columns: 220px 1fr;
  overflow: hidden;
}
.main.show-costs {
  grid-template-columns: 220px 1fr 240px;
}

/* Panels */
.panel {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--panel-border);
  overflow: hidden;
}
.panel:last-child { border-right: none; }
.panel-header {
  padding: 12px 16px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-dim);
  border-bottom: 1px solid var(--panel-border);
  background: var(--panel);
}
.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

/* Agents Panel */
.agent-item {
  padding: 10px 16px;
  cursor: pointer;
  transition: background 0.15s;
}
.agent-item:hover { background: rgba(74, 158, 255, 0.08); }
.agent-item.selected { background: rgba(74, 158, 255, 0.15); }
.agent-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 500;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-dot.working { background: var(--green); }
.status-dot.done { background: var(--green); }
.status-dot.blocked { background: var(--yellow); }
.status-dot.waiting_review { background: var(--yellow); }
.status-dot.idle { background: var(--text-dim); }
.status-dot.error { background: var(--red); }
.agent-tags {
  display: flex;
  gap: 4px;
  margin-left: 16px;
}
.agent-tag {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(255,255,255,0.08);
  color: var(--text-dim);
}
.agent-task {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 4px;
  margin-left: 16px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.agent-status-text {
  font-size: 11px;
  color: var(--text-dim);
  margin-left: 16px;
  margin-top: 2px;
}

/* Activity Log */
.activity-entry {
  padding: 4px 16px;
  font-size: 13px;
  line-height: 1.5;
  font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
}
.activity-time {
  color: var(--text-dim);
  margin-right: 8px;
  font-size: 12px;
}
.activity-sender {
  color: var(--accent);
  margin-right: 8px;
  font-weight: 500;
}
.activity-content {
  color: var(--text);
  word-break: break-word;
}
.activity-entry.stderr .activity-content {
  color: var(--text-dim);
}

/* Costs Panel */
.cost-item {
  padding: 8px 16px;
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}
.cost-agent { color: var(--text); }
.cost-value { color: var(--text-dim); font-family: "SF Mono", monospace; }
.cost-total {
  padding: 10px 16px;
  border-top: 1px solid var(--panel-border);
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  font-size: 14px;
}
.budget-bar-container {
  margin: 12px 16px;
}
.budget-label {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-dim);
  margin-bottom: 4px;
}
.budget-bar {
  height: 6px;
  background: rgba(255,255,255,0.1);
  border-radius: 3px;
  overflow: hidden;
}
.budget-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.5s, background 0.5s;
}
.budget-fill.normal { background: var(--green); }
.budget-fill.warning { background: var(--yellow); }
.budget-fill.danger { background: var(--red); }

/* Escalation Panel */
.escalation {
  border-top: 2px solid var(--panel-border);
  background: var(--panel);
  transition: border-color 0.3s;
}
.escalation.active {
  border-top-color: var(--orange);
}
.escalation-content {
  padding: 16px 20px;
}
.escalation-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--orange);
  margin-bottom: 8px;
}
.escalation-question {
  font-size: 14px;
  line-height: 1.6;
  margin-bottom: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.escalation-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.escalation-btn {
  padding: 8px 20px;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.15s;
}
.escalation-btn:hover { transform: translateY(-1px); box-shadow: 0 2px 8px rgba(0,0,0,0.3); }
.escalation-btn:active { transform: translateY(0); }
.escalation-btn.primary {
  background: var(--accent);
  color: white;
}
.escalation-btn.secondary {
  background: rgba(255,255,255,0.1);
  color: var(--text);
}
.escalation-input-row {
  display: flex;
  gap: 8px;
}
.escalation-input {
  flex: 1;
  padding: 10px 14px;
  background: var(--bg);
  border: 1px solid var(--panel-border);
  border-radius: 6px;
  color: var(--text);
  font-size: 14px;
  outline: none;
}
.escalation-input:focus { border-color: var(--accent); }
.escalation-input::placeholder { color: var(--text-dim); }
.escalation-send {
  padding: 10px 20px;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
  font-weight: 500;
}
.escalation-send:hover { background: #5aadff; }

/* Message input (when no escalation) */
.message-bar {
  padding: 12px 20px;
  border-top: 1px solid var(--panel-border);
  background: var(--panel);
}
.message-input-row {
  display: flex;
  gap: 8px;
}
.message-input {
  flex: 1;
  padding: 10px 14px;
  background: var(--bg);
  border: 1px solid var(--panel-border);
  border-radius: 6px;
  color: var(--text);
  font-size: 14px;
  outline: none;
}
.message-input:focus { border-color: var(--accent); }
.message-input::placeholder { color: var(--text-dim); }
.message-send {
  padding: 10px 20px;
  background: rgba(255,255,255,0.1);
  color: var(--text);
  border: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
}
.message-send:hover { background: rgba(255,255,255,0.15); }

/* Modal */
.modal-overlay {
  display: none;
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.7);
  z-index: 100;
  justify-content: center;
  align-items: center;
}
.modal-overlay.visible { display: flex; }
.modal {
  background: var(--panel);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  width: 80%;
  max-width: 900px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--panel-border);
}
.modal-title { font-size: 16px; font-weight: 600; }
.modal-close {
  background: none;
  border: none;
  color: var(--text-dim);
  font-size: 20px;
  cursor: pointer;
  padding: 4px 8px;
}
.modal-close:hover { color: var(--text); }
.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  font-family: "SF Mono", "Fira Code", monospace;
  font-size: 13px;
  line-height: 1.6;
}
.modal-entry {
  padding: 4px 0;
  border-bottom: 1px solid rgba(255,255,255,0.03);
}
.modal-entry .time { color: var(--text-dim); }
.modal-entry .sender { color: var(--accent); font-weight: 500; }
.modal-entry .arrow { color: var(--text-dim); }
.modal-entry .recipient { color: var(--orange); }
.modal-entry .content { color: var(--text); }
.modal-entry .tool { color: var(--green); }
.modal-entry .status-ok { color: var(--green); }
.modal-entry .status-error { color: var(--red); }
.modal-entry .duration { color: var(--text-dim); }

/* Toast */
.toast {
  position: fixed;
  bottom: 80px;
  right: 20px;
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 14px;
  z-index: 200;
  opacity: 0;
  transform: translateY(10px);
  transition: opacity 0.3s, transform 0.3s;
}
.toast.visible { opacity: 1; transform: translateY(0); }
.toast.success { background: var(--green); color: white; }
.toast.error { background: var(--red); color: white; }

/* Scrollbar styling */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.25); }

/* Help content */
.help-grid {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 8px 16px;
  font-family: -apple-system, sans-serif;
  font-size: 14px;
}
.help-key {
  font-family: "SF Mono", monospace;
  background: rgba(255,255,255,0.08);
  padding: 2px 8px;
  border-radius: 4px;
  text-align: center;
  font-size: 13px;
}
.help-desc { color: var(--text-dim); }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="header-title">ARCH</div>
    <div class="header-project" id="project-name">—</div>
  </div>
  <div class="header-right">
    <span id="runtime">00:00:00</span>
    <span><span class="connection-dot disconnected" id="conn-dot"></span> <span id="conn-text">Connecting</span></span>
    <button class="help-btn" onclick="showModal(\'help\')">?</button>
  </div>
</div>

<!-- Main Content -->
<div class="main" id="main-grid">
  <!-- Agents Panel -->
  <div class="panel">
    <div class="panel-header">Agents</div>
    <div class="panel-body" id="agents-list"></div>
  </div>

  <!-- Activity Log -->
  <div class="panel">
    <div class="panel-header">Activity Log</div>
    <div class="panel-body" id="activity-log"></div>
  </div>

  <!-- Costs Panel (hidden by default) -->
  <div class="panel" id="costs-panel" style="display:none">
    <div class="panel-header">Costs</div>
    <div class="panel-body" id="costs-list"></div>
  </div>
</div>

<!-- Escalation / Message Bar -->
<div id="bottom-bar">
  <!-- Escalation (shown when there's a pending decision) -->
  <div class="escalation" id="escalation-panel" style="display:none">
    <div class="escalation-content">
      <div class="escalation-label" id="escalation-label">Archie Asks</div>
      <div class="escalation-question" id="escalation-question"></div>
      <div class="escalation-buttons" id="escalation-buttons"></div>
      <div class="escalation-input-row">
        <input type="text" class="escalation-input" id="escalation-input" placeholder="Or type a custom answer...">
        <button class="escalation-send" onclick="sendEscalationInput()">Send</button>
      </div>
    </div>
  </div>

  <!-- Message bar (shown when no escalation) -->
  <div class="message-bar" id="message-bar">
    <div class="message-input-row">
      <input type="text" class="message-input" id="message-input" placeholder="Send a message to Archie...">
      <button class="message-send" onclick="sendMessage()">Send</button>
    </div>
  </div>
</div>

<!-- Modal Overlay -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="modal-title">Modal</div>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
// State
let state = { project: {}, agents: [], messages: [], decisions: [], costs: {} };
let currentDecision = null;
let showCosts = false;
let startTime = null;
let connected = false;
let autoScroll = true;
let seenMessageIds = new Set();

// SSE Connection
let evtSource = null;

function connectSSE() {
  evtSource = new EventSource("/api/dashboard/events");

  evtSource.onopen = () => {
    connected = true;
    updateConnectionStatus();
    loadFullState();
  };

  evtSource.onerror = () => {
    connected = false;
    updateConnectionStatus();
  };

  evtSource.addEventListener("agents", (e) => {
    state.agents = JSON.parse(e.data).agents;
    renderAgents();
  });

  evtSource.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    if (!seenMessageIds.has(msg.id)) {
      seenMessageIds.add(msg.id);
      state.messages.push(msg);
      addActivityEntry(msg);
    }
  });

  evtSource.addEventListener("escalation", (e) => {
    const decision = JSON.parse(e.data);
    currentDecision = decision;
    renderEscalation();
  });

  evtSource.addEventListener("escalation_cleared", (e) => {
    currentDecision = null;
    renderEscalation();
  });

  evtSource.addEventListener("costs", (e) => {
    state.costs = JSON.parse(e.data);
    renderCosts();
  });

  evtSource.addEventListener("event_log", (e) => {
    // Could live-update event log modal if open
  });
}

async function loadFullState() {
  try {
    const resp = await fetch("/api/dashboard/state");
    const data = await resp.json();
    state = data;

    // Track start time from project
    if (state.project && state.project.started_at) {
      startTime = new Date(state.project.started_at);
    }

    // Update project name
    const nameEl = document.getElementById("project-name");
    nameEl.textContent = state.project?.name || "—";

    renderAgents();
    renderCosts();

    // Render all messages
    const log = document.getElementById("activity-log");
    log.innerHTML = "";
    seenMessageIds.clear();
    for (const msg of state.messages) {
      seenMessageIds.add(msg.id);
      addActivityEntry(msg);
    }

    // Check for pending decisions
    if (state.decisions && state.decisions.length > 0) {
      currentDecision = state.decisions[0];
    } else {
      currentDecision = null;
    }
    renderEscalation();
  } catch (err) {
    console.error("Failed to load state:", err);
  }
}

// Rendering
function renderAgents() {
  const list = document.getElementById("agents-list");
  list.innerHTML = "";
  for (const agent of state.agents) {
    const div = document.createElement("div");
    div.className = "agent-item";
    div.onclick = () => showAgentMessages(agent.id);

    let tags = "";
    if (agent.sandboxed) tags += \'<span class="agent-tag">docker</span>\';
    if (agent.skip_permissions) tags += \'<span class="agent-tag">!perms</span>\';

    const status = agent.status || "idle";
    div.innerHTML = `
      <div class="agent-name">
        <span class="status-dot ${status}"></span>
        ${escapeHtml(agent.id)}
      </div>
      ${tags ? \'<div class="agent-tags">\' + tags + "</div>" : ""}
      <div class="agent-status-text">${escapeHtml(status)}</div>
      ${agent.task ? \'<div class="agent-task">\' + escapeHtml(agent.task) + "</div>" : ""}
    `;
    list.appendChild(div);
  }
}

function addActivityEntry(msg) {
  const log = document.getElementById("activity-log");
  const div = document.createElement("div");

  const isStderr = msg.content && msg.content.includes("[stderr]");
  div.className = "activity-entry" + (isStderr ? " stderr" : "");

  const time = new Date(msg.timestamp);
  const hhmm = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

  div.innerHTML = `<span class="activity-time">${hhmm}</span><span class="activity-sender">${escapeHtml(msg.from)}</span><span class="activity-content">${escapeHtml(msg.content)}</span>`;
  log.appendChild(div);

  if (autoScroll) {
    log.scrollTop = log.scrollHeight;
  }
}

function renderCosts() {
  const list = document.getElementById("costs-list");
  list.innerHTML = "";
  let total = 0;

  const entries = Object.entries(state.costs);
  for (const [agentId, usage] of entries) {
    const cost = usage.cost_usd || 0;
    total += cost;
    const div = document.createElement("div");
    div.className = "cost-item";
    div.innerHTML = `<span class="cost-agent">${escapeHtml(agentId)}</span><span class="cost-value">$${cost.toFixed(4)}</span>`;
    list.appendChild(div);
  }

  const totalDiv = document.createElement("div");
  totalDiv.className = "cost-total";
  totalDiv.innerHTML = `<span>Total</span><span>$${total.toFixed(4)}</span>`;
  list.appendChild(totalDiv);
}

function renderEscalation() {
  const escPanel = document.getElementById("escalation-panel");
  const msgBar = document.getElementById("message-bar");

  if (currentDecision) {
    escPanel.style.display = "block";
    escPanel.classList.add("active");
    msgBar.style.display = "none";

    const label = document.getElementById("escalation-label");
    const isPermission = currentDecision.type === "permission_request";
    label.textContent = isPermission ? "Permission Request" : "Archie Asks";

    document.getElementById("escalation-question").textContent = currentDecision.question;

    const btnContainer = document.getElementById("escalation-buttons");
    btnContainer.innerHTML = "";
    const options = currentDecision.options || [];
    options.forEach((opt, i) => {
      const btn = document.createElement("button");
      btn.className = "escalation-btn " + (i === 0 ? "primary" : "secondary");
      btn.textContent = opt;
      btn.onclick = () => sendEscalationAnswer(opt);
      btnContainer.appendChild(btn);
    });

    document.getElementById("escalation-input").value = "";
    document.getElementById("escalation-input").focus();
  } else {
    escPanel.style.display = "none";
    escPanel.classList.remove("active");
    msgBar.style.display = "block";
  }
}

// Actions
async function sendEscalationAnswer(answer) {
  if (!currentDecision) return;
  const decisionId = currentDecision.id;
  try {
    const resp = await fetch(`/api/escalation/${decisionId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer })
    });
    if (resp.ok) {
      currentDecision = null;
      renderEscalation();
      showToast("Answer sent", "success");
    } else {
      showToast("Failed to send answer", "error");
    }
  } catch (err) {
    showToast("Network error", "error");
  }
}

function sendEscalationInput() {
  const input = document.getElementById("escalation-input");
  const val = input.value.trim();
  if (val) sendEscalationAnswer(val);
}

async function sendMessage() {
  const input = document.getElementById("message-input");
  const val = input.value.trim();
  if (!val) return;
  try {
    const resp = await fetch("/api/dashboard/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: val })
    });
    if (resp.ok) {
      input.value = "";
      showToast("Message sent", "success");
    }
  } catch (err) {
    showToast("Failed to send message", "error");
  }
}

// Modals
function showModal(type) {
  const overlay = document.getElementById("modal-overlay");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");

  if (type === "help") {
    title.textContent = "Keyboard Shortcuts";
    body.innerHTML = `<div class="help-grid">
      <span class="help-key">?</span><span class="help-desc">Show this help</span>
      <span class="help-key">c</span><span class="help-desc">Toggle costs panel</span>
      <span class="help-key">m</span><span class="help-desc">View all messages</span>
      <span class="help-key">e</span><span class="help-desc">View MCP event log</span>
      <span class="help-key">Esc</span><span class="help-desc">Close modal</span>
    </div>`;
    overlay.classList.add("visible");
  } else if (type === "messages") {
    title.textContent = "Message Log";
    loadMessages().then(messages => {
      body.innerHTML = messages.map(m => {
        const time = new Date(m.timestamp);
        const ts = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
        return `<div class="modal-entry"><span class="time">${ts}</span> <span class="sender">${escapeHtml(m.from)}</span> <span class="arrow">&#8594;</span> <span class="recipient">${escapeHtml(m.to)}</span> <span class="content">${escapeHtml(m.content)}</span></div>`;
      }).join("") || "<p style=\\"color:var(--text-dim)\\">No messages yet.</p>";
    });
    overlay.classList.add("visible");
  } else if (type === "events") {
    title.textContent = "MCP Event Log";
    loadEvents().then(events => {
      body.innerHTML = events.map(ev => {
        const time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "??:??";
        const statusClass = ev.result?.status === "error" ? "status-error" : "status-ok";
        const dur = ev.duration_ms != null ? `${ev.duration_ms.toFixed(0)}ms` : "";
        return `<div class="modal-entry"><span class="time">${time}</span> <span class="sender">${escapeHtml(ev.agent_id || "?")}</span> <span class="tool">${escapeHtml(ev.tool || "?")}</span> <span class="${statusClass}">${escapeHtml(ev.result?.status || "?")}</span> <span class="duration">${dur}</span></div>`;
      }).join("") || "<p style=\\"color:var(--text-dim)\\">No events yet.</p>";
    });
    overlay.classList.add("visible");
  }
}

function showAgentMessages(agentId) {
  const overlay = document.getElementById("modal-overlay");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");

  title.textContent = `Messages: ${agentId}`;
  loadMessages().then(messages => {
    const filtered = messages.filter(m => m.from === agentId || m.to === agentId);
    body.innerHTML = filtered.map(m => {
      const time = new Date(m.timestamp);
      const ts = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      return `<div class="modal-entry"><span class="time">${ts}</span> <span class="sender">${escapeHtml(m.from)}</span> <span class="arrow">&#8594;</span> <span class="recipient">${escapeHtml(m.to)}</span> <span class="content">${escapeHtml(m.content)}</span></div>`;
    }).join("") || `<p style="color:var(--text-dim)">No messages for ${escapeHtml(agentId)}.</p>`;
  });
  overlay.classList.add("visible");
}

function closeModal() {
  document.getElementById("modal-overlay").classList.remove("visible");
}

async function loadMessages() {
  try {
    const resp = await fetch("/api/dashboard/messages");
    return await resp.json();
  } catch { return []; }
}

async function loadEvents() {
  try {
    const resp = await fetch("/api/dashboard/events-log");
    return await resp.json();
  } catch { return []; }
}

// Utilities
function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function showToast(msg, type) {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.className = "toast visible " + type;
  setTimeout(() => { toast.classList.remove("visible"); }, 2000);
}

function updateConnectionStatus() {
  const dot = document.getElementById("conn-dot");
  const text = document.getElementById("conn-text");
  if (connected) {
    dot.className = "connection-dot connected";
    text.textContent = "Connected";
  } else {
    dot.className = "connection-dot disconnected";
    text.textContent = "Disconnected";
  }
}

function updateRuntime() {
  if (!startTime) return;
  const elapsed = Math.floor((Date.now() - startTime.getTime()) / 1000);
  const h = String(Math.floor(elapsed / 3600)).padStart(2, "0");
  const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");
  const s = String(elapsed % 60).padStart(2, "0");
  document.getElementById("runtime").textContent = `${h}:${m}:${s}`;
}

function toggleCosts() {
  showCosts = !showCosts;
  const panel = document.getElementById("costs-panel");
  const grid = document.getElementById("main-grid");
  if (showCosts) {
    panel.style.display = "flex";
    grid.classList.add("show-costs");
    renderCosts();
  } else {
    panel.style.display = "none";
    grid.classList.remove("show-costs");
  }
}

// Track scroll position for auto-scroll
document.addEventListener("DOMContentLoaded", () => {
  const log = document.getElementById("activity-log");
  log.addEventListener("scroll", () => {
    const threshold = 50;
    autoScroll = (log.scrollHeight - log.scrollTop - log.clientHeight) < threshold;
  });
});

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  // Don\'t capture when typing in inputs
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
    if (e.key === "Enter") {
      if (e.target.id === "message-input") sendMessage();
      else if (e.target.id === "escalation-input") sendEscalationInput();
    }
    return;
  }

  if (e.key === "?" || e.key === "/") { e.preventDefault(); showModal("help"); }
  else if (e.key === "c") { e.preventDefault(); toggleCosts(); }
  else if (e.key === "m") { e.preventDefault(); showModal("messages"); }
  else if (e.key === "e") { e.preventDefault(); showModal("events"); }
  else if (e.key === "Escape") { closeModal(); }
});

// Initialize
connectSSE();
setInterval(updateRuntime, 1000);

// Fallback: reload full state every 30s in case SSE missed events
setInterval(loadFullState, 30000);
</script>

</body>
</html>'''
