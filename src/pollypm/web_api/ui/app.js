// PollyPM v0 web UI — vanilla JS, no build step, no dependencies.
//
// Auth model: the cookie ``pollypm-session`` is set by GET /ui/ from
// the on-disk token, so every request just needs ``credentials:
// 'include'`` to ride that cookie. We never read or display the token
// in the browser.
//
// Cookie issuance is gated on the server side (see app.py _ui_index)
// to one of: loopback client, Tailscale CGNAT peer, or valid bearer
// header. LAN devices get the HTML but no cookie; the first /api/
// call returns 401 and ``setAuthGate`` below surfaces a banner that
// tells the operator how to recover.

(function () {
  "use strict";

  const API = "/api/v1";
  const POLL_DASHBOARD_MS = 15000;
  const POLL_MESSAGES_MS = 5000;
  const MAX_MESSAGES = 50;
  const AUDIT_LIMIT = 25;

  const state = {
    surfaces: [],
    selectedSurface: null,
    messageTimer: null,
    auditExpanded: {},
    auditEntries: {},
    auditErrors: {},
    auditLoading: {},
    surfaceMidStream: {},
    surfaceFilter: "",
  };

  // ----- DOM helpers ------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else node.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      for (const child of children) {
        if (child) node.appendChild(child);
      }
    }
    return node;
  }

  function setStatus(level, label) {
    const node = $("conn-status");
    if (!node) return;
    node.className = "conn-status conn-" + level;
    const lbl = node.querySelector(".conn-label");
    if (lbl) lbl.textContent = label;
  }

  // Show the auth-gate banner once when the SPA hits a 401. This
  // happens when /ui/ was reached from a host that isn't loopback /
  // Tailscale and didn't supply a valid bearer header, so the server
  // declined to set the pollypm-session cookie. Surfacing the recovery
  // path inline (rather than leaving the rails frozen on "loading…")
  // makes the constraint visible at the moment it bites.
  let authGateShown = false;
  function showAuthGate() {
    if (authGateShown) return;
    authGateShown = true;
    const banner = document.createElement("div");
    banner.className = "error-banner auth-gate-banner";
    banner.textContent =
      "This UI requires Tailscale or local (loopback) access. " +
      "Visit http://127.0.0.1:<port>/ui/ from the host running " +
      "`pm serve`, or open http://<tailscale-ip>:<port>/ui/ from " +
      "a tailnet device. To bootstrap manually, run " +
      "`pm api regen-token` and send the value in an " +
      "`Authorization: Bearer …` header.";
    const layout = document.getElementById("layout") || document.body;
    layout.parentNode.insertBefore(banner, layout);
  }

  function showToast(level, text) {
    const toast = document.createElement("div");
    toast.className = "toast toast-" + level;
    toast.textContent = text;
    document.body.appendChild(toast);
    setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 4000);
  }

  // ----- fetch wrapper ----------------------------------------------------

  async function apiFetch(path, opts) {
    const init = Object.assign({ credentials: "include" }, opts || {});
    init.headers = Object.assign(
      { "Accept": "application/json" },
      init.headers || {},
    );
    let resp;
    try {
      resp = await fetch(path, init);
    } catch (err) {
      setStatus("error", "offline");
      throw err;
    }
    if (resp.status === 401) {
      setStatus("error", "auth required");
      showAuthGate();
      throw new Error("unauthorized");
    }
    if (!resp.ok) {
      setStatus("warn", "HTTP " + resp.status);
      let body = null;
      try { body = await resp.json(); } catch (e) { /* ignore */ }
      const detail = body && body.error
        ? body.error.message || body.error.code
        : ("HTTP " + resp.status);
      throw new Error(detail);
    }
    setStatus("ok", "online");
    return resp;
  }

  async function apiJson(path, opts) {
    const resp = await apiFetch(path, opts);
    return resp.json();
  }

  // ----- surfaces (left rail) --------------------------------------------

  async function loadSurfaces() {
    try {
      const data = await apiJson(API + "/chat/sessions");
      state.surfaces = Array.isArray(data.sessions) ? data.sessions : [];
      renderSurfaces();
    } catch (err) {
      renderSurfaceError(err);
    }
  }

  function renderSurfaces() {
    const list = $("surface-list");
    list.innerHTML = "";
    const filter = state.surfaceFilter.trim().toLowerCase();
    const surfaces = filter
      ? state.surfaces.filter((s) => (
        String(s.session_name || "").toLowerCase().includes(filter)
      ))
      : state.surfaces;
    if (state.surfaces.length === 0) {
      list.appendChild(
        el("li", { class: "surface-empty", text: "no surfaces registered" }),
      );
      return;
    }
    if (surfaces.length === 0) {
      list.appendChild(
        el("li", { class: "surface-empty", text: "no matching surfaces" }),
      );
      return;
    }
    for (const s of surfaces) {
      const dotClass =
        s.window && s.window.pane_dead
          ? "surface-dot dead"
          : s.window && s.window.present
            ? "surface-dot present"
            : "surface-dot";
      const labelParts = [];
      if (s.surface_type) labelParts.push(s.surface_type);
      if (s.persona && s.persona !== s.surface_type) labelParts.push(s.persona);
      if (s.project) labelParts.push(s.project);
      const li = el(
        "li",
        {
          "data-session": s.session_name,
          "class": state.selectedSurface === s.session_name ? "active" : "",
        },
        [
          el("span", { class: "surface-name" }, [
            el("span", { class: dotClass }),
            document.createTextNode(s.session_name),
          ]),
          el("span", {
            class: "surface-meta",
            text: labelParts.join(" · ") || "—",
          }),
        ],
      );
      li.addEventListener("click", () => selectSurface(s.session_name));
      list.appendChild(li);
    }
  }

  function renderSurfaceError(err) {
    const list = $("surface-list");
    list.innerHTML = "";
    list.appendChild(
      el("li", { class: "surface-empty", text: "error: " + err.message }),
    );
  }

  function selectSurface(name) {
    state.selectedSurface = name;
    $("pane-title").textContent = name;
    $("pane-meta").textContent = "";
    $("send-input").disabled = false;
    $("send-button").disabled = false;
    updateStopAgentButton();
    renderSurfaces();
    loadHistory(name);
    if (state.auditExpanded[name]) loadAuditForSurface(name);
    schedulePoll();
  }

  function surfaceByName(name) {
    return state.surfaces.find((s) => s.session_name === name) || null;
  }

  function auditPatternForSurface(surface, name) {
    if (
      surface
      && surface.surface_type === "worker"
      && surface.project
      && surface.task_id != null
    ) {
      return surface.project + "/" + surface.task_id;
    }
    return name;
  }

  function auditPathForSurface(name) {
    const surface = surfaceByName(name);
    const params = new URLSearchParams();
    params.set("limit", String(AUDIT_LIMIT));
    params.set("since", "7d");
    params.set("pattern", auditPatternForSurface(surface, name));
    if (surface && surface.project) params.set("project", surface.project);
    return API + "/audit/grep?" + params.toString();
  }

  function auditSummary(entry) {
    const parts = [entry.event || "audit"];
    if (entry.subject) parts.push(entry.subject);
    if (entry.status) parts.push(entry.status);
    if (entry.actor) parts.push("by " + entry.actor);
    return parts.join(" · ");
  }

  function removeExistingAuditPanel(list) {
    const existing = list.querySelector(".audit-panel");
    if (existing) existing.remove();
  }

  function renderAuditPanel(name) {
    if (!name) return;
    const list = $("message-list");
    removeExistingAuditPanel(list);
    const expanded = Boolean(state.auditExpanded[name]);
    const entries = state.auditEntries[name] || [];
    const loading = Boolean(state.auditLoading[name]);
    const error = state.auditErrors[name];
    const children = [];
    const toggle = el("button", {
      class: "audit-toggle",
      type: "button",
      "aria-expanded": expanded ? "true" : "false",
      text: expanded ? "Hide audit log" : "Show audit log",
    });
    toggle.addEventListener("click", () => {
      state.auditExpanded[name] = !state.auditExpanded[name];
      renderAuditPanel(name);
      if (state.auditExpanded[name]) loadAuditForSurface(name);
    });
    const headerChildren = [
      el("div", { class: "audit-title", text: "Audit log" }),
      toggle,
    ];
    if (expanded) {
      const refresh = el("button", {
        class: "audit-refresh",
        type: "button",
        text: "Refresh",
      });
      refresh.addEventListener("click", () => loadAuditForSurface(name));
      headerChildren.push(refresh);
    }
    children.push(el("div", { class: "audit-header" }, headerChildren));
    if (expanded) {
      const bodyChildren = [];
      if (loading) {
        bodyChildren.push(el("div", {
          class: "audit-empty",
          text: "loading audit entries...",
        }));
      } else if (error) {
        bodyChildren.push(el("div", {
          class: "audit-empty",
          text: "audit error: " + error.message,
        }));
      } else if (entries.length === 0) {
        bodyChildren.push(el("div", {
          class: "audit-empty",
          text: "no audit entries",
        }));
      } else {
        for (const entry of entries.slice(0, AUDIT_LIMIT)) {
          bodyChildren.push(el("div", { class: "audit-entry" }, [
            el("span", { class: "audit-ts", text: entry.ts || "" }),
            el("span", { class: "audit-line", text: auditSummary(entry) }),
          ]));
        }
      }
      children.push(el("div", { class: "audit-body" }, bodyChildren));
    }
    list.appendChild(el("div", { class: "audit-panel" }, children));
  }

  async function loadAuditForSurface(name) {
    if (!name) return;
    state.auditLoading[name] = true;
    state.auditErrors[name] = null;
    renderAuditPanel(name);
    try {
      const data = await apiJson(auditPathForSurface(name));
      state.auditEntries[name] = Array.isArray(data.events) ? data.events : [];
    } catch (err) {
      state.auditEntries[name] = [];
      state.auditErrors[name] = err;
    } finally {
      state.auditLoading[name] = false;
      if (state.selectedSurface === name && state.auditExpanded[name]) {
        renderAuditPanel(name);
      }
    }
  }

  function messageLooksMidStream(message) {
    const text = String(message && message.text ? message.text : "")
      .toLowerCase();
    return (
      text.includes("esc to interrupt")
      || text.includes("working (")
      || text.includes("unsafe_mid_tool")
    );
  }

  function updateStopAgentButton() {
    const btn = $("stop-agent-button");
    if (!btn) return;
    const active = Boolean(
      state.selectedSurface && state.surfaceMidStream[state.selectedSurface],
    );
    btn.hidden = !active;
    btn.disabled = !active;
  }

  // ----- history (center) ------------------------------------------------

  async function loadHistory(name) {
    if (!name) return;
    try {
      const path =
        API + "/chat/" + encodeURIComponent(name) + "/messages?limit="
        + MAX_MESSAGES + "&direction=desc";
      const data = await apiJson(path);
      renderHistory(data);
    } catch (err) {
      renderHistoryError(err);
    }
  }

  function renderHistory(data) {
    const list = $("message-list");
    list.innerHTML = "";
    const meta = [];
    if (data.surface_type) meta.push(data.surface_type);
    if (data.transcript_source) meta.push("src=" + data.transcript_source);
    $("pane-meta").textContent = meta.join(" · ");
    const msgs = Array.isArray(data.messages) ? data.messages.slice() : [];
    state.surfaceMidStream[data.session_name] = (
      msgs.length > 0 && messageLooksMidStream(msgs[0])
    );
    updateStopAgentButton();
    if (msgs.length === 0) {
      list.appendChild(
        el("div", { class: "message-empty", text: "no messages yet" }),
      );
      renderAuditPanel(data.session_name);
      return;
    }
    // API returned newest-first; render oldest-first so the latest is
    // at the bottom (chat convention).
    msgs.reverse();
    for (const m of msgs) {
      const roleClass = "message message-role-" + (m.role || "system");
      list.appendChild(
        el("div", { class: roleClass }, [
          el("div", { class: "message-head" }, [
            el("span", { class: "message-actor", text: m.actor || m.role || "?" }),
            el("span", { class: "message-ts", text: m.ts || "" }),
            el("span", { class: "message-type", text: m.type || "" }),
          ]),
          el("div", { class: "message-text", text: m.text || "" }),
        ]),
      );
    }
    list.scrollTop = list.scrollHeight;
    renderAuditPanel(data.session_name);
  }

  function renderHistoryError(err) {
    const list = $("message-list");
    list.innerHTML = "";
    list.appendChild(
      el("div", { class: "error-banner", text: "history error: " + err.message }),
    );
    renderAuditPanel(state.selectedSurface);
    updateStopAgentButton();
  }

  // ----- send (bottom) ---------------------------------------------------

  async function sendMessage(name, text) {
    if (!name || !text) return;
    const path = API + "/chat/" + encodeURIComponent(name) + "/send";
    await apiFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    });
    // Refresh after a short delay so the new line shows up.
    setTimeout(() => loadHistory(name), 400);
  }

  async function interruptSurface(name) {
    if (!name) return;
    const path = API + "/sessions/" + encodeURIComponent(name) + "/interrupt";
    await apiFetch(path, { method: "POST" });
    state.surfaceMidStream[name] = false;
    updateStopAgentButton();
    showToast("ok", "sent interrupt to " + name);
  }

  // ----- dashboard rollups (right rail) ----------------------------------

  async function pollDashboard() {
    try {
      const data = await apiJson(API + "/dashboard");
      renderDashboard(data);
    } catch (err) {
      renderDashboardError(err);
    }
  }

  // Map a server-side ``scoped_fields`` entry (e.g. ``rollups.open_inbox_count``)
  // to a small ``(filtered)`` tag on the corresponding card. ``scoped_fields``
  // is set by the dashboard route whenever ``?project=`` narrows the
  // response so the client can distinguish a workspace counter from a
  // project-scoped one without having to know the gather pipeline's rules.
  // We currently never poll with ``?project=`` from the v0 UI, but reading
  // the field keeps the rail honest if the URL is ever crafted by hand
  // and makes the contract self-documenting in the DOM.
  function isScoped(scopedFields, key) {
    if (!Array.isArray(scopedFields)) return false;
    return scopedFields.indexOf(key) !== -1;
  }

  function buildCard(label, value, cls, scoped) {
    const labelText = scoped ? label + " (filtered)" : label;
    return el("div", { class: "rollup-card " + (cls || "") }, [
      el("div", { class: "rollup-label", text: labelText }),
      el("div", { class: "rollup-value", text: String(value) }),
    ]);
  }

  function renderDashboard(data) {
    const box = $("dashboard-rollups");
    box.innerHTML = "";
    if (!data || typeof data !== "object") {
      box.appendChild(el("div", { class: "rollup-empty", text: "no data" }));
      return;
    }
    // The dashboard envelope matches ``DashboardResponse`` in
    // ``src/pollypm/web_api/routes/dashboard.py`` — counters live under
    // ``rollups``, list fields (``active_sessions``, ``recent_messages``,
    // ``projects``) sit at the top level, and ``daemon_status`` is a
    // string ("up" | "down"). ``scoped_fields`` enumerates which
    // sub-fields got narrowed by a ``?project=`` filter so the UI can
    // mark them as filtered rather than mis-presenting them as global.
    const rollups = (data && typeof data.rollups === "object" && data.rollups)
      || {};
    const scopedFields = Array.isArray(data.scoped_fields)
      ? data.scoped_fields : [];
    const cards = [];

    // --- counters from rollups ------------------------------------------
    if (typeof rollups.open_inbox_count === "number") {
      cards.push(buildCard(
        "inbox",
        rollups.open_inbox_count + " items",
        "rollup-attention",
        isScoped(scopedFields, "rollups.open_inbox_count"),
      ));
    }
    if (typeof rollups.pending_plan_reviews === "number") {
      cards.push(buildCard(
        "plan reviews",
        rollups.pending_plan_reviews + " waiting",
        "rollup-attention",
        isScoped(scopedFields, "rollups.pending_plan_reviews"),
      ));
    }
    if (typeof rollups.alert_count === "number") {
      cards.push(buildCard(
        "alerts",
        rollups.alert_count,
        rollups.alert_count > 0 ? "rollup-blocked" : "",
        // alert_count is intentionally global per DashboardRollups
        // docstring — never appears in scoped_fields, so no tag.
        false,
      ));
    }

    // --- activity (24h) -------------------------------------------------
    if (
      typeof rollups.sweep_count_24h === "number"
      || typeof rollups.message_count_24h === "number"
    ) {
      const sweeps = typeof rollups.sweep_count_24h === "number"
        ? rollups.sweep_count_24h : 0;
      const msgs = typeof rollups.message_count_24h === "number"
        ? rollups.message_count_24h : 0;
      cards.push(buildCard(
        "activity (24h)",
        sweeps + " sweeps / " + msgs + " msgs",
        "rollup-working",
        false,
      ));
    }

    // --- daemon health --------------------------------------------------
    if (typeof data.daemon_status === "string") {
      const up = data.daemon_status === "up";
      cards.push(buildCard(
        "daemon",
        data.daemon_status,
        up ? "rollup-working" : "rollup-blocked",
        false,
      ));
    }

    // --- active sessions (list length) ----------------------------------
    if (Array.isArray(data.active_sessions)) {
      cards.push(buildCard(
        "active sessions",
        data.active_sessions.length,
        "",
        isScoped(scopedFields, "active_sessions"),
      ));
    }

    // --- tracked projects ----------------------------------------------
    if (typeof rollups.tracked_count === "number") {
      cards.push(buildCard(
        "projects tracked",
        rollups.tracked_count,
        "",
        isScoped(scopedFields, "rollups.tracked_count"),
      ));
    }

    if (cards.length === 0) {
      box.appendChild(el("div", { class: "rollup-empty", text: "no rollups" }));
      return;
    }
    for (const c of cards) box.appendChild(c);
  }

  function renderDashboardError(err) {
    const box = $("dashboard-rollups");
    box.innerHTML = "";
    box.appendChild(
      el("div", { class: "rollup-empty", text: "error: " + err.message }),
    );
  }

  // ----- timers -----------------------------------------------------------

  function schedulePoll() {
    if (state.messageTimer) clearInterval(state.messageTimer);
    state.messageTimer = setInterval(() => {
      if (state.selectedSurface) loadHistory(state.selectedSurface);
    }, POLL_MESSAGES_MS);
  }

  // ----- wire-up ----------------------------------------------------------

  function wireSendForm() {
    const form = $("send-form");
    const input = $("send-input");
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const text = input.value.trim();
      if (!text || !state.selectedSurface) return;
      input.value = "";
      sendMessage(state.selectedSurface, text).catch((err) => {
        renderHistoryError(err);
      });
    });
  }

  function wireStopAgentButton() {
    const btn = $("stop-agent-button");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const name = state.selectedSurface;
      if (!name || btn.disabled) return;
      btn.disabled = true;
      interruptSurface(name).catch((err) => {
        btn.disabled = false;
        showToast("error", "interrupt failed: " + err.message);
      });
    });
  }

  function wireSurfaceFilter() {
    const input = $("surface-filter");
    if (!input) return;
    input.addEventListener("input", () => {
      state.surfaceFilter = input.value || "";
      renderSurfaces();
    });
  }

  function init() {
    wireSurfaceFilter();
    wireSendForm();
    wireStopAgentButton();
    setStatus("warn", "connecting…");
    loadSurfaces();
    pollDashboard();
    setInterval(loadSurfaces, 30000);
    setInterval(pollDashboard, POLL_DASHBOARD_MS);
  }

  // Expose for tests / debugging. ``renderDashboard`` is exported so
  // an executable test harness can feed a representative
  // ``DashboardResponse`` payload through the real mapping logic and
  // assert visible labels/values — static source greps cannot catch a
  // runtime mapping bug that still happens to contain the right field
  // names (Codex round-5 blocker).
  window.PollyPM = {
    loadSurfaces: loadSurfaces,
    loadHistory: loadHistory,
    sendMessage: sendMessage,
    interruptSurface: interruptSurface,
    pollDashboard: pollDashboard,
    renderAuditPanel: renderAuditPanel,
    renderDashboard: renderDashboard,
    state: state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
