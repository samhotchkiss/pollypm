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

  const state = {
    surfaces: [],
    selectedSurface: null,
    messageTimer: null,
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
    if (state.surfaces.length === 0) {
      list.appendChild(
        el("li", { class: "surface-empty", text: "no surfaces registered" }),
      );
      return;
    }
    for (const s of state.surfaces) {
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
    renderSurfaces();
    loadHistory(name);
    schedulePoll();
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
    if (msgs.length === 0) {
      list.appendChild(
        el("div", { class: "message-empty", text: "no messages yet" }),
      );
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
  }

  function renderHistoryError(err) {
    const list = $("message-list");
    list.innerHTML = "";
    list.appendChild(
      el("div", { class: "error-banner", text: "history error: " + err.message }),
    );
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

  function init() {
    wireSendForm();
    setStatus("warn", "connecting…");
    loadSurfaces();
    pollDashboard();
    setInterval(loadSurfaces, 30000);
    setInterval(pollDashboard, POLL_DASHBOARD_MS);
  }

  // Expose for tests / debugging.
  window.PollyPM = {
    loadSurfaces: loadSurfaces,
    loadHistory: loadHistory,
    sendMessage: sendMessage,
    pollDashboard: pollDashboard,
    state: state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
