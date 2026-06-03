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
  const FALLBACK_POLL_MS = 15000;
  const SSE_RETRY_MS = 30000;
  const SSE_FAILURE_LIMIT = 3;
  const SSE_STABLE_OPEN_MS = 5000;
  const PUSH_REFRESH_DEBOUNCE_MS = 150;
  const FETCH_RETRY_MS = 3000;
  const MAX_MESSAGES = 50;
  const HISTORY_RENDER_LIMIT = MAX_MESSAGES;
  const SESSION_ISSUED_COOKIE = "pollypm-session-issued-at";
  const SESSION_EXPIRY_WARNING_MS = 6 * 24 * 60 * 60 * 1000;
  const TASK_RAIL_LIMIT = 200;
  const TASK_RENDER_LIMIT = 80;
  const TASK_STATUS_GROUPS = {
    attention: ["review", "queued", "rework", "blocked", "on_hold"],
    active: ["in_progress", "rework"],
    blocked: ["blocked", "on_hold"],
    review: ["review"],
    done: ["done"],
    cancelled: ["cancelled"],
  };
  const AUDIT_LIMIT = 25;
  const ACTIVITY_LIMIT = 40;
  const ACTIVITY_DEFAULT_SINCE = "2d";
  const INBOX_PAGE_LIMIT = 25;
  const OPERATOR_ACTOR = "operator";
  const CLAIM_ACTOR = "worker";
  const railRequestTimeoutOverride = Number(
    window.__POLLYPM_RAIL_REQUEST_TIMEOUT_MS,
  );
  const RAIL_REQUEST_TIMEOUT_MS = (
    Number.isFinite(railRequestTimeoutOverride)
    && railRequestTimeoutOverride > 0
  ) ? railRequestTimeoutOverride : 10000;
  const ACTIVITY_REQUEST_TIMEOUT_MS = 3000;
  const ACTIVITY_STATS_REQUEST_TIMEOUT_MS = 5000;
  const ACTIVITY_STATS_DEADLINE_SECONDS = 2.5;

  const state = {
    projects: [],
    projectLoadError: null,
    projectLoading: false,
    selectedProject: null,
    projectFilter: "",
    projectSort: "urgency",
    showTestProjects: false,
    surfaces: [],
    taskSurfaces: [],
    surfaceLoadError: null,
    taskLoadError: null,
    surfaceLoading: false,
    taskLoading: false,
    taskStatusFilter: "",
    selectedKind: null,
    selectedSurface: null,
    selectedTaskKey: null,
    sessionExpiryDismissed: false,
    eventSource: null,
    fallbackTimer: null,
    pushRefreshTimer: null,
    sseStableOpenTimer: null,
    surfacesInFlight: false,
    surfacesRefreshQueued: false,
    surfacesQueuedPreserveErrors: false,
    dashboardData: null,
    dashboardBriefingRequested: false,
    dashboardInFlight: false,
    dashboardRefreshQueued: false,
    historyInFlight: {},
    historyRefreshQueued: {},
    historyRenderSignatures: {},
    fetchStates: {},
    sseFailures: 0,
    sseRetryTimer: null,
    lastEventId: null,
    auditExpanded: {},
    auditEntries: {},
    auditErrors: {},
    auditLoading: {},
    surfaceMidStream: {},
    pendingAskUser: {},
    surfaceFilter: "",
    activitySince: ACTIVITY_DEFAULT_SINCE,
    activityEntries: [],
    activityStats: null,
    activityStatsNote: null,
    activityLoading: false,
    activityError: null,
    activityInFlight: false,
    activityRefreshQueued: false,
    alerts: {
      items: [],
      loading: false,
      error: null,
      reloadQueued: false,
      doctorReport: null,
      doctorLoading: false,
      doctorError: null,
      doctorRunInFlight: false,
      actionInFlight: {},
    },
    pendingMessages: {},
    inbox: {
      items: [],
      nextCursor: null,
      loading: false,
      loadingMore: false,
      error: null,
      warning: null,
      reloadQueued: false,
      filters: {
        project: "",
        state: "",
        type: "",
      },
      selectedId: null,
      detail: null,
      detailLoading: false,
      detailError: null,
      actionInFlight: {},
      replyDraft: "",
    },
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

  function clearMessageList(list) {
    list.innerHTML = "";
    delete list.dataset.historySession;
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

  function cookieValue(name) {
    const prefix = name + "=";
    const parts = document.cookie ? document.cookie.split(";") : [];
    for (const rawPart of parts) {
      const part = rawPart.trim();
      if (part.indexOf(prefix) === 0) {
        return decodeURIComponent(part.slice(prefix.length));
      }
    }
    return "";
  }

  function maybeShowSessionExpiryBanner() {
    if (state.sessionExpiryDismissed || $("session-expiry-banner")) return;
    const issuedRaw = cookieValue(SESSION_ISSUED_COOKIE);
    const issuedSeconds = Number.parseInt(issuedRaw, 10);
    if (!Number.isFinite(issuedSeconds)) return;
    const ageMs = Date.now() - issuedSeconds * 1000;
    if (ageMs < SESSION_EXPIRY_WARNING_MS) return;

    const banner = el("div", {
      id: "session-expiry-banner",
      class: "session-expiry-banner",
    }, [
      el("span", {
        text: "Session expires soon; refresh page to renew.",
      }),
      el("button", {
        type: "button",
        class: "session-expiry-dismiss",
        text: "Dismiss",
        "aria-label": "Dismiss session expiry warning",
      }),
    ]);
    const dismiss = banner.querySelector("button");
    if (dismiss) {
      dismiss.addEventListener("click", () => {
        state.sessionExpiryDismissed = true;
        banner.remove();
      });
    }
    const layout = $("layout") || document.body;
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

  function isAbortError(err) {
    return err && (
      err.name === "AbortError"
      || err.message === "aborted"
      || err.code === 20
    );
  }

  function ensureFetchState(name) {
    if (!state.fetchStates[name]) {
      state.fetchStates[name] = {
        name: name,
        seq: 0,
        stage: "idle",
        controller: null,
        timers: [],
        retry: null,
        label: name,
        hasSettled: false,
      };
    }
    return state.fetchStates[name];
  }

  function clearFetchTimers(fetchState) {
    for (const timer of fetchState.timers) clearTimeout(timer);
    fetchState.timers = [];
  }

  function beginFetchState(name, label, render, retry, opts) {
    const options = opts || {};
    const fetchState = ensureFetchState(name);
    fetchState.seq += 1;
    fetchState.stage = "loading";
    fetchState.label = label;
    fetchState.retry = retry;
    fetchState.controller = new AbortController();
    clearFetchTimers(fetchState);
    const seq = fetchState.seq;
    fetchState.timers.push(setTimeout(() => {
      if (fetchState.seq !== seq) return;
      fetchState.stage = "slow";
      render();
    }, FETCH_RETRY_MS));
    if (!(options.preserveSettled && fetchState.hasSettled)) {
      render();
    }
    return {
      seq: seq,
      signal: fetchState.controller.signal,
    };
  }

  function finishFetchState(name, seq) {
    const fetchState = ensureFetchState(name);
    if (fetchState.seq !== seq) return false;
    clearFetchTimers(fetchState);
    fetchState.stage = "idle";
    fetchState.controller = null;
    fetchState.hasSettled = true;
    return true;
  }

  function abortFetchState(name) {
    const fetchState = ensureFetchState(name);
    if (fetchState.controller) fetchState.controller.abort();
    clearFetchTimers(fetchState);
    fetchState.seq += 1;
    fetchState.stage = "idle";
    fetchState.controller = null;
  }

  function retryFetchState(name) {
    const fetchState = ensureFetchState(name);
    const retry = fetchState.retry;
    abortFetchState(name);
    if (retry) retry();
  }

  function loadingText(fetchState, fallback) {
    if (!fetchState || fetchState.stage === "loading") return fallback;
    return fetchState.label + " is taking longer than expected.";
  }

  function fetchAffordance(fetchName, tag, className, fallback) {
    const fetchState = ensureFetchState(fetchName);
    const children = [
      document.createTextNode(loadingText(fetchState, fallback)),
    ];
    if (fetchState.stage === "slow") {
      const retry = el("button", {
        type: "button",
        class: "fetch-retry",
        text: "Retry",
        "aria-label": "Retry loading " + fetchState.label,
      });
      retry.addEventListener("click", () => retryFetchState(fetchName));
      children.push(retry);
    }
    return el(tag, { class: className + " fetch-state" }, children);
  }

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
      if (isAbortError(err)) throw err;
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
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }
    setStatus("ok", "online");
    return resp;
  }

  async function apiJson(path, opts) {
    const resp = await apiFetch(path, opts);
    return resp.json();
  }

  async function apiJsonOptional(path, opts) {
    const init = Object.assign({ credentials: "include" }, opts || {});
    init.headers = Object.assign(
      { "Accept": "application/json" },
      init.headers || {},
    );
    const resp = await fetch(path, init);
    if (resp.status === 401) {
      setStatus("error", "auth required");
      showAuthGate();
      throw new Error("unauthorized");
    }
    if (!resp.ok) {
      let body = null;
      try { body = await resp.json(); } catch (e) { /* ignore */ }
      const detail = body && body.error
        ? body.error.message || body.error.code
        : ("HTTP " + resp.status);
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  }

  // ----- surfaces (left rail) --------------------------------------------

  function projectListPath() {
    const params = new URLSearchParams();
    if (!state.showTestProjects) {
      params.set("operator", "true");
    }
    if (state.projectFilter.trim()) {
      params.set("q", state.projectFilter.trim());
    }
    if (state.projectSort) {
      params.set("sort", state.projectSort);
    }
    const query = params.toString();
    return API + "/projects" + (query ? "?" + query : "");
  }

  function asError(reason) {
    return reason instanceof Error ? reason : new Error(String(reason));
  }

  function timeoutLabel(timeoutMs) {
    return timeoutMs >= 1000
      ? Math.round(timeoutMs / 100) / 10 + "s"
      : timeoutMs + "ms";
  }

  function requestTimeoutError(label, timeoutMs) {
    return new Error(
      label + " request timed out after " + timeoutLabel(timeoutMs),
    );
  }

  function railTimeoutError(label) {
    return requestTimeoutError(label, RAIL_REQUEST_TIMEOUT_MS);
  }

  function railJsonWithTimeout(label, request, opts) {
    const parentSignal = opts && opts.signal;
    const controller = new AbortController();
    const requestOpts = Object.assign({}, opts || {}, {
      signal: controller.signal,
    });
    let timedOut = false;
    let timer = null;
    let onParentAbort = null;
    if (parentSignal) {
      onParentAbort = () => controller.abort();
      if (parentSignal.aborted) controller.abort();
      else parentSignal.addEventListener("abort", onParentAbort, { once: true });
    }
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, RAIL_REQUEST_TIMEOUT_MS);
    return Promise.resolve().then(() => request(requestOpts)).catch((err) => {
      if (timedOut && isAbortError(err)) throw railTimeoutError(label);
      throw err;
    }).finally(() => {
      if (timer !== null) clearTimeout(timer);
      if (parentSignal && onParentAbort) {
        parentSignal.removeEventListener("abort", onParentAbort);
      }
    });
  }

  async function apiJsonOptionalWithTimeout(label, path, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, timeoutMs);
    try {
      return await apiJsonOptional(path, { signal: controller.signal });
    } catch (err) {
      if (isAbortError(err)) {
        throw requestTimeoutError(label, timeoutMs);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadSurfaces(opts) {
    const force = opts && opts.force;
    const preserveErrors = opts && opts.preserveErrors;
    if (state.surfacesInFlight) {
      if (force) {
        abortFetchState("surfaces");
        state.surfacesInFlight = false;
        state.surfacesRefreshQueued = false;
        state.surfacesQueuedPreserveErrors = false;
      } else {
        state.surfacesRefreshQueued = true;
        state.surfacesQueuedPreserveErrors = (
          state.surfacesQueuedPreserveErrors || !!preserveErrors
        );
        return;
      }
    }
    state.surfacesInFlight = true;
    state.surfaceLoading = true;
    state.taskLoading = true;
    state.projectLoading = true;
    if (!preserveErrors) {
      state.surfaceLoadError = null;
      state.taskLoadError = null;
      state.projectLoadError = null;
    }
    const request = beginFetchState(
      "surfaces",
      "surfaces",
      renderSurfaces,
      () => loadSurfaces({ force: true }),
      { preserveSettled: true },
    );
    const ownsRequest = () => (
      ensureFetchState("surfaces").seq === request.seq
    );
    renderProjects();
    renderSurfaces();
    const sessionsPromise = railJsonWithTimeout(
      "chat surfaces",
      (opts) => apiJson(API + "/chat/sessions?include_transcripts=false", opts),
      { signal: request.signal },
    ).then((data) => {
      if (!ownsRequest()) return;
      state.surfaces = Array.isArray(data.sessions) ? data.sessions : [];
      state.surfaceLoadError = null;
    }, (err) => {
      if (!ownsRequest() || isAbortError(err)) return;
      state.surfaces = [];
      state.surfaceLoadError = asError(err);
    }).finally(() => {
      if (!ownsRequest()) return;
      state.surfaceLoading = false;
      ensureSelectionVisible();
      renderSurfaces();
    });

    const tasksPromise = railJsonWithTimeout(
      "tasks",
      (opts) => apiJsonOptional(
        taskListPath(),
        opts,
      ),
      { signal: request.signal },
    ).then((taskData) => {
      if (!ownsRequest()) return;
      state.taskLoadError = null;
      const items = Array.isArray(taskData.items) ? taskData.items : [];
      state.taskSurfaces = dedupeTaskSurfaces(
        items.map(normalizeTaskSurface),
      );
    }, (err) => {
      if (!ownsRequest() || isAbortError(err)) return;
      state.taskSurfaces = [];
      state.taskLoadError = asError(err);
    }).finally(() => {
      if (!ownsRequest()) return;
      state.taskLoading = false;
      ensureSelectionVisible();
      renderSurfaces();
    });

    const projectsPromise = railJsonWithTimeout(
      "projects",
      (opts) => apiJsonOptional(projectListPath(), opts),
      { signal: request.signal },
    ).then((projectData) => {
      if (!ownsRequest()) return;
      state.projectLoadError = null;
      state.projects = Array.isArray(projectData.items)
        ? projectData.items : [];
    }, (err) => {
      if (!ownsRequest() || isAbortError(err)) return;
      state.projects = [];
      state.projectLoadError = asError(err);
    }).finally(() => {
      if (!ownsRequest()) return;
      state.projectLoading = false;
      renderProjects();
    });

    try {
      await Promise.allSettled([
        sessionsPromise,
        tasksPromise,
        projectsPromise,
      ]);
    } finally {
      if (!finishFetchState("surfaces", request.seq)) return;
      state.surfacesInFlight = false;
      if (state.surfacesRefreshQueued) {
        const queuedPreserveErrors = state.surfacesQueuedPreserveErrors;
        state.surfacesRefreshQueued = false;
        state.surfacesQueuedPreserveErrors = false;
        loadSurfaces({ preserveErrors: queuedPreserveErrors });
      }
    }
  }

  function renderSurfacesLoading() {
    const list = $("surface-list");
    if (!list) return;
    list.innerHTML = "";
    list.appendChild(
      fetchAffordance("surfaces", "li", "surface-empty", "loading..."),
    );
  }

  function normalizeTaskSurface(task) {
    const project = task.project || "";
    const number = task.task_number == null ? "" : String(task.task_number);
    const key = project && number ? project + "/" + number
      : task.task_id || task.title || "task";
    return {
      key: key,
      task_id: task.task_id || "",
      project: project,
      task_number: number,
      title: task.title || key,
      work_status: task.work_status || "unknown",
      type: task.type || "task",
      priority: task.priority || "",
      assignee: task.assignee || "",
      updated_at: task.updated_at || "",
      project_paused: Boolean(task.project_paused),
      duplicate_count: task.duplicate_count || 1,
    };
  }

  function taskListPath() {
    const params = new URLSearchParams();
    params.set("limit", String(TASK_RAIL_LIMIT));
    const statuses = TASK_STATUS_GROUPS[state.taskStatusFilter] || [];
    for (const status of statuses) params.append("status", status);
    return API + "/tasks?" + params.toString();
  }

  function parseTaskKey(value) {
    const match = String(value || "").trim().match(/^([^/\s]+)\/([0-9]+)$/);
    if (!match) return null;
    return {
      key: match[1] + "/" + match[2],
      project: match[1],
      task_number: match[2],
    };
  }

  function taskDedupeKey(task) {
    return [
      task.project || "",
      task.work_status || "",
      String(task.title || "").trim().toLowerCase(),
    ].join("\u0000");
  }

  function dedupeTaskSurfaces(tasks) {
    const byKey = {};
    const out = [];
    for (const task of tasks) {
      const key = taskDedupeKey(task);
      const existing = byKey[key];
      if (existing) {
        existing.duplicate_count += 1;
        if (
          task.updated_at
          && (!existing.updated_at || task.updated_at > existing.updated_at)
        ) {
          existing.updated_at = task.updated_at;
        }
        continue;
      }
      byKey[key] = task;
      out.push(task);
    }
    return out;
  }

  function taskDotClass(status) {
    if (status === "in_progress" || status === "rework") return "present";
    if (status === "queued" || status === "review") return "waiting";
    if (status === "blocked" || status === "on_hold") return "dead";
    if (status === "done") return "done";
    if (status === "cancelled") return "dead";
    return "";
  }

  function appendRailGroup(list, label) {
    list.appendChild(el("li", { class: "surface-group", text: label }));
  }

  function countFor(project, name) {
    const counts = project && typeof project.task_counts === "object"
      ? project.task_counts : {};
    return Number(counts && counts[name]) || 0;
  }

  function activeTaskCount(project) {
    return countFor(project, "in_progress") + countFor(project, "rework");
  }

  function projectUrgency(project) {
    const blocked = countFor(project, "blocked") + countFor(project, "on_hold");
    const queued = countFor(project, "queued");
    const active = activeTaskCount(project);
    const review = countFor(project, "review");
    if (!project.tracked || project.glyph === "paused") {
      return { rank: 5, label: "paused", className: "paused" };
    }
    if (blocked > 0) {
      return { rank: 0, label: "blocked", className: "blocked" };
    }
    if (queued > 0 && active === 0) {
      return { rank: 1, label: "stalled", className: "stalled" };
    }
    if (project.pending_plan_review || project.open_inbox_count > 0 || review > 0) {
      return { rank: 2, label: "attention", className: "attention" };
    }
    if (active > 0) {
      return { rank: 3, label: "active", className: "active" };
    }
    return { rank: 4, label: "healthy", className: "healthy" };
  }

  function projectLastActivityMs(project) {
    const raw = project && project.last_activity_at;
    if (!raw) return 0;
    const parsed = Date.parse(raw);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function projectMeta(project) {
    const parts = [];
    const blocked = countFor(project, "blocked") + countFor(project, "on_hold");
    const queued = countFor(project, "queued");
    const active = activeTaskCount(project);
    const inbox = Number(project.open_inbox_count) || 0;
    if (blocked > 0) parts.push(blocked + " blocked");
    if (queued > 0) parts.push(queued + " queued");
    if (active > 0) parts.push(active + " active");
    if (inbox > 0) parts.push(inbox + " inbox");
    const last = projectLastActivityMs(project);
    if (last > 0) parts.push("last " + formatShortTime(project.last_activity_at));
    return parts.join(" · ") || "no open work";
  }

  function projectAttentionCount(project) {
    const blocked = countFor(project, "blocked") + countFor(project, "on_hold");
    const review = countFor(project, "review");
    const inbox = Number(project.open_inbox_count) || 0;
    const plan = project.pending_plan_review ? 1 : 0;
    return blocked + review + inbox + plan;
  }

  function projectTriageText(attentionTotal, attentionProjects) {
    if (attentionTotal <= 0) return "All projects clear";
    const projectCount = attentionProjects.length;
    const scope = projectCount === 1
      ? " in 1 project"
      : " across " + projectCount + " projects";
    const names = attentionProjects.slice(0, 3).map((project) => (
      project.name || project.key
    )).join(", ");
    return attentionTotal + " " + plural(attentionTotal, "project status flag")
      + scope
      + (names ? " - " + names : "");
  }

  function formatShortTime(value) {
    const parsed = Date.parse(value);
    if (!Number.isFinite(parsed)) return String(value || "");
    return new Date(parsed).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function sortProjects(projects) {
    const sorted = projects.slice();
    if (state.projectSort === "name") {
      sorted.sort((a, b) => String(a.name || a.key).localeCompare(String(b.name || b.key)));
    } else if (state.projectSort === "inbox_desc") {
      sorted.sort((a, b) => (
        (Number(b.open_inbox_count) || 0) - (Number(a.open_inbox_count) || 0)
        || String(a.name || a.key).localeCompare(String(b.name || b.key))
      ));
    } else if (state.projectSort === "recent") {
      sorted.sort((a, b) => (
        projectLastActivityMs(b) - projectLastActivityMs(a)
        || String(a.name || a.key).localeCompare(String(b.name || b.key))
      ));
    } else {
      sorted.sort((a, b) => (
        projectUrgency(a).rank - projectUrgency(b).rank
        || String(a.name || a.key).localeCompare(String(b.name || b.key))
      ));
    }
    return sorted;
  }

  function renderProjectItem(list, project) {
    const urgency = projectUrgency(project);
    const key = project.key || "";
    const li = el("li", {
      "data-project": key,
      "class": (
        state.selectedProject === key ? "active " : ""
      ) + "project-" + urgency.className,
    }, [
      el("span", { class: "project-name" }, [
        el("span", {
          class: "project-dot project-dot-" + urgency.className,
        }),
        document.createTextNode(project.name || key),
      ]),
      el("span", {
        class: "project-meta",
        text: urgency.label + " · " + projectMeta(project),
      }),
    ]);
    li.addEventListener("click", () => selectProject(key));
    list.appendChild(li);
  }

  function renderProjects() {
    const list = $("project-list");
    if (!list) return;
    list.innerHTML = "";

    const all = el("li", {
      "data-project": "",
      "class": state.selectedProject ? "project-all" : "project-all active",
    }, [
      el("span", { class: "project-name" }, [
        el("span", { class: "project-dot project-dot-all" }),
        document.createTextNode("All projects"),
      ]),
      el("span", {
        class: "project-meta",
        text: state.projects.length + " tracked",
      }),
    ]);
    all.addEventListener("click", () => selectProject(null));
    list.appendChild(all);

    if (state.projectLoadError) {
      list.appendChild(el("li", {
        class: "project-empty",
        text: "projects unavailable: " + state.projectLoadError.message,
      }));
      return;
    }
    const attentionProjects = sortProjects(
      state.projects.filter((project) => projectAttentionCount(project) > 0),
    );
    const attentionTotal = attentionProjects.reduce(
      (sum, project) => sum + projectAttentionCount(project),
      0,
    );
    const triageText = projectTriageText(attentionTotal, attentionProjects);
    const triage = el("li", {
      class: "project-triage " + (attentionTotal > 0 ? "attention" : "clear"),
    }, [
      el("span", { class: "project-name", text: triageText }),
    ]);
    if (attentionProjects[0]) {
      triage.addEventListener("click", () => selectProject(attentionProjects[0].key));
    }
    list.appendChild(triage);

    const filter = state.projectFilter.trim().toLowerCase();
    const projects = sortProjects(
      filter
        ? state.projects.filter((project) => (
          String(project.key || "").toLowerCase().includes(filter)
          || String(project.name || "").toLowerCase().includes(filter)
          || String(project.path || "").toLowerCase().includes(filter)
          || String(project.persona_name || "").toLowerCase().includes(filter)
        ))
        : state.projects,
    );
    if (projects.length === 0) {
      list.appendChild(el("li", {
        class: "project-empty",
        text: state.projectLoading
          ? "loading projects..." : "no matching projects",
      }));
      return;
    }
    for (const project of projects) renderProjectItem(list, project);
  }

  function renderChatSurfaceItem(list, s) {
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
        "class": (
          state.selectedKind === "chat"
          && state.selectedSurface === s.session_name
        ) ? "active" : "",
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

  function renderTaskSurfaceItem(list, task) {
    const meta = [
      "task",
      task.work_status,
      task.project,
    ].filter(Boolean);
    if (task.assignee) meta.push("@" + task.assignee);
    if (task.duplicate_count > 1) meta.push("\u00d7" + task.duplicate_count);
    const li = el(
      "li",
      {
        "data-task": task.key,
        "class": (
          "task-surface "
          + (
            state.selectedKind === "task"
            && state.selectedTaskKey === task.key
              ? "active" : ""
          )
        ).trim(),
      },
      [
        el("span", { class: "surface-name" }, [
          el("span", {
            class: ("surface-dot " + taskDotClass(task.work_status)).trim(),
          }),
          document.createTextNode(task.title),
        ]),
        el("span", {
          class: "surface-meta",
          text: meta.join(" · "),
        }),
      ],
    );
    li.addEventListener("click", () => selectTask(task.key));
    list.appendChild(li);
  }

  function renderDirectTaskOpenItem(list, parsed) {
    const active = state.selectedKind === "task"
      && state.selectedTaskKey === parsed.key;
    const li = el(
      "li",
      {
        "data-task": parsed.key,
        "class": ("task-surface direct-task" + (active ? " active" : "")),
      },
      [
        el("span", { class: "surface-name" }, [
          el("span", { class: "surface-dot waiting" }),
          document.createTextNode("Open " + parsed.key),
        ]),
        el("span", {
          class: "surface-meta",
          text: "fetch task detail",
        }),
      ],
    );
    li.addEventListener("click", () => selectTask(parsed.key));
    list.appendChild(li);
  }

  function selectedProjectMatches(project) {
    return !state.selectedProject || project === state.selectedProject;
  }

  function surfaceMatchesFilter(surface, filter) {
    if (!filter) return true;
    return (
      String(surface.session_name || "").toLowerCase().includes(filter)
      || String(surface.project || "").toLowerCase().includes(filter)
      || String(surface.surface_type || "").toLowerCase().includes(filter)
      || String(surface.persona || "").toLowerCase().includes(filter)
    );
  }

  function taskMatchesFilter(task, filter) {
    if (!filter) return true;
    return (
      String(task.key || "").toLowerCase().includes(filter)
      || String(task.title || "").toLowerCase().includes(filter)
      || String(task.project || "").toLowerCase().includes(filter)
      || String(task.work_status || "").toLowerCase().includes(filter)
      || String(task.assignee || "").toLowerCase().includes(filter)
    );
  }

  function renderSurfaces() {
    const list = $("surface-list");
    list.innerHTML = "";
    const filter = state.surfaceFilter.trim().toLowerCase();
    const surfaces = state.surfaces.filter((s) => (
      selectedProjectMatches(s.project || "")
      && surfaceMatchesFilter(s, filter)
    ));
    const taskSurfaces = state.taskSurfaces.filter((task) => (
      selectedProjectMatches(task.project || "")
      && taskMatchesFilter(task, filter)
    ));
    const directTask = parseTaskKey(filter);
    const directTaskVisible = Boolean(
      directTask
      && selectedProjectMatches(directTask.project)
      && !taskSurfaces.some((task) => task.key === directTask.key)
    );
    const hasRegistered = (
      state.surfaces.length > 0
      || state.taskSurfaces.length > 0
      || state.surfaceLoadError
      || state.taskLoadError
      || directTaskVisible
    );
    const isLoading = state.surfaceLoading || state.taskLoading;
    const allSectionsPending = state.surfaceLoading && state.taskLoading;
    if (!hasRegistered && isLoading && allSectionsPending) {
      list.appendChild(
        fetchAffordance(
          "surfaces", "li", "surface-empty", "loading surfaces...",
        ),
      );
      return;
    }
    if (!hasRegistered) {
      list.appendChild(
        el("li", { class: "surface-empty", text: "no surfaces registered" }),
      );
      return;
    }
    if (
      filter
      && surfaces.length === 0
      && taskSurfaces.length === 0
      && !state.surfaceLoadError
      && !state.taskLoadError
      && !directTaskVisible
    ) {
      list.appendChild(
        el("li", { class: "surface-empty", text: "no matching surfaces" }),
      );
      return;
    }
    if (surfaces.length > 0) {
      appendRailGroup(list, "Chat surfaces");
      for (const s of surfaces) renderChatSurfaceItem(list, s);
    } else if (state.surfaceLoadError) {
      appendRailGroup(list, "Chat surfaces");
      list.appendChild(el("li", {
        class: "surface-empty",
        text: "error: chat unavailable: " + state.surfaceLoadError.message,
      }));
    } else if (state.surfaceLoading) {
      appendRailGroup(list, "Chat surfaces");
      list.appendChild(el("li", {
        class: "surface-empty",
        text: "loading chat surfaces...",
      }));
    }
    if (taskSurfaces.length > 0 || state.taskLoadError || directTaskVisible) {
      appendRailGroup(list, "Tasks");
      if (directTaskVisible) renderDirectTaskOpenItem(list, directTask);
      const visibleTasks = taskSurfaces.slice(0, TASK_RENDER_LIMIT);
      for (const task of visibleTasks) renderTaskSurfaceItem(list, task);
      if (taskSurfaces.length > visibleTasks.length) {
        list.appendChild(el("li", {
          class: "surface-empty surface-summary",
          text: "showing " + visibleTasks.length + " of "
            + taskSurfaces.length + " tasks",
        }));
      }
      if (state.taskLoadError) {
        list.appendChild(el("li", {
          class: "surface-empty",
          text: "error: tasks unavailable: " + state.taskLoadError.message,
        }));
      }
    } else if (state.taskLoading) {
      appendRailGroup(list, "Tasks");
      list.appendChild(el("li", {
        class: "surface-empty",
        text: "loading tasks...",
      }));
    }
  }

  function renderSurfaceError(err) {
    const list = $("surface-list");
    list.innerHTML = "";
    list.appendChild(
      el("li", { class: "surface-empty", text: "error: " + err.message }),
    );
  }

  function clearSelection() {
    state.selectedKind = null;
    state.selectedSurface = null;
    state.selectedTaskKey = null;
    renderNoSelection();
  }

  function ensureSelectionVisible() {
    if (!state.selectedProject) return;
    if (state.selectedKind === "chat") {
      const surface = surfaceByName(state.selectedSurface);
      if (!surface || surface.project !== state.selectedProject) clearSelection();
    } else if (state.selectedKind === "task") {
      const task = state.taskSurfaces.find((item) => (
        item.key === state.selectedTaskKey
      ));
      const parsed = parseTaskKey(state.selectedTaskKey);
      if (
        task
        ? task.project !== state.selectedProject
        : (!parsed || parsed.project !== state.selectedProject)
      ) {
        clearSelection();
      }
    }
  }

  function selectProject(key) {
    state.selectedProject = key || null;
    ensureSelectionVisible();
    renderProjects();
    renderSurfaces();
    pollDashboard();
    loadActivity();
  }

  function selectSurface(name) {
    state.selectedKind = "chat";
    state.selectedSurface = name;
    state.selectedTaskKey = null;
    state.inbox.selectedId = null;
    $("pane-title").textContent = name;
    $("pane-meta").textContent = "";
    $("send-input").disabled = false;
    $("send-button").disabled = false;
    updateStopAgentButton();
    renderSurfaces();
    loadHistory(name);
    if (state.auditExpanded[name]) loadAuditForSurface(name);
  }

  function numeric(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  }

  function plural(count, singular, pluralText) {
    return count === 1 ? singular : (pluralText || singular + "s");
  }

  function dashboardProjectLabel(data) {
    const projects = data && Array.isArray(data.projects) ? data.projects : [];
    const project = projects.length === 1 ? projects[0] : null;
    if (!project) return state.selectedProject || "Project";
    return project.name || project.key || state.selectedProject || "Project";
  }

  function firstProjectAlert(data) {
    const alerts = data && Array.isArray(data.project_alerts)
      ? data.project_alerts : [];
    return alerts.length > 0 ? alerts[0] : null;
  }

  function projectAlertHeadline(data, alerts) {
    const project = dashboardProjectLabel(data);
    const firstAlert = firstProjectAlert(data);
    const title = firstAlert && firstAlert.alert_type === "plan_missing"
      ? project + " is waiting on a plan"
      : project + " needs attention";
    const detail = firstAlert && firstAlert.message
      ? firstAlert.message
      : alerts + " project " + plural(alerts, "alert") + " waiting.";
    return {
      title: title,
      detail: detail,
      actionLabel: "Open alerts",
      target: "alerts",
    };
  }

  function dashboardStatus(data) {
    if (!data || typeof data !== "object") {
      return {
        title: "Loading workspace state...",
        detail: "Dashboard state is loading.",
        actionLabel: "Open inbox",
        target: "inbox",
      };
    }
    const rollups = data.rollups && typeof data.rollups === "object"
      ? data.rollups : {};
    const planReviews = numeric(rollups.pending_plan_reviews);
    const inbox = numeric(rollups.open_inbox_count);
    const alerts = numeric(rollups.alert_count);
    const total = planReviews + inbox;
    const scopedAlerts = isScoped(data.scoped_fields, "rollups.alert_count");
    const alertKind = scopedAlerts ? "project alert" : "background alert";
    const alertNote = alerts > 0
      ? " " + alerts + " " + plural(alerts, alertKind)
        + " being watched."
      : "";
    if (total === 0) {
      if (scopedAlerts && alerts > 0) {
        return projectAlertHeadline(data, alerts);
      }
      const sweeps = numeric(rollups.sweep_count_24h);
      const recoveries = numeric(rollups.recovery_count_24h);
      const proof = sweeps || recoveries
        ? "24h: " + sweeps + " "
          + plural(sweeps, "sweep") + " / " + recoveries + " "
          + plural(recoveries, "recovery", "recoveries") + "."
          + alertNote
        : "No inbox items or plan reviews waiting." + alertNote;
      return {
        title: "All handled - Polly's got it",
        detail: proof,
        actionLabel: "Open inbox",
        target: "inbox",
      };
    }
    if (planReviews > 0) {
      return {
        title: total + " " + plural(total, "thing") + " "
          + (total === 1 ? "needs" : "need") + " you",
        detail: planReviews + " "
          + plural(planReviews, "plan review") + " waiting." + alertNote,
        actionLabel: "Open plan reviews",
        target: "plan-review",
      };
    }
    if (inbox > 0) {
      return {
        title: total + " " + plural(total, "thing") + " "
          + (total === 1 ? "needs" : "need") + " you",
        detail: inbox + " "
          + plural(inbox, "inbox item") + " waiting." + alertNote,
        actionLabel: "Open inbox",
        target: "inbox",
      };
    }
  }

  function runDashboardStatusAction(status) {
    if (!status) return;
    if (status.target === "plan-review") {
      selectInbox({ type: "plan_review" });
      return;
    }
    if (status.target === "alerts") {
      selectAlerts();
      return;
    }
    selectInbox();
  }

  function renderNoSelection() {
    state.selectedKind = null;
    state.selectedSurface = null;
    state.selectedTaskKey = null;
    state.inbox.selectedId = null;
    $("pane-title").textContent = "Ready";
    $("pane-meta").textContent = "";
    $("send-input").disabled = true;
    $("send-button").disabled = true;
    updateStopAgentButton();
    const openInbox = el("button", {
      class: "empty-action primary",
      type: "button",
      text: "Open inbox",
    });
    openInbox.addEventListener("click", () => selectInbox());
    const refresh = el("button", {
      class: "empty-action",
      type: "button",
      text: "Refresh",
    });
    refresh.addEventListener("click", () => {
      loadSurfaces();
      pollDashboard();
    });
    const list = $("message-list");
    clearMessageList(list);
    const status = dashboardStatus(state.dashboardData);
    const briefing = state.dashboardData
      && typeof state.dashboardData.briefing === "string"
      && state.dashboardData.briefing.trim()
      ? state.dashboardData.briefing.trim()
      : "";
    list.appendChild(el("div", { class: "empty-state" }, [
      el("div", { class: "empty-title", text: status.title }),
      briefing
        ? el("div", { class: "empty-briefing", text: briefing })
        : el("div", { class: "empty-copy", text: status.detail }),
      el("div", { class: "empty-actions" }, [openInbox, refresh]),
    ]));
    renderSurfaces();
  }

  function selectTask(key) {
    let task = state.taskSurfaces.find((item) => item.key === key);
    if (!task) {
      const parsed = parseTaskKey(key);
      if (!parsed) return;
      task = {
        key: parsed.key,
        task_id: parsed.key,
        project: parsed.project,
        task_number: parsed.task_number,
        title: parsed.key,
        work_status: "",
        type: "",
        priority: "",
        assignee: "",
        updated_at: "",
      };
    }
    state.selectedKind = "task";
    state.selectedSurface = null;
    state.selectedTaskKey = key;
    state.inbox.selectedId = null;
    $("pane-title").textContent = task.key;
    $("pane-meta").textContent = [
      task.work_status,
      task.type,
      task.priority,
    ].filter(Boolean).join(" · ");
    $("send-input").disabled = true;
    $("send-button").disabled = true;
    updateStopAgentButton();
    renderSurfaces();
    renderTaskSummary(task);
    loadTaskDetail(task);
  }

  // §1.4.5 (#2336): compact "stuck for X" string from dwell_seconds so
  // the operator can see at a glance that a queued task has been
  // sitting for 40 hours.
  function formatDwell(seconds) {
    if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) {
      return null;
    }
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return days + "d " + hours + "h";
    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m";
    return Math.floor(seconds) + "s";
  }

  const TERMINAL_TASK_STATES = new Set(["done", "cancelled"]);

  function renderTaskSummary(task, detail) {
    const data = detail || task;
    const list = $("message-list");
    clearMessageList(list);
    const status = data.work_status;
    const isTerminal = TERMINAL_TASK_STATES.has(status);
    // §1.4.5 (#2336): dwell row when non-terminal — surfaces "stuck
    // for X" right next to Status.
    const dwellStr = !isTerminal ? formatDwell(data.dwell_seconds) : null;
    const rows = [];
    if (data.type && data.type !== "task") rows.push(["Type", data.type]);
    rows.push(
      ["Status", status],
      ["Project", data.project],
      ["Task", data.task_number],
      ["Assignee", data.assignee],
      ["Priority", data.priority],
      ["Updated", data.updated_at],
    );
    if (Array.isArray(data.labels) && data.labels.length > 0) {
      rows.push(["Labels", data.labels.join(", ")]);
    }
    if (data.requires_human_review != null) {
      rows.push([
        "Human review",
        data.requires_human_review ? "required" : "not required",
      ]);
    }
    if (dwellStr) {
      rows.push(["Dwell", dwellStr + " in " + status]);
    }
    // §1.4.5 (#2337): list dependency blockers instead of swallowing
    // ``relationships.blocked_by``.
    const blockedBy = (data.relationships && data.relationships.blocked_by) || [];
    if (blockedBy.length) {
      rows.push(["Blocked by", blockedBy.join(", ")]);
    }
    const filteredRows = rows.filter((row) => row[1] != null && row[1] !== "");
    const children = [
      el("div", { class: "task-detail-title", text: data.title || task.title }),
    ];
    // §1.4.5 (#2335): paused-project banner so a queued task that
    // can't be claimed has an obvious reason. Mirrors the CLI warning
    // in ``_print_task``.
    if (data.project_paused) {
      children.push(el("div", {
        class: "error-banner",
        text: "PROJECT PAUSED — task will not be claimed until the "
          + "project is resumed.",
      }));
    }
    // §1.4.5 (#2336): claim-without-session warnings (both shapes —
    // claimed-by-dead and assignee-without-claim).
    if (data.claimed_by_session && !isTerminal && data.dwell_seconds != null
        && data.dwell_seconds > 5 * 60) {
      children.push(el("div", {
        class: "error-banner",
        text: "claimed-by " + data.claimed_by_session
          + " has been holding this task for "
          + (dwellStr || data.dwell_seconds + "s")
          + " — session may be dead. Check `pm sessions health`.",
      }));
    } else if (data.assignee && !data.claimed_by_session && !isTerminal
        && status === "in_progress") {
      children.push(el("div", {
        class: "error-banner",
        text: "assignee " + data.assignee + " has no live session "
          + "(claimed_by_session is null); task may be stranded.",
      }));
    }
    if (status === "blocked" && blockedBy.length === 0) {
      children.push(el("div", {
        class: "error-banner",
        text: "Status=blocked but no blocked_by relationships — "
          + "likely stale state, run `pm doctor`.",
      }));
    }
    appendTaskDetailSection(children, "Description", data.description);
    for (const row of filteredRows) {
      children.push(el("div", { class: "task-detail-row" }, [
        el("span", { class: "task-detail-label", text: row[0] }),
        el("span", { class: "task-detail-value", text: String(row[1]) }),
      ]));
    }
    appendTaskDetailSection(
      children, "Acceptance criteria", data.acceptance_criteria,
    );
    appendTaskDetailSection(children, "Constraints", data.constraints);
    appendTaskDetailList(children, "Relevant files", data.relevant_files);
    const actions = [];
    if (data.work_status === "queued" || data.work_status === "rework") {
      const claim = el("button", {
        class: "task-action-button primary",
        type: "button",
        text: data.work_status === "rework" ? "Resume" : "Start",
      });
      claim.addEventListener("click", () => claimTaskFromDetail(data));
      actions.push(claim);
    }
    if (data.work_status === "cancelled") {
      const reopen = el("button", {
        class: "task-action-button",
        type: "button",
        text: "Reopen",
      });
      reopen.addEventListener("click", () => reopenTaskFromDetail(data));
      actions.push(reopen);
    } else if (data.work_status && data.work_status !== "done") {
      const cancel = el("button", {
        class: "task-action-button danger",
        type: "button",
        text: "Cancel",
      });
      cancel.addEventListener("click", () => cancelTaskFromDetail(data));
      actions.push(cancel);
    }
    if (actions.length > 0) {
      children.push(el("div", { class: "task-detail-actions" }, actions));
    }
    list.appendChild(el("div", { class: "task-detail" }, children));
  }

  function appendTaskDetailSection(children, heading, text) {
    if (text == null || text === "") return;
    children.push(el("div", { class: "task-detail-section" }, [
      el("h3", { class: "task-detail-heading", text: heading }),
      el("div", { class: "task-detail-section-body", text: String(text) }),
    ]));
  }

  function appendTaskDetailList(children, heading, items) {
    if (!Array.isArray(items) || items.length === 0) return;
    children.push(el("div", { class: "task-detail-section" }, [
      el("h3", { class: "task-detail-heading", text: heading }),
      el("ul", { class: "task-detail-list" }, items.map((item) => (
        el("li", { text: String(item) })
      ))),
    ]));
  }

  async function claimTaskFromDetail(task) {
    if (!task.project || !task.task_number) return;
    const path = API + "/tasks/" + encodeURIComponent(task.project)
      + "/" + encodeURIComponent(task.task_number) + "/claim";
    try {
      const result = await apiJson(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ actor: CLAIM_ACTOR }),
      });
      renderTaskSummary(result.task || task);
      if (Array.isArray(result.warnings)) {
        for (const warning of result.warnings) {
          if (warning) showToast("error", warning);
        }
      }
      loadSurfaces();
    } catch (err) {
      const list = $("message-list");
      list.appendChild(el("div", {
        class: "error-banner",
        text: "start error: " + err.message,
      }));
    }
  }

  async function cancelTaskFromDetail(task) {
    if (!task.project || !task.task_number) return;
    let force = false;
    if (task.work_status === "in_progress") {
      const assignee = task.assignee || "worker";
      if (!window.confirm(
        "Worker " + assignee + " currently working this task. Cancel anyway?",
      )) {
        return;
      }
      force = true;
    }
    const path = API + "/tasks/" + encodeURIComponent(task.project)
      + "/" + encodeURIComponent(task.task_number)
      + "/cancel" + (force ? "?force=true" : "");
    try {
      const result = await apiJson(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "cancelled from web UI" }),
      });
      renderTaskSummary(result.task || task);
      loadSurfaces();
    } catch (err) {
      const list = $("message-list");
      list.appendChild(el("div", {
        class: "error-banner",
        text: "cancel error: " + err.message,
      }));
    }
  }

  async function reopenTaskFromDetail(task) {
    if (!task.project || !task.task_number) return;
    const path = API + "/tasks/" + encodeURIComponent(task.project)
      + "/" + encodeURIComponent(task.task_number) + "/reopen";
    try {
      const result = await apiJson(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "reopened from web UI" }),
      });
      renderTaskSummary(result.task || task);
      loadSurfaces();
    } catch (err) {
      const list = $("message-list");
      list.appendChild(el("div", {
        class: "error-banner",
        text: "reopen error: " + err.message,
      }));
    }
  }

  async function loadTaskDetail(task) {
    if (!task.project || !task.task_number) return;
    try {
      const path = API + "/tasks/" + encodeURIComponent(task.project)
        + "/" + encodeURIComponent(task.task_number);
      const detail = await apiJson(path);
      if (
        state.selectedKind === "task"
        && state.selectedTaskKey === task.key
      ) {
        renderTaskSummary(task, detail);
      }
    } catch (err) {
      if (
        state.selectedKind === "task"
        && state.selectedTaskKey === task.key
      ) {
        const list = $("message-list");
        list.appendChild(el("div", {
          class: "error-banner",
          text: "task detail error: " + err.message,
        }));
      }
    }
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
    if (entry && typeof entry.summary === "string" && entry.summary.trim()) {
      return entry.summary.trim();
    }
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

  function pendingMessagesFor(name) {
    if (!state.pendingMessages[name]) state.pendingMessages[name] = [];
    return state.pendingMessages[name];
  }

  function normalizeMessageText(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function reconcilePendingMessages(name, messages) {
    const serverTexts = new Set(
      messages.map((message) => normalizeMessageText(message.text)),
    );
    const pending = pendingMessagesFor(name).filter((message) => (
      message.status === "failed"
      || !serverTexts.has(normalizeMessageText(message.text))
    ));
    state.pendingMessages[name] = pending;
    return pending;
  }

  function removeLocalEchoNodes(list) {
    for (const node of list.querySelectorAll(".message-local-echo")) {
      node.remove();
    }
  }

  function localEchoNode(message) {
    const status = message.status === "failed"
      ? "failed"
      : message.status === "sending" ? "sending" : "pending";
    const children = [
      el("div", { class: "message-head" }, [
        el("span", { class: "message-actor", text: "you" }),
        el("span", { class: "message-ts", text: message.ts || "" }),
        el("span", { class: "message-type", text: status }),
      ]),
      el("div", { class: "message-text", text: message.text || "" }),
    ];
    if (message.error) {
      children.push(el("div", {
        class: "message-error",
        text: "send failed: " + message.error,
      }));
    }
    return el("div", {
      class: "message message-role-user message-local-echo message-local-"
        + status,
      "data-local-message": message.id,
    }, children);
  }

  function renderLocalEchoes(name) {
    if (state.selectedKind !== "chat" || state.selectedSurface !== name) return;
    const list = $("message-list");
    removeLocalEchoNodes(list);
    const pending = pendingMessagesFor(name);
    if (pending.length === 0) return;
    const empty = list.querySelector(".message-empty");
    if (empty) empty.remove();
    const auditPanel = list.querySelector(".audit-panel");
    for (const message of pending) {
      const node = localEchoNode(message);
      if (auditPanel) list.insertBefore(node, auditPanel);
      else list.appendChild(node);
    }
    list.scrollTop = list.scrollHeight;
  }

  function addLocalEcho(name, text) {
    const message = {
      id: "local-" + Date.now() + "-" + Math.random().toString(16).slice(2),
      text: text,
      ts: new Date().toISOString(),
      status: "sending",
      error: null,
    };
    pendingMessagesFor(name).push(message);
    renderLocalEchoes(name);
    return message;
  }

  function updateLocalEcho(name, id, patch) {
    const pending = pendingMessagesFor(name);
    const message = pending.find((item) => item.id === id);
    if (!message) return;
    Object.assign(message, patch);
    renderLocalEchoes(name);
  }

  function historyMessageSignature(message, index) {
    const m = message || {};
    return JSON.stringify([
      m.id == null ? "index:" + index : "id:" + String(m.id),
      String(m.ts || ""),
      String(m.role || ""),
      String(m.actor || ""),
      String(m.type || ""),
      String(m.text || ""),
      JSON.stringify(m.metadata || {}),
    ]);
  }

  function historyRenderSignature(data, messages) {
    return JSON.stringify([
      String(data.session_name || ""),
      String(data.surface_type || ""),
      String(data.transcript_source || ""),
      String(messages.length),
      messages.map(historyMessageSignature),
    ]);
  }

  function askUserQuestions(message) {
    const metadata = message && message.metadata && typeof message.metadata === "object"
      ? message.metadata : {};
    return Array.isArray(metadata.questions) ? metadata.questions : [];
  }

  function askUserOptionLabel(option) {
    if (typeof option === "string") return option;
    if (!option || typeof option !== "object") return "";
    return String(option.label || option.value || "").trim();
  }

  function askUserOptionDescription(option) {
    if (!option || typeof option !== "object") return "";
    return String(option.description || option.help || "").trim();
  }

  function askUserQuestionAllowsMultiple(question) {
    if (!question || typeof question !== "object") return false;
    return Boolean(
      question.multiSelect
      || question.multi_select
      || question.multiselect
      || question.multiple,
    );
  }

  function currentAskUserFromMessages(messages) {
    for (const message of messages || []) {
      const type = String(message && message.type || "");
      const role = String(message && message.role || "");
      if (type === "ask_user" && message.id) return message;
      if (role === "user" || type === "tool_result") return null;
      if (type === "text" && role === "assistant") return null;
    }
    return null;
  }

  function buildChatSendBody(name, text) {
    const askUser = state.pendingAskUser[name];
    if (askUser && askUser.id) {
      return { answer_to: askUser.id, text: text };
    }
    return { text: text };
  }

  function askUserAnswerText(selections, notes) {
    const picked = (selections || []).filter(Boolean);
    const extra = String(notes || "").trim();
    if (picked.length && extra) return picked.join(", ") + " - " + extra;
    if (picked.length) return picked.join(", ");
    return extra;
  }

  async function sendAskUserAnswer(name, answerTo, selections, notes) {
    if (!name || !answerTo) return;
    const answerText = askUserAnswerText(selections, notes);
    if (!answerText) {
      showToast("warn", "choose an answer first");
      return;
    }
    const local = addLocalEcho(name, answerText);
    const path = API + "/chat/" + encodeURIComponent(name) + "/send";
    try {
      await apiFetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answer_to: answerTo,
          selections: selections || [],
          notes: notes || "",
        }),
      });
      updateLocalEcho(name, local.id, { status: "pending" });
      delete state.pendingAskUser[name];
      setTimeout(() => loadHistory(name), 400);
    } catch (err) {
      updateLocalEcho(name, local.id, {
        status: "failed",
        error: err.message || String(err),
      });
      showToast("error", "answer failed: " + (err.message || String(err)));
      throw err;
    }
  }

  function askUserControlsNode(message, sessionName) {
    const questions = askUserQuestions(message);
    if (!questions.length || !message.id) return null;
    const groups = [];
    questions.forEach((question, qIndex) => {
      const options = Array.isArray(question.options) ? question.options : [];
      const optionNodes = [];
      options.forEach((option) => {
        const label = askUserOptionLabel(option);
        if (!label) return;
        const inputType = askUserQuestionAllowsMultiple(question)
          ? "checkbox" : "radio";
        const input = el("input", {
          type: inputType,
          name: "ask-user-" + message.id + "-" + qIndex,
          value: label,
          "data-ask-user-option": label,
        });
        const description = askUserOptionDescription(option);
        const children = [
          input,
          el("span", { class: "ask-user-option-label", text: label }),
        ];
        if (description) {
          children.push(el("span", {
            class: "ask-user-option-description",
            text: description,
          }));
        }
        optionNodes.push(el("label", { class: "ask-user-option" }, children));
      });
      const groupChildren = [];
      const questionText = String(question.question || "").trim();
      if (questions.length > 1 && questionText) {
        groupChildren.push(el("div", {
          class: "ask-user-question",
          text: questionText,
        }));
      }
      groupChildren.push(el("div", { class: "ask-user-options" }, optionNodes));
      groups.push(el("fieldset", { class: "ask-user-group" }, groupChildren));
    });
    const submit = el("button", {
      type: "submit",
      class: "ask-user-submit",
      text: "Submit answer",
    });
    const form = el("form", {
      class: "ask-user-card",
      "data-answer-to": message.id,
    }, groups.concat([submit]));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const inputs = Array.from(
        form.querySelectorAll("input[data-ask-user-option]:checked"),
      );
      const selections = inputs.map((input) => input.value).filter(Boolean);
      sendAskUserAnswer(sessionName, message.id, selections, "").catch(() => {});
    });
    return form;
  }

  function shouldSkipHistoryRender(list, sessionName, signature) {
    return (
      list.dataset.historySession === sessionName
      && state.historyRenderSignatures[sessionName] === signature
    );
  }

  function markHistoryRendered(list, sessionName, signature) {
    state.historyRenderSignatures[sessionName] = signature;
    list.dataset.historySession = sessionName;
  }

  function historyMessageNode(message, sessionName) {
    const m = message || {};
    const roleClass = "message message-role-" + (m.role || "system");
    const children = [
      el("div", { class: "message-head" }, [
        el("span", { class: "message-actor", text: m.actor || m.role || "?" }),
        el("span", { class: "message-ts", text: m.ts || "" }),
        el("span", { class: "message-type", text: m.type || "" }),
      ]),
      el("div", { class: "message-text", text: m.text || "" }),
    ];
    const askControls = askUserControlsNode(m, sessionName);
    if (askControls) children.push(askControls);
    return el("div", { class: roleClass }, children);
  }

  async function loadHistory(name, opts) {
    if (!name) return;
    const force = opts && opts.force;
    const fetchName = "history:" + name;
    if (state.historyInFlight[name]) {
      if (force) {
        abortFetchState(fetchName);
        state.historyInFlight[name] = false;
      } else {
        state.historyRefreshQueued[name] = true;
        return;
      }
    }
    state.historyInFlight[name] = true;
    const request = beginFetchState(
      fetchName,
      "history",
      () => {
        if (state.selectedKind === "chat" && state.selectedSurface === name) {
          renderHistoryLoading(fetchName);
        }
      },
      () => loadHistory(name, { force: true }),
      { preserveSettled: true },
    );
    try {
      const path =
        API + "/chat/" + encodeURIComponent(name) + "/messages?limit="
        + MAX_MESSAGES + "&direction=desc";
      const data = await apiJson(path, { signal: request.signal });
      if (state.selectedKind === "chat" && state.selectedSurface === name) {
        renderHistory(data);
      }
    } catch (err) {
      if (isAbortError(err)) return;
      if (state.selectedKind === "chat" && state.selectedSurface === name) {
        renderHistoryError(err);
      }
    } finally {
      if (!finishFetchState(fetchName, request.seq)) return;
      state.historyInFlight[name] = false;
      if (state.historyRefreshQueued[name]) {
        state.historyRefreshQueued[name] = false;
        if (state.selectedKind === "chat" && state.selectedSurface === name) {
          loadHistory(name);
        }
      }
    }
  }

  function renderHistoryLoading(fetchName) {
    const list = $("message-list");
    clearMessageList(list);
    list.appendChild(
      fetchAffordance(fetchName, "div", "message-empty", "loading history..."),
    );
    updateStopAgentButton();
  }

  function renderHistory(data) {
    const list = $("message-list");
    const meta = [];
    if (data.surface_type) meta.push(data.surface_type);
    if (data.transcript_source) meta.push("src=" + data.transcript_source);
    $("pane-meta").textContent = meta.join(" · ");
    const rawMessages = Array.isArray(data.messages) ? data.messages : [];
    const msgs = rawMessages.slice(0, HISTORY_RENDER_LIMIT);
    const pendingAskUser = currentAskUserFromMessages(msgs);
    if (pendingAskUser) state.pendingAskUser[data.session_name] = pendingAskUser;
    else delete state.pendingAskUser[data.session_name];
    const pending = reconcilePendingMessages(data.session_name, msgs);
    state.surfaceMidStream[data.session_name] = (
      msgs.length > 0 && messageLooksMidStream(msgs[0])
    );
    updateStopAgentButton();
    const signature = historyRenderSignature(data, msgs);
    if (shouldSkipHistoryRender(list, data.session_name, signature)) {
      renderLocalEchoes(data.session_name);
      return;
    }
    clearMessageList(list);
    markHistoryRendered(list, data.session_name, signature);
    if (msgs.length === 0 && pending.length === 0) {
      list.appendChild(
        el("div", { class: "message-empty", text: "no messages yet" }),
      );
      renderAuditPanel(data.session_name);
      return;
    }
    // API returned newest-first; render oldest-first so the latest is
    // at the bottom (chat convention).
    const fragment = document.createDocumentFragment();
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      fragment.appendChild(historyMessageNode(msgs[i], data.session_name));
    }
    list.appendChild(fragment);
    renderLocalEchoes(data.session_name);
    list.scrollTop = list.scrollHeight;
    renderAuditPanel(data.session_name);
  }

  function renderHistoryError(err) {
    const list = $("message-list");
    clearMessageList(list);
    list.appendChild(
      el("div", { class: "error-banner", text: "history error: " + err.message }),
    );
    renderAuditPanel(state.selectedSurface);
    updateStopAgentButton();
  }

  // ----- send (bottom) ---------------------------------------------------

  async function sendMessage(name, text) {
    if (!name || !text) return;
    const local = addLocalEcho(name, text);
    const path = API + "/chat/" + encodeURIComponent(name) + "/send";
    try {
      await apiFetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildChatSendBody(name, text)),
      });
      updateLocalEcho(name, local.id, { status: "pending" });
      // Refresh after a short delay so the server-confirmed line can
      // replace the local echo once tmux capture catches up.
      setTimeout(() => loadHistory(name), 400);
    } catch (err) {
      updateLocalEcho(name, local.id, {
        status: "failed",
        error: err.message || String(err),
      });
      showToast("error", "send failed: " + (err.message || String(err)));
      throw err;
    }
  }

  async function interruptSurface(name) {
    if (!name) return;
    const path = API + "/sessions/" + encodeURIComponent(name) + "/interrupt";
    await apiFetch(path, { method: "POST" });
    state.surfaceMidStream[name] = false;
    updateStopAgentButton();
    showToast("ok", "sent interrupt to " + name);
  }

  // ----- inbox ------------------------------------------------------------

  function encodePathId(id) {
    return String(id || "")
      .split("/")
      .map((part) => encodeURIComponent(part))
      .join("/");
  }

  function inboxListPath(cursor, omitType) {
    const params = new URLSearchParams();
    const filters = state.inbox.filters;
    params.set("limit", String(INBOX_PAGE_LIMIT));
    if (cursor) params.set("cursor", cursor);
    if (filters.project.trim()) params.set("project", filters.project.trim());
    if (filters.state) params.set("state", filters.state);
    if (filters.type && !omitType) params.set("type", filters.type);
    return API + "/inbox?" + params.toString();
  }

  function selectInbox(filterPatch) {
    state.selectedKind = "inbox";
    state.selectedSurface = null;
    state.selectedTaskKey = null;
    if (filterPatch) {
      state.inbox.filters = Object.assign(
        {},
        state.inbox.filters,
        filterPatch,
      );
    }
    $("pane-title").textContent = "Inbox";
    $("pane-meta").textContent = "";
    $("send-input").disabled = true;
    $("send-button").disabled = true;
    updateStopAgentButton();
    renderSurfaces();
    renderInboxView();
    loadInbox(true);
  }

  function selectAlerts() {
    state.selectedKind = "alerts";
    state.selectedSurface = null;
    state.selectedTaskKey = null;
    state.inbox.selectedId = null;
    $("pane-title").textContent = "Alerts";
    $("pane-meta").textContent = "";
    $("send-input").disabled = true;
    $("send-button").disabled = true;
    updateStopAgentButton();
    renderSurfaces();
    renderAlertsView();
    loadAlerts();
    loadDoctorReport();
  }

  async function loadAlerts() {
    if (state.alerts.loading) {
      state.alerts.reloadQueued = true;
      return;
    }
    state.alerts.loading = true;
    state.alerts.error = null;
    renderAlertsView();
    try {
      const data = await apiJson(API + "/alerts?limit=100");
      state.alerts.items = Array.isArray(data.alerts) ? data.alerts : [];
    } catch (err) {
      state.alerts.error = err;
      state.alerts.items = [];
    } finally {
      state.alerts.loading = false;
      renderAlertsView();
      if (state.alerts.reloadQueued) {
        state.alerts.reloadQueued = false;
        loadAlerts();
      }
    }
  }

  async function loadDoctorReport() {
    if (state.alerts.doctorLoading) return;
    state.alerts.doctorLoading = true;
    state.alerts.doctorError = null;
    renderAlertsView();
    try {
      state.alerts.doctorReport = await apiJson(API + "/doctor/report");
    } catch (err) {
      if (err && err.status === 404) {
        state.alerts.doctorReport = null;
      } else {
        state.alerts.doctorError = err;
      }
    } finally {
      state.alerts.doctorLoading = false;
      renderAlertsView();
    }
  }

  async function runSessionDriftCheck() {
    if (state.alerts.doctorRunInFlight) return;
    state.alerts.doctorRunInFlight = true;
    state.alerts.doctorError = null;
    renderAlertsView();
    try {
      state.alerts.doctorReport = await apiJson(API + "/doctor/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ check: "session-drift", fix: false }),
      });
    } catch (err) {
      state.alerts.doctorError = err;
    } finally {
      state.alerts.doctorRunInFlight = false;
      renderAlertsView();
    }
  }

  function alertActionKey(item, action) {
    return String(item.id != null ? item.id : item.session_name || "alert")
      + ":" + String(action.kind || "action");
  }

  function setAlertAction(item, action, active) {
    const key = alertActionKey(item, action);
    if (active) state.alerts.actionInFlight[key] = true;
    else delete state.alerts.actionInFlight[key];
    renderAlertsView();
  }

  async function acknowledgeAlert(item, action) {
    if (item.id == null) return;
    setAlertAction(item, action, true);
    try {
      await apiJson(
        API + "/alerts/" + encodeURIComponent(String(item.id))
          + "/actions/acknowledge",
        { method: "POST" },
      );
      showToast("ok", "alert acknowledged");
      await loadAlerts();
      pollDashboard();
      loadActivity();
    } catch (err) {
      showToast("error", "acknowledge failed: " + err.message);
    } finally {
      setAlertAction(item, action, false);
    }
  }

  function routeAlertToInbox(action) {
    const parsed = parseTaskKey(action.task_id || "");
    const project = action.project_key || (parsed && parsed.project) || "";
    const filters = { type: "plan_review" };
    if (project) filters.project = project;
    selectInbox(filters);
  }

  function renderAlertAction(item, action) {
    const detail = action.hint || action.kind || "";
    const node = el("button", {
      class: "alert-action",
      type: "button",
      text: action.label || action.kind || "Action",
    });
    if (detail) node.setAttribute("title", detail);
    if (action.kind === "acknowledge") {
      const key = alertActionKey(item, action);
      node.disabled = Boolean(state.alerts.actionInFlight[key]) || item.id == null;
      node.addEventListener("click", () => acknowledgeAlert(item, action));
    } else if (action.kind === "route_inbox") {
      node.addEventListener("click", () => routeAlertToInbox(action));
    } else {
      node.disabled = true;
      node.setAttribute("aria-disabled", "true");
    }
    return node;
  }

  function renderAlertRow(item) {
    const label = [
      item.severity || "alert",
      item.channel || "",
    ].filter(Boolean).join(" · ");
    const actions = Array.isArray(item.actions) ? item.actions : [];
    const children = [
      el("div", { class: "alert-row-head" }, [
        el("span", { class: "alert-session", text: item.session_name || "unknown" }),
        el("span", { class: "alert-updated", text: formatShortTime(item.updated_at) }),
      ]),
      el("div", { class: "alert-row-type", text: item.alert_type || "alert" }),
      el("div", { class: "alert-row-message", text: item.message || "" }),
      el("div", { class: "alert-row-meta", text: label }),
    ];
    if (actions.length > 0) {
      children.push(el(
        "div",
        { class: "alert-actions" },
        actions.map((action) => renderAlertAction(item, action)),
      ));
    }
    return el("div", { class: "alert-row" }, children);
  }

  function sessionDriftRow(report) {
    const checks = report && Array.isArray(report.checks) ? report.checks : [];
    return checks.find((check) => check.name === "session-drift") || null;
  }

  function renderDoctorDriftPanel() {
    const run = el("button", {
      type: "button",
      class: "empty-action",
      text: state.alerts.doctorRunInFlight ? "Running..." : "Run session drift",
      "aria-label": "Run session drift doctor check",
    });
    run.disabled = state.alerts.doctorRunInFlight;
    run.addEventListener("click", () => runSessionDriftCheck());

    const children = [
      el("div", { class: "alert-panel-title", text: "Session drift" }),
    ];
    if (state.alerts.doctorLoading) {
      children.push(el("div", {
        class: "alert-row-message",
        text: "loading doctor report...",
      }));
    } else if (state.alerts.doctorError) {
      children.push(el("div", {
        class: "alert-row-message alert-error",
        text: "doctor unavailable: " + state.alerts.doctorError.message,
      }));
    } else {
      const row = sessionDriftRow(state.alerts.doctorReport);
      if (row) {
        children.push(el("div", {
          class: "alert-row-message",
          text: (row.status || (row.passed ? "ok" : "warning")),
        }));
        if (row.why) {
          children.push(el("div", { class: "alert-row-meta", text: row.why }));
        }
        if (row.fix) {
          children.push(el("div", { class: "alert-row-meta", text: row.fix }));
        }
      } else {
        children.push(el("div", {
          class: "alert-row-message",
          text: "No cached session-drift report.",
        }));
      }
    }
    children.push(el("div", { class: "empty-actions" }, [run]));
    return el("div", { class: "alert-doctor" }, children);
  }

  function renderAlertsView() {
    if (state.selectedKind !== "alerts") return;
    const list = $("message-list");
    clearMessageList(list);
    const children = [];
    children.push(renderDoctorDriftPanel());
    if (state.alerts.loading) {
      children.push(el("div", {
        class: "activity-empty",
        text: "loading alerts...",
      }));
    } else if (state.alerts.error) {
      const retry = el("button", {
        type: "button",
        class: "fetch-retry",
        text: "Retry",
        "aria-label": "Retry loading alerts",
      });
      retry.addEventListener("click", () => loadAlerts());
      children.push(el("div", { class: "activity-empty" }, [
        document.createTextNode("alerts unavailable: " + state.alerts.error.message),
        retry,
      ]));
    } else if (state.alerts.items.length === 0) {
      children.push(el("div", {
        class: "activity-empty",
        text: "no action-required alerts",
      }));
    } else {
      for (const item of state.alerts.items) {
        children.push(renderAlertRow(item));
      }
    }
    list.appendChild(el("div", { class: "alerts-panel" }, children));
  }

  async function loadInbox(reset, omitType, preserveSelection) {
    if (state.inbox.loading || state.inbox.loadingMore) {
      state.inbox.reloadQueued = true;
      return;
    }
    const cursor = reset ? null : state.inbox.nextCursor;
    if (!reset && !cursor) return;
    const selectedId = preserveSelection ? state.inbox.selectedId : null;
    if (reset) {
      state.inbox.loading = true;
      state.inbox.items = [];
      state.inbox.nextCursor = null;
      if (!preserveSelection) {
        state.inbox.selectedId = null;
        state.inbox.detail = null;
        state.inbox.detailError = null;
      }
    } else {
      state.inbox.loadingMore = true;
    }
    state.inbox.error = null;
    state.inbox.warning = null;
    renderInboxView();
    try {
      const data = await apiJson(inboxListPath(cursor, Boolean(omitType)));
      const items = Array.isArray(data.items) ? data.items : [];
      state.inbox.items = reset ? items : state.inbox.items.concat(items);
      state.inbox.nextCursor = data.next_cursor || null;
      if (selectedId) state.inbox.selectedId = selectedId;
      if (omitType && state.inbox.filters.type) {
        state.inbox.warning = "Type filtering is unavailable on this API; showing matching state/project items.";
      }
    } catch (err) {
      if (
        !omitType
        && state.inbox.filters.type
        && (err.status === 400 || err.status === 404 || err.status === 422)
      ) {
        state.inbox.loading = false;
        state.inbox.loadingMore = false;
        await loadInbox(reset, true, preserveSelection);
        return;
      }
      state.inbox.error = err;
    } finally {
      state.inbox.loading = false;
      state.inbox.loadingMore = false;
      const reloadQueued = state.inbox.reloadQueued;
      state.inbox.reloadQueued = false;
      if (state.selectedKind === "inbox") renderInboxView();
      if (reloadQueued && state.selectedKind === "inbox") {
        loadInbox(true, false, preserveSelection);
      }
    }
  }

  function inboxMeta(item) {
    return [
      item.project,
      item.type,
      item.state,
      item.owner ? "@" + item.owner : "",
      item.updated_at,
    ].filter(Boolean).join(" · ");
  }

  function setInboxAction(key, active) {
    if (active) state.inbox.actionInFlight[key] = true;
    else delete state.inbox.actionInFlight[key];
    renderInboxView();
  }

  function inboxActionButton(item, action, label, handler, extraClass) {
    const key = item.id + ":" + action;
    const btn = el("button", {
      class: ("inbox-action " + (extraClass || "")).trim(),
      type: "button",
      text: label,
    });
    btn.disabled = Boolean(state.inbox.actionInFlight[key]);
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      handler(item);
    });
    return btn;
  }

  function taskRefFromInboxItem(item) {
    const metadata = item && item.metadata && typeof item.metadata === "object"
      ? item.metadata : {};
    return parseTaskKey(item.task_id || metadata.task_id || item.id || "");
  }

  function isBlockedCancelledHandoff(item) {
    const metadata = item && item.metadata && typeof item.metadata === "object"
      ? item.metadata : {};
    return Boolean(metadata.blocked_cancelled_handoff);
  }

  function disabledInboxAction(label, title) {
    const btn = el("button", {
      class: "inbox-action disabled",
      type: "button",
      text: label,
      title: title,
    });
    btn.disabled = true;
    return btn;
  }

  async function runPlanReviewDecision(item, decision) {
    const ref = taskRefFromInboxItem(item);
    if (!ref) {
      showToast("error", "plan task id missing");
      return;
    }
    const key = item.id + ":plan-" + decision;
    const verb = decision === "approve" ? "approve" : "rework";
    const path = API + "/tasks/" + encodeURIComponent(ref.project)
      + "/" + encodeURIComponent(ref.task_number) + "/" + verb;
    const body = { actor: OPERATOR_ACTOR };
    if (decision === "approve") {
      body.reason = "approved from web UI";
    } else {
      body.reason = "rejected from web UI";
    }
    setInboxAction(key, true);
    try {
      await apiJson(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      showToast(
        "ok",
        decision === "approve" ? "plan approved" : "plan rejected",
      );
      state.inbox.selectedId = null;
      state.inbox.detail = null;
      state.inbox.replyDraft = "";
      await loadInbox(true);
      pollDashboard();
      loadSurfaces();
    } catch (err) {
      state.inbox.error = err;
      showToast("error", decision + " failed: " + err.message);
      renderInboxView();
    } finally {
      setInboxAction(key, false);
    }
  }

  function planReviewButton(item, decision, label, extraClass) {
    if (!taskRefFromInboxItem(item)) {
      return disabledInboxAction(label, "Plan task id is missing.");
    }
    return inboxActionButton(
      item,
      "plan-" + decision,
      label,
      () => runPlanReviewDecision(item, decision),
      extraClass,
    );
  }

  function renderInboxFilters() {
    const project = el("input", {
      class: "inbox-filter-input",
      type: "text",
      placeholder: "Project",
      "aria-label": "Filter inbox by project",
      value: state.inbox.filters.project,
    });
    const stateSelect = el("select", {
      class: "inbox-filter-select",
      "aria-label": "Filter inbox by state",
    });
    const states = [
      ["", "All states"],
      ["open", "Open"],
      ["threaded", "Threaded"],
      ["waiting-on-pa", "Waiting on PA"],
      ["waiting-on-pm", "Waiting on PM"],
      ["resolved", "Resolved"],
      ["closed", "Closed"],
    ];
    for (const pair of states) {
      const option = el("option", { value: pair[0], text: pair[1] });
      if (pair[0] === state.inbox.filters.state) option.selected = true;
      stateSelect.appendChild(option);
    }
    const typeSelect = el("select", {
      class: "inbox-filter-select",
      "aria-label": "Filter inbox by type",
    });
    const types = [
      ["", "All types"],
      ["message", "Messages"],
      ["plan_review", "Plan reviews"],
    ];
    for (const pair of types) {
      const option = el("option", { value: pair[0], text: pair[1] });
      if (pair[0] === state.inbox.filters.type) option.selected = true;
      typeSelect.appendChild(option);
    }
    project.addEventListener("input", () => {
      state.inbox.filters.project = project.value || "";
    });
    stateSelect.addEventListener("change", () => {
      state.inbox.filters.state = stateSelect.value || "";
    });
    typeSelect.addEventListener("change", () => {
      state.inbox.filters.type = typeSelect.value || "";
    });
    const apply = el("button", {
      class: "inbox-filter-button primary",
      type: "submit",
      text: "Apply",
    });
    const clear = el("button", {
      class: "inbox-filter-button",
      type: "button",
      text: "Clear",
    });
    clear.addEventListener("click", () => {
      state.inbox.filters = { project: "", state: "", type: "" };
      loadInbox(true);
    });
    const refresh = el("button", {
      class: "inbox-filter-button",
      type: "button",
      text: "Refresh",
    });
    refresh.addEventListener("click", () => loadInbox(true));
    const form = el("form", { class: "inbox-filters" }, [
      project,
      stateSelect,
      typeSelect,
      apply,
      clear,
      refresh,
    ]);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      state.inbox.filters = {
        project: project.value || "",
        state: stateSelect.value || "",
        type: typeSelect.value || "",
      };
      loadInbox(true);
    });
    return form;
  }

  function renderInboxList() {
    const children = [];
    if (state.inbox.warning) {
      children.push(el("div", {
        class: "inbox-warning",
        text: state.inbox.warning,
      }));
    }
    if (state.inbox.error) {
      children.push(el("div", {
        class: "error-banner inbox-error",
        text: "inbox error: " + state.inbox.error.message,
      }));
    }
    if (state.inbox.loading) {
      children.push(el("div", {
        class: "message-empty",
        text: "loading inbox...",
      }));
      return el("div", { class: "inbox-list" }, children);
    }
    if (state.inbox.items.length === 0 && !state.inbox.error) {
      children.push(el("div", {
        class: "message-empty",
        text: "no inbox items",
      }));
      return el("div", { class: "inbox-list" }, children);
    }
    for (const item of state.inbox.items) {
      const rowActions = [
        inboxActionButton(item, "mark-read", "Mark read", markInboxRead),
        inboxActionButton(item, "snooze", "Snooze 1h", snoozeInboxItem),
        inboxActionButton(item, "archive", "Archive", archiveInboxItem, "danger"),
      ];
      if (item.type === "plan_review") {
        rowActions.push(planReviewButton(item, "approve", "Approve"));
        rowActions.push(planReviewButton(item, "reject", "Reject", "danger"));
      }
      const row = el("div", {
        class: (
          "inbox-row "
          + (state.inbox.selectedId === item.id ? "active" : "")
        ).trim(),
        "data-inbox-id": item.id,
      }, [
        el("div", { class: "inbox-row-main" }, [
          el("div", { class: "inbox-subject", text: item.subject || item.id }),
          el("div", { class: "inbox-meta", text: inboxMeta(item) }),
          el("div", { class: "inbox-preview", text: item.preview || "" }),
        ]),
        el("div", { class: "inbox-row-actions" }, rowActions),
      ]);
      row.addEventListener("click", () => selectInboxItem(item.id));
      children.push(row);
    }
    if (state.inbox.nextCursor) {
      const more = el("button", {
        class: "inbox-load-more",
        type: "button",
        text: state.inbox.loadingMore ? "Loading..." : "Load more",
      });
      more.disabled = state.inbox.loadingMore;
      more.addEventListener("click", () => loadInbox(false));
      children.push(more);
    }
    return el("div", { class: "inbox-list" }, children);
  }

  function renderInboxDetail() {
    if (!state.inbox.selectedId) {
      return el("div", {
        class: "inbox-detail inbox-detail-empty",
        text: "Select an inbox item to read the thread.",
      });
    }
    if (state.inbox.detailLoading) {
      return el("div", {
        class: "inbox-detail inbox-detail-empty",
        text: "loading thread...",
      });
    }
    if (state.inbox.detailError) {
      return el("div", {
        class: "error-banner inbox-error",
        text: "thread error: " + state.inbox.detailError.message,
      });
    }
    const detail = state.inbox.detail;
    if (!detail) {
      return el("div", {
        class: "inbox-detail inbox-detail-empty",
        text: "No thread loaded.",
      });
    }
    const messages = Array.isArray(detail.messages) ? detail.messages : [];
    const messageNodes = messages.length === 0
      ? [el("div", { class: "inbox-thread-empty", text: "no replies yet" })]
      : messages.map((msg) => el("div", { class: "inbox-thread-message" }, [
        el("div", { class: "inbox-thread-head" }, [
          el("span", { text: msg.sender || "operator" }),
          el("span", { text: msg.timestamp || "" }),
        ]),
        el("div", { class: "inbox-thread-body", text: msg.body || "" }),
      ]));
    const resolvesCancelledHandoff = isBlockedCancelledHandoff(detail);
    const reply = el("textarea", {
      class: "inbox-reply-input",
      rows: "3",
      placeholder: resolvesCancelledHandoff
        ? "Answer the handoff inputs..."
        : "Reply...",
      "aria-label": "Reply to inbox item",
    });
    reply.value = state.inbox.replyDraft || "";
    reply.addEventListener("input", () => {
      state.inbox.replyDraft = reply.value;
    });
    const send = el("button", {
      class: "inbox-filter-button primary",
      type: "submit",
      text: resolvesCancelledHandoff ? "Answer and unblock" : "Reply",
    });
    const actionKey = detail.id + ":reply";
    send.disabled = Boolean(state.inbox.actionInFlight[actionKey]);
    const form = el("form", { class: "inbox-reply-form" }, [reply, send]);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      replyInboxItem(detail, reply.value || "", {
        clearSelection: resolvesCancelledHandoff,
      });
    });
    const decisionActions = [];
    if (detail.type === "plan_review") {
      decisionActions.push(planReviewButton(detail, "approve", "Approve"));
      decisionActions.push(planReviewButton(detail, "reject", "Reject", "danger"));
    }
    return el("div", { class: "inbox-detail" }, [
      el("div", { class: "inbox-detail-title", text: detail.subject || detail.id }),
      el("div", { class: "inbox-meta", text: inboxMeta(detail) }),
      detail.preview
        ? el("div", { class: "inbox-detail-preview", text: detail.preview })
        : null,
      decisionActions.length
        ? el("div", { class: "inbox-decision-actions" }, decisionActions)
        : null,
      el("div", { class: "inbox-thread" }, messageNodes),
      form,
    ]);
  }

  function renderInboxView() {
    if (state.selectedKind !== "inbox") return;
    $("pane-title").textContent = "Inbox";
    $("pane-meta").textContent = (
      state.inbox.items.length + " shown"
      + (state.inbox.nextCursor ? " · more available" : "")
    );
    const list = $("message-list");
    clearMessageList(list);
    list.appendChild(el("div", { class: "inbox-view" }, [
      renderInboxFilters(),
      el("div", { class: "inbox-content" }, [
        renderInboxList(),
        renderInboxDetail(),
      ]),
    ]));
  }

  async function selectInboxItem(id) {
    state.inbox.selectedId = id;
    state.inbox.detail = null;
    state.inbox.detailError = null;
    state.inbox.replyDraft = "";
    await loadInboxDetail(id);
  }

  async function loadInboxDetail(id) {
    if (!id) return;
    state.inbox.detailLoading = true;
    state.inbox.detailError = null;
    renderInboxView();
    try {
      const data = await apiJson(API + "/inbox/" + encodePathId(id));
      if (state.selectedKind === "inbox" && state.inbox.selectedId === id) {
        state.inbox.detail = data;
      }
    } catch (err) {
      if (state.selectedKind === "inbox" && state.inbox.selectedId === id) {
        state.inbox.detailError = err;
      }
    } finally {
      state.inbox.detailLoading = false;
      renderInboxView();
    }
  }

  async function runInboxAction(item, action, suffix, body, options) {
    const key = item.id + ":" + action;
    setInboxAction(key, true);
    try {
      await apiJson(API + "/inbox/" + encodePathId(item.id) + suffix, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (options && options.clearSelection) {
        state.inbox.selectedId = null;
        state.inbox.detail = null;
        state.inbox.replyDraft = "";
      }
      await loadInbox(true, false, !(options && options.clearSelection));
      if (state.inbox.selectedId) await loadInboxDetail(state.inbox.selectedId);
      return true;
    } catch (err) {
      state.inbox.error = err;
      showToast("error", action + " failed: " + err.message);
      return false;
    } finally {
      setInboxAction(key, false);
    }
  }

  function markInboxRead(item) {
    runInboxAction(
      item,
      "mark-read",
      "/mark-read",
      { actor: OPERATOR_ACTOR },
      {},
    );
  }

  function archiveInboxItem(item) {
    runInboxAction(
      item,
      "archive",
      "/archive",
      { reason: "archived from web UI" },
      { clearSelection: true },
    );
  }

  function snoozeInboxItem(item) {
    runInboxAction(
      item,
      "snooze",
      "/snooze",
      { duration_seconds: 3600, reason: "snoozed from web UI" },
      { clearSelection: true },
    );
  }

  async function replyInboxItem(item, body, options) {
    const text = String(body || "").trim();
    if (!text) return;
    const clearSelection = Boolean(options && options.clearSelection);
    const ok = await runInboxAction(
      item,
      "reply",
      "/reply",
      { body: text, owner: OPERATOR_ACTOR },
      { clearSelection },
    );
    if (ok) {
      state.inbox.replyDraft = "";
      showToast(
        "ok",
        clearSelection ? "answer recorded; dependency cleared" : "reply sent",
      );
      renderInboxView();
    }
  }

  // ----- dashboard rollups (right rail) ----------------------------------

  async function pollDashboard(opts) {
    const force = opts && opts.force;
    if (state.dashboardInFlight) {
      if (force) {
        abortFetchState("dashboard");
        state.dashboardInFlight = false;
      } else {
        state.dashboardRefreshQueued = true;
        return;
      }
    }
    state.dashboardInFlight = true;
    const request = beginFetchState(
      "dashboard",
      "dashboard",
      renderDashboardLoading,
      () => pollDashboard({ force: true }),
      { preserveSettled: true },
    );
    try {
      const params = new URLSearchParams();
      if (state.selectedProject) params.set("project", state.selectedProject);
      const includeBriefing = !state.dashboardBriefingRequested;
      if (includeBriefing) params.set("include_briefing", "true");
      const query = params.toString();
      const data = await apiJson(
        API + "/dashboard" + (query ? "?" + query : ""),
        { signal: request.signal },
      );
      state.dashboardData = data;
      if (includeBriefing) state.dashboardBriefingRequested = true;
      renderDashboard(data);
      if (state.selectedKind === null) renderNoSelection();
    } catch (err) {
      if (isAbortError(err)) return;
      renderDashboardError(err);
    } finally {
      if (!finishFetchState("dashboard", request.seq)) return;
      state.dashboardInFlight = false;
      if (state.dashboardRefreshQueued) {
        state.dashboardRefreshQueued = false;
        pollDashboard();
      }
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

  function renderDashboardLoading() {
    const box = $("dashboard-rollups");
    if (!box) return;
    box.innerHTML = "";
    box.appendChild(
      fetchAffordance("dashboard", "div", "rollup-empty", "loading..."),
    );
  }

  function renderDashboardDrilldown(card) {
    $("pane-title").textContent = "Dashboard: " + card.label;
    $("pane-meta").textContent = card.value;
    $("send-input").disabled = true;
    $("send-button").disabled = true;
    updateStopAgentButton();
    const list = $("message-list");
    clearMessageList(list);
    const rows = [
      ["Metric", card.label],
      ["Value", card.value],
    ];
    if (card.detail) rows.push(["Context", card.detail]);
    const children = [];
    for (const row of rows) {
      children.push(el("div", { class: "task-detail-row" }, [
        el("span", { class: "task-detail-label", text: row[0] }),
        el("span", { class: "task-detail-value", text: String(row[1]) }),
      ]));
    }
    list.appendChild(el("div", { class: "task-detail dashboard-detail" }, children));
  }

  function firstSurfaceName(data) {
    if (data && Array.isArray(data.active_sessions)) {
      for (const item of data.active_sessions) {
        const name = item && item.session_name;
        if (name && state.surfaces.some((s) => s.session_name === name)) {
          return name;
        }
      }
      const firstActive = data.active_sessions.find((item) => item.session_name);
      if (firstActive) return firstActive.session_name;
    }
    return state.surfaces.length > 0 ? state.surfaces[0].session_name : null;
  }

  function firstTaskKey(statuses) {
    for (const task of state.taskSurfaces) {
      if (!statuses || statuses.indexOf(task.work_status) !== -1) return task.key;
    }
    return null;
  }

  function dashboardCardAction(card, data) {
    return () => {
      if (card.target === "surface") {
        const name = firstSurfaceName(data);
        if (name && state.surfaces.some((s) => s.session_name === name)) {
          selectSurface(name);
          return;
        }
      }
      if (card.target === "review-task") {
        const key = firstTaskKey(["review", "queued", "rework", "blocked"]);
        if (key) {
          selectTask(key);
          return;
        }
      }
      if (card.target === "task") {
        const key = firstTaskKey(null);
        if (key) {
          selectTask(key);
          return;
        }
      }
      if (card.target === "inbox") {
        selectInbox(card.filter || undefined);
        return;
      }
      if (card.target === "alerts") {
        selectAlerts();
        return;
      }
      renderDashboardDrilldown(card);
    };
  }

  // Dashboard rail cards always render as buttons with a Playwright-friendly
  // aria-label and route through `dashboardCardAction` for drill-down.
  function buildCard(label, value, cls, scoped, action, detail) {
    const labelText = scoped ? label + " (filtered)" : label;
    const button = el("button", {
      type: "button",
      role: "button",
      class: "rollup-card " + (cls || ""),
      "aria-label": "Open dashboard detail for " + labelText + ": " + value,
    }, [
      el("div", { class: "rollup-label", text: labelText }),
      el("div", { class: "rollup-value", text: String(value) }),
    ]);
    button.addEventListener("click", action);
    if (detail) button.setAttribute("title", detail);
    return button;
  }

  function buildDashboardHeadline(data) {
    const status = dashboardStatus(data);
    const button = el("button", {
      type: "button",
      class: "dashboard-headline",
      "aria-label": status.actionLabel + ": " + status.title,
    }, [
      el("div", { class: "dashboard-headline-title", text: status.title }),
      el("div", { class: "dashboard-headline-detail", text: status.detail }),
    ]);
    button.addEventListener("click", () => runDashboardStatusAction(status));
    return button;
  }

  function quotaSeverityClass(severity) {
    const value = String(severity || "").toLowerCase();
    if (value === "critical" || value === "error") return "quota-critical";
    if (value === "warn" || value === "warning") return "quota-warn";
    return "quota-ok";
  }

  function clampedPercent(value) {
    const pct = numeric(value);
    if (pct < 0) return 0;
    if (pct > 100) return 100;
    return pct;
  }

  function buildQuotaCard(data) {
    const usages = data && Array.isArray(data.account_usages)
      ? data.account_usages : [];
    const tokens = data && data.tokens && typeof data.tokens === "object"
      ? data.tokens : {};
    if (usages.length === 0 && tokens.today == null && tokens.total == null) {
      return null;
    }
    const primary = usages[0] || {};
    const pct = clampedPercent(primary.used_pct);
    const children = [
      el("div", { class: "quota-label", text: "Claude headroom" }),
    ];
    if (primary.summary) {
      children.push(el("div", {
        class: "quota-summary",
        text: primary.summary,
      }));
    } else {
      children.push(el("div", {
        class: "quota-summary",
        text: "Usage details unavailable.",
      }));
    }
    const fill = el("div", { class: "quota-fill" });
    fill.setAttribute("style", "width: " + pct + "%;");
    children.push(el("div", { class: "quota-bar" }, [fill]));
    if (usages.length > 1) {
      const backup = usages[1];
      children.push(el("div", {
        class: "quota-secondary",
        text: "+ backup: " + (backup.summary || backup.account_name || "available"),
      }));
    }
    if (tokens.today != null || tokens.total != null) {
      children.push(el("div", {
        class: "quota-tokens",
        text: String(tokens.today || 0) + " today / "
          + String(tokens.total || 0) + " total tokens",
      }));
    }
    return el("div", {
      class: "quota-card " + quotaSeverityClass(primary.severity),
    }, children);
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

    box.appendChild(buildDashboardHeadline(data));
    const quotaCard = buildQuotaCard(data);
    if (quotaCard) box.appendChild(quotaCard);

    // --- counters from rollups ------------------------------------------
    if (typeof rollups.open_inbox_count === "number") {
      cards.push(buildCard(
        "inbox",
        rollups.open_inbox_count + " items",
        "rollup-attention",
        isScoped(scopedFields, "rollups.open_inbox_count"),
        dashboardCardAction({
          label: "inbox",
          value: rollups.open_inbox_count + " items",
          target: "inbox",
          detail: "Opens the inbox view.",
        }, data),
        "Open inbox",
      ));
    }
    if (typeof rollups.pending_plan_reviews === "number") {
      cards.push(buildCard(
        "plan reviews",
        rollups.pending_plan_reviews + " waiting",
        "rollup-attention",
        isScoped(scopedFields, "rollups.pending_plan_reviews"),
        dashboardCardAction({
          label: "plan reviews",
          value: rollups.pending_plan_reviews + " waiting",
          target: "inbox",
          filter: { type: "plan_review" },
          detail: "Opens inbox items waiting on plan review.",
        }, data),
        "Open plan reviews",
      ));
    }
    if (typeof rollups.alert_count === "number") {
      const scoped = isScoped(scopedFields, "rollups.alert_count");
      cards.push(buildCard(
        "watching",
        rollups.alert_count,
        rollups.alert_count > 0 ? "rollup-working" : "",
        scoped,
        dashboardCardAction({
          label: "watching",
          value: rollups.alert_count,
          target: "alerts",
          detail: scoped
            ? "Opens alerts for the selected project."
            : "Opens background alerts and recovery notes.",
        }, data),
        scoped ? "Open project alerts" : "Open background alerts",
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
        dashboardCardAction({
          label: "activity (24h)",
          value: sweeps + " sweeps / " + msgs + " msgs",
          target: "surface",
          detail: "Opens an active chat surface when one is visible.",
        }, data),
        "Open an active chat surface",
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
        dashboardCardAction({
          label: "daemon",
          value: data.daemon_status,
          target: "detail",
          detail: "Shows daemon status.",
        }, data),
        "Show daemon status",
      ));
    }

    // --- active sessions (list length) ----------------------------------
    if (Array.isArray(data.active_sessions)) {
      cards.push(buildCard(
        "active sessions",
        data.active_sessions.length,
        "",
        isScoped(scopedFields, "active_sessions"),
        dashboardCardAction({
          label: "active sessions",
          value: data.active_sessions.length,
          target: "surface",
          detail: "Opens the first visible active chat surface.",
        }, data),
        "Open active session",
      ));
    }

    // --- tracked projects ----------------------------------------------
    if (typeof rollups.tracked_count === "number") {
      cards.push(buildCard(
        "projects tracked",
        rollups.tracked_count,
        "",
        isScoped(scopedFields, "rollups.tracked_count"),
        dashboardCardAction({
          label: "projects tracked",
          value: rollups.tracked_count,
          target: "task",
          detail: "Opens a visible task when available.",
        }, data),
        "Open a visible task",
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

  // ----- activity feed (right rail) --------------------------------------

  function activityParams() {
    const params = new URLSearchParams();
    params.set("since", state.activitySince || ACTIVITY_DEFAULT_SINCE);
    if (state.selectedProject) params.set("project", state.selectedProject);
    return params;
  }

  function activityFeedPath() {
    const params = activityParams();
    params.set("limit", String(ACTIVITY_LIMIT));
    return API + "/activity?" + params.toString();
  }

  function activityStatsPath() {
    const params = activityParams();
    params.set("deadline_seconds", String(ACTIVITY_STATS_DEADLINE_SECONDS));
    return API + "/audit/stats?" + params.toString();
  }

  async function loadActivity() {
    if (state.activityInFlight) {
      state.activityRefreshQueued = true;
      return;
    }
    state.activityInFlight = true;
    state.activityLoading = true;
    state.activityError = null;
    state.activityStatsNote = null;
    renderActivity();
    try {
      const [grepResult, statsResult] = await Promise.allSettled([
        apiJsonOptionalWithTimeout(
          "activity feed", activityFeedPath(), ACTIVITY_REQUEST_TIMEOUT_MS,
        ),
        apiJsonOptionalWithTimeout(
          "activity stats", activityStatsPath(), ACTIVITY_STATS_REQUEST_TIMEOUT_MS,
        ),
      ]);
      if (grepResult.status === "fulfilled") {
        state.activityEntries = Array.isArray(grepResult.value.events)
          ? grepResult.value.events : [];
      } else {
        state.activityEntries = [];
        state.activityError = grepResult.reason instanceof Error
          ? grepResult.reason
          : new Error(String(grepResult.reason));
      }
      state.activityStats = statsResult.status === "fulfilled"
        ? statsResult.value : null;
      if (statsResult.status === "rejected" && !state.activityError) {
        const err = statsResult.reason instanceof Error
          ? statsResult.reason
          : new Error(String(statsResult.reason));
        state.activityStatsNote = "stats unavailable: " + err.message;
      } else if (
        statsResult.status === "fulfilled"
        && statsResult.value
        && statsResult.value._truncated_by_deadline
      ) {
        const lines = Number(statsResult.value._lines_scanned || 0);
        state.activityStatsNote = (
          "stats partial"
          + (lines > 0 ? " after scanning " + lines + " lines" : "")
        );
      }
    } finally {
      state.activityLoading = false;
      state.activityInFlight = false;
      renderActivity();
      if (state.activityRefreshQueued) {
        state.activityRefreshQueued = false;
        loadActivity();
      }
    }
  }

  function eventMetadata(entry) {
    return entry && typeof entry.metadata === "object" && entry.metadata
      ? entry.metadata : {};
  }

  function activityCompletedCount(entries) {
    return entries.filter((entry) => {
      const eventName = String(entry.event || "").toLowerCase();
      const meta = eventMetadata(entry);
      const stateName = String(
        meta.to_state || meta.work_status || meta.status || "",
      ).toLowerCase();
      return (
        stateName === "done"
        || eventName.includes("completed")
        || eventName.includes(".done")
      );
    }).length;
  }

  function activityDecisionCount(entries) {
    return entries.filter((entry) => {
      const eventName = String(entry.event || "").toLowerCase();
      return (
        eventName.includes("decision")
        || eventName.includes("approve")
        || eventName.includes("reject")
        || eventName.includes("review")
        || eventName.includes("plan.")
      );
    }).length;
  }

  function activityWarningCount(entries) {
    return entries.filter((entry) => {
      const eventName = String(entry.event || "").toLowerCase();
      const status = String(entry.status || "").toLowerCase();
      return (
        status === "warn"
        || status === "error"
        || eventName.includes("blocked")
        || eventName.includes("hold")
        || eventName.includes("failed")
      );
    }).length;
  }

  function renderActivitySummary(summaryBox, entries) {
    summaryBox.innerHTML = "";
    const stats = state.activityStats || {};
    const total = typeof stats.total === "number" ? stats.total : entries.length;
    const actors = new Set(entries.map((entry) => entry.actor).filter(Boolean));
    const cards = [
      ["events", total],
      ["done", activityCompletedCount(entries)],
      ["warnings", activityWarningCount(entries)],
      ["decisions", activityDecisionCount(entries)],
      ["actors", actors.size],
    ];
    for (const card of cards) {
      summaryBox.appendChild(el("div", { class: "activity-chip" }, [
        el("span", { class: "activity-chip-label", text: card[0] }),
        el("span", { class: "activity-chip-value", text: String(card[1]) }),
      ]));
    }
    if (state.activityStatsNote) {
      summaryBox.appendChild(el("div", {
        class: "activity-note",
        text: state.activityStatsNote,
      }));
    }
  }

  function renderActivity() {
    const summaryBox = $("activity-summary");
    const feed = $("activity-feed");
    if (!summaryBox || !feed) return;
    if (state.activityLoading) {
      summaryBox.innerHTML = "";
      feed.innerHTML = "";
      feed.appendChild(el("div", {
        class: "activity-empty",
        text: "loading activity...",
      }));
      return;
    }
    if (state.activityError) {
      summaryBox.innerHTML = "";
      feed.innerHTML = "";
      const retry = el("button", {
        type: "button",
        class: "fetch-retry",
        text: "Retry",
        "aria-label": "Retry loading activity",
      });
      retry.addEventListener("click", () => loadActivity());
      feed.appendChild(el("div", { class: "activity-empty" }, [
        document.createTextNode(
          "activity unavailable: " + state.activityError.message,
        ),
        retry,
      ]));
      return;
    }
    const entries = state.activityEntries.slice().sort((a, b) => (
      (Date.parse(b.ts || "") || 0) - (Date.parse(a.ts || "") || 0)
    ));
    renderActivitySummary(summaryBox, entries);
    feed.innerHTML = "";
    if (entries.length === 0) {
      feed.appendChild(el("div", {
        class: "activity-empty",
        text: "no activity in " + (state.activitySince || ACTIVITY_DEFAULT_SINCE),
      }));
      return;
    }
    for (const entry of entries.slice(0, ACTIVITY_LIMIT)) {
      feed.appendChild(el("div", { class: "activity-entry" }, [
        el("div", { class: "activity-entry-head" }, [
          el("span", {
            class: "activity-entry-project",
            text: entry.project || "workspace",
          }),
          el("span", {
            class: "activity-entry-ts",
            text: formatShortTime(entry.ts),
          }),
        ]),
        el("div", {
          class: "activity-entry-line",
          text: auditSummary(entry),
        }),
      ]));
    }
  }

  // ----- push refresh / fallback polling ---------------------------------

  function refreshFromPush() {
    pollDashboard();
    loadSurfaces({ preserveErrors: true });
    loadActivity();
    if (state.selectedSurface) {
      loadHistory(state.selectedSurface);
      if (state.auditExpanded[state.selectedSurface]) {
        loadAuditForSurface(state.selectedSurface);
      }
    }
    if (state.selectedKind === "inbox") {
      loadInbox(true, false, true);
    }
  }

  function refreshFromFallback() {
    pollDashboard();
    loadActivity();
    if (state.selectedSurface) loadHistory(state.selectedSurface);
    if (state.selectedKind === "inbox") loadInbox(true, false, true);
  }

  function requestPushRefresh() {
    if (state.pushRefreshTimer !== null) return;
    state.pushRefreshTimer = setTimeout(() => {
      state.pushRefreshTimer = null;
      refreshFromPush();
    }, PUSH_REFRESH_DEBOUNCE_MS);
  }

  function startFallbackPolling() {
    if (state.fallbackTimer !== null) return;
    refreshFromFallback();
    state.fallbackTimer = setInterval(refreshFromFallback, FALLBACK_POLL_MS);
  }

  function stopFallbackPolling() {
    if (state.fallbackTimer === null) return;
    clearInterval(state.fallbackTimer);
    state.fallbackTimer = null;
  }

  function clearSseStableOpenTimer() {
    if (state.sseStableOpenTimer === null) return;
    clearTimeout(state.sseStableOpenTimer);
    state.sseStableOpenTimer = null;
  }

  function eventSourceIsOpen(source) {
    if (!source || typeof source.readyState !== "number") return true;
    const openState = (
      typeof window.EventSource === "function"
      && typeof window.EventSource.OPEN === "number"
    ) ? window.EventSource.OPEN : 1;
    return source.readyState === openState;
  }

  function suspendFallbackAfterStableOpen(source) {
    clearSseStableOpenTimer();
    state.sseStableOpenTimer = setTimeout(() => {
      state.sseStableOpenTimer = null;
      if (state.eventSource !== source) return;
      if (!eventSourceIsOpen(source)) {
        startFallbackPolling();
        return;
      }
      stopFallbackPolling();
    }, SSE_STABLE_OPEN_MS);
  }

  function resumeFallbackForUnhealthyStream() {
    clearSseStableOpenTimer();
    startFallbackPolling();
  }

  function closeEventStream(options) {
    const resumeFallback = !options || options.resumeFallback !== false;
    clearSseStableOpenTimer();
    if (!state.eventSource) {
      if (resumeFallback) startFallbackPolling();
      return;
    }
    state.eventSource.close();
    state.eventSource = null;
    if (resumeFallback) startFallbackPolling();
  }

  function eventStreamUrl() {
    if (!state.lastEventId) return API + "/events";
    return API + "/events?since=" + encodeURIComponent(state.lastEventId);
  }

  function handleSseEvent(ev) {
    state.sseFailures = 0;
    if (ev && ev.lastEventId) {
      state.lastEventId = ev.lastEventId;
    } else if (ev && ev.data) {
      try {
        const data = JSON.parse(ev.data);
        if (data && data.ts) state.lastEventId = data.ts;
      } catch (e) { /* ignore malformed event payloads */ }
    }
    requestPushRefresh();
  }

  function scheduleEventStreamRetry() {
    if (state.sseRetryTimer !== null) return;
    state.sseRetryTimer = setTimeout(() => {
      state.sseRetryTimer = null;
      startEventStream();
    }, SSE_RETRY_MS);
  }

  function startEventStream() {
    if (!("EventSource" in window)) {
      startFallbackPolling();
      return;
    }

    if (state.sseRetryTimer !== null) {
      clearTimeout(state.sseRetryTimer);
      state.sseRetryTimer = null;
    }
    closeEventStream({ resumeFallback: false });
    startFallbackPolling();

    let source;
    try {
      source = new window.EventSource(eventStreamUrl());
    } catch (err) {
      setStatus("warn", "SSE unavailable");
      scheduleEventStreamRetry();
      return;
    }
    state.eventSource = source;
    source.onopen = () => {
      if (state.eventSource !== source) return;
      state.sseFailures = 0;
      suspendFallbackAfterStableOpen(source);
      setStatus("ok", "online");
    };
    source.addEventListener("audit", handleSseEvent);
    source.onmessage = handleSseEvent;
    source.onerror = () => {
      if (state.eventSource !== source) return;
      state.sseFailures += 1;
      resumeFallbackForUnhealthyStream();
      setStatus("warn", "SSE reconnecting");
      if (state.sseFailures >= SSE_FAILURE_LIMIT) {
        closeEventStream();
        scheduleEventStreamRetry();
      }
    };
  }

  // ----- wire-up ----------------------------------------------------------

  function wireSendForm() {
    const form = $("send-form");
    const input = $("send-input");
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const text = input.value.trim();
      if (!text || state.selectedKind !== "chat" || !state.selectedSurface) {
        return;
      }
      input.value = "";
      sendMessage(state.selectedSurface, text).catch(() => {});
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
    if (input) {
      input.addEventListener("input", () => {
        state.surfaceFilter = input.value || "";
        renderSurfaces();
      });
    }
    const status = $("task-status-filter");
    if (status) {
      status.value = state.taskStatusFilter;
      status.addEventListener("change", () => {
        state.taskStatusFilter = status.value || "";
        loadSurfaces();
      });
    }
  }

  function wireProjectControls() {
    const filter = $("project-filter");
    if (filter) {
      filter.addEventListener("input", () => {
        state.projectFilter = filter.value || "";
        renderProjects();
        loadSurfaces();
      });
    }
    const sort = $("project-sort");
    if (sort) {
      sort.value = state.projectSort;
      sort.addEventListener("change", () => {
        state.projectSort = sort.value || "urgency";
        renderProjects();
        loadSurfaces();
      });
    }
    const showTests = $("project-show-tests");
    if (showTests) {
      showTests.checked = state.showTestProjects;
      showTests.addEventListener("change", () => {
        state.showTestProjects = Boolean(showTests.checked);
        loadSurfaces();
        pollDashboard();
      });
    }
  }

  function wireActivityControls() {
    const since = $("activity-since");
    if (since) {
      since.value = state.activitySince;
      since.addEventListener("change", () => {
        state.activitySince = since.value || ACTIVITY_DEFAULT_SINCE;
        loadActivity();
      });
    }
    const refresh = $("activity-refresh");
    if (refresh) {
      refresh.addEventListener("click", () => loadActivity());
    }
  }

  function initialUiRoute() {
    const path = window.location && window.location.pathname
      ? window.location.pathname.replace(/\/+$/, "")
      : "";
    if (path === "/ui/inbox") return "inbox";
    if (path === "/ui/alerts") return "alerts";
    return null;
  }

  function init() {
    maybeShowSessionExpiryBanner();
    wireProjectControls();
    wireSurfaceFilter();
    wireActivityControls();
    wireSendForm();
    wireStopAgentButton();
    const route = initialUiRoute();
    if (route === "inbox") {
      selectInbox();
    } else if (route === "alerts") {
      selectAlerts();
    } else {
      renderNoSelection();
    }
    setStatus("warn", "connecting…");
    loadSurfaces();
    startEventStream();
    setInterval(() => loadSurfaces({ preserveErrors: true }), 30000);
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
    selectSurface: selectSurface,
    selectTask: selectTask,
    selectInbox: selectInbox,
    selectAlerts: selectAlerts,
    interruptSurface: interruptSurface,
    loadInbox: loadInbox,
    renderInboxView: renderInboxView,
    pollDashboard: pollDashboard,
    loadActivity: loadActivity,
    startEventStream: startEventStream,
    startFallbackPolling: startFallbackPolling,
    handleSseEvent: handleSseEvent,
    renderAuditPanel: renderAuditPanel,
    renderSurfaces: renderSurfaces,
    renderProjects: renderProjects,
    renderDashboard: renderDashboard,
    renderHistory: renderHistory,
    renderActivity: renderActivity,
    dedupeTaskSurfaces: dedupeTaskSurfaces,
    buildChatSendBody: buildChatSendBody,
    currentAskUserFromMessages: currentAskUserFromMessages,
    state: state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
