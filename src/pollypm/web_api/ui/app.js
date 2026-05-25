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
  const SESSION_ISSUED_COOKIE = "pollypm-session-issued-at";
  const SESSION_EXPIRY_WARNING_MS = 6 * 24 * 60 * 60 * 1000;
  const TASK_RAIL_LIMIT = 200;
  const TASK_RENDER_LIMIT = 80;
  const AUDIT_LIMIT = 25;
  const ACTIVITY_LIMIT = 40;
  const ACTIVITY_DEFAULT_SINCE = "2d";
  const INBOX_PAGE_LIMIT = 25;
  const OPERATOR_ACTOR = "operator";
  const CLAIM_ACTOR = "worker";

  const state = {
    projects: [],
    projectLoadError: null,
    selectedProject: null,
    projectFilter: "",
    projectSort: "urgency",
    surfaces: [],
    taskSurfaces: [],
    surfaceLoadError: null,
    taskLoadError: null,
    surfaceLoading: false,
    taskLoading: false,
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
    dashboardInFlight: false,
    dashboardRefreshQueued: false,
    historyInFlight: {},
    historyRefreshQueued: {},
    fetchStates: {},
    sseFailures: 0,
    sseRetryTimer: null,
    lastEventId: null,
    auditExpanded: {},
    auditEntries: {},
    auditErrors: {},
    auditLoading: {},
    surfaceMidStream: {},
    surfaceFilter: "",
    activitySince: ACTIVITY_DEFAULT_SINCE,
    activityEntries: [],
    activityStats: null,
    activityLoading: false,
    activityError: null,
    activityInFlight: false,
    activityRefreshQueued: false,
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
    if (state.projectFilter.trim()) {
      params.set("q", state.projectFilter.trim());
    }
    if (state.projectSort) {
      params.set("sort", state.projectSort);
    }
    const query = params.toString();
    return API + "/projects" + (query ? "?" + query : "");
  }

  async function loadSurfaces(opts) {
    const force = opts && opts.force;
    if (state.surfacesInFlight) {
      if (force) {
        abortFetchState("surfaces");
        state.surfacesInFlight = false;
      } else {
        state.surfacesRefreshQueued = true;
        return;
      }
    }
    state.surfacesInFlight = true;
    state.surfaceLoading = true;
    state.taskLoading = true;
    const request = beginFetchState(
      "surfaces",
      "surfaces",
      renderSurfacesLoading,
      () => loadSurfaces({ force: true }),
      { preserveSettled: true },
    );
    renderSurfaces();
    try {
      const [sessionsResult, tasksResult, projectsResult] =
        await Promise.allSettled([
          apiJson(API + "/chat/sessions", { signal: request.signal }),
          apiJsonOptional(
            API + "/tasks?limit=" + TASK_RAIL_LIMIT,
            { signal: request.signal },
          ),
          apiJsonOptional(projectListPath(), { signal: request.signal }),
        ]);

      // If any leg aborted because a newer load took over, bail without
      // mutating shared state — the newer load owns the render path.
      if (
        (sessionsResult.status === "rejected"
          && isAbortError(sessionsResult.reason))
        || (tasksResult.status === "rejected"
          && isAbortError(tasksResult.reason))
        || (projectsResult.status === "rejected"
          && isAbortError(projectsResult.reason))
      ) {
        return;
      }

      if (sessionsResult.status === "rejected") {
        state.surfaces = [];
        state.taskSurfaces = [];
        state.projects = [];
        state.projectLoadError = null;
        state.taskLoadError = null;
        const err = sessionsResult.reason instanceof Error
          ? sessionsResult.reason
          : new Error(String(sessionsResult.reason));
        state.surfaceLoadError = err;
        renderSurfaceError(err);
        renderProjects();
        return;
      }
      const data = sessionsResult.value;
      state.surfaces = Array.isArray(data.sessions) ? data.sessions : [];
      state.surfaceLoadError = null;

      if (tasksResult.status === "fulfilled") {
        state.taskLoadError = null;
        const taskData = tasksResult.value;
        const items = Array.isArray(taskData.items) ? taskData.items : [];
        state.taskSurfaces = dedupeTaskSurfaces(
          items.map(normalizeTaskSurface),
        );
      } else {
        state.taskSurfaces = [];
        state.taskLoadError = tasksResult.reason instanceof Error
          ? tasksResult.reason
          : new Error(String(tasksResult.reason));
      }

      if (projectsResult.status === "fulfilled") {
        state.projectLoadError = null;
        const projectData = projectsResult.value;
        state.projects = Array.isArray(projectData.items)
          ? projectData.items : [];
      } else {
        state.projects = [];
        state.projectLoadError = projectsResult.reason instanceof Error
          ? projectsResult.reason
          : new Error(String(projectsResult.reason));
      }
      renderProjects();
      ensureSelectionVisible();
    } finally {
      state.surfaceLoading = false;
      state.taskLoading = false;
      if (!finishFetchState("surfaces", request.seq)) return;
      state.surfacesInFlight = false;
      renderSurfaces();
      if (state.surfacesRefreshQueued) {
        state.surfacesRefreshQueued = false;
        loadSurfaces();
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
      duplicate_count: task.duplicate_count || 1,
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
        text: "no matching projects",
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
    const hasRegistered = (
      state.surfaces.length > 0
      || state.taskSurfaces.length > 0
      || state.surfaceLoadError
      || state.taskLoadError
    );
    const isLoading = state.surfaceLoading || state.taskLoading;
    if (!hasRegistered && isLoading) {
      list.appendChild(
        el("li", { class: "surface-empty", text: "loading surfaces..." }),
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
    if (taskSurfaces.length > 0 || state.taskLoadError) {
      appendRailGroup(list, "Tasks");
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
    $("pane-title").textContent = "Select a surface";
    $("pane-meta").textContent = "";
    $("send-input").disabled = true;
    $("send-button").disabled = true;
    const list = $("message-list");
    list.innerHTML = "";
    list.appendChild(el("div", {
      class: "message-empty",
      text: "No surface selected.",
    }));
    updateStopAgentButton();
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
      if (!task || task.project !== state.selectedProject) clearSelection();
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
    list.innerHTML = "";
    list.appendChild(el("div", { class: "empty-state" }, [
      el("div", { class: "empty-title", text: "No surface selected" }),
      el("div", {
        class: "empty-copy",
        text: "Choose a chat or task from the rail, or review items waiting in the inbox.",
      }),
      el("div", { class: "empty-actions" }, [openInbox, refresh]),
    ]));
    renderSurfaces();
  }

  function selectTask(key) {
    const task = state.taskSurfaces.find((item) => item.key === key);
    if (!task) return;
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

  function renderTaskSummary(task, detail) {
    const data = detail || task;
    const list = $("message-list");
    list.innerHTML = "";
    const rows = [
      ["Status", data.work_status],
      ["Project", data.project],
      ["Task", data.task_number],
      ["Assignee", data.assignee],
      ["Priority", data.priority],
      ["Updated", data.updated_at],
    ].filter((row) => row[1] != null && row[1] !== "");
    const children = [
      el("div", { class: "task-detail-title", text: data.title || task.title }),
    ];
    if (data.description) {
      children.push(el("div", {
        class: "task-detail-description",
        text: data.description,
      }));
    }
    for (const row of rows) {
      children.push(el("div", { class: "task-detail-row" }, [
        el("span", { class: "task-detail-label", text: row[0] }),
        el("span", { class: "task-detail-value", text: String(row[1]) }),
      ]));
    }
    if (data.acceptance_criteria) {
      children.push(el("div", {
        class: "task-detail-description",
        text: data.acceptance_criteria,
      }));
    }
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
    list.innerHTML = "";
    list.appendChild(
      fetchAffordance(fetchName, "div", "message-empty", "loading history..."),
    );
    updateStopAgentButton();
  }

  function renderHistory(data) {
    const list = $("message-list");
    list.innerHTML = "";
    const meta = [];
    if (data.surface_type) meta.push(data.surface_type);
    if (data.transcript_source) meta.push("src=" + data.transcript_source);
    $("pane-meta").textContent = meta.join(" · ");
    const msgs = Array.isArray(data.messages) ? data.messages.slice() : [];
    const pending = reconcilePendingMessages(data.session_name, msgs);
    state.surfaceMidStream[data.session_name] = (
      msgs.length > 0 && messageLooksMidStream(msgs[0])
    );
    updateStopAgentButton();
    if (msgs.length === 0 && pending.length === 0) {
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
    renderLocalEchoes(data.session_name);
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
    const local = addLocalEcho(name, text);
    const path = API + "/chat/" + encodeURIComponent(name) + "/send";
    try {
      await apiFetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
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

  function disabledPlanReviewButton(label) {
    const btn = el("button", {
      class: "inbox-action disabled",
      type: "button",
      text: label,
      title: "Plan approve/reject API is not available in this branch.",
    });
    btn.disabled = true;
    return btn;
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
        rowActions.push(disabledPlanReviewButton("Approve"));
        rowActions.push(disabledPlanReviewButton("Reject"));
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
    const reply = el("textarea", {
      class: "inbox-reply-input",
      rows: "3",
      placeholder: "Reply...",
      "aria-label": "Reply to inbox item",
    });
    reply.value = state.inbox.replyDraft || "";
    reply.addEventListener("input", () => {
      state.inbox.replyDraft = reply.value;
    });
    const send = el("button", {
      class: "inbox-filter-button primary",
      type: "submit",
      text: "Reply",
    });
    const actionKey = detail.id + ":reply";
    send.disabled = Boolean(state.inbox.actionInFlight[actionKey]);
    const form = el("form", { class: "inbox-reply-form" }, [reply, send]);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      replyInboxItem(detail, reply.value || "");
    });
    const decisionActions = [];
    if (detail.type === "plan_review") {
      decisionActions.push(disabledPlanReviewButton("Approve"));
      decisionActions.push(disabledPlanReviewButton("Reject"));
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
    list.innerHTML = "";
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

  async function replyInboxItem(item, body) {
    const text = String(body || "").trim();
    if (!text) return;
    const ok = await runInboxAction(
      item,
      "reply",
      "/reply",
      { body: text, owner: OPERATOR_ACTOR },
      {},
    );
    if (ok) {
      state.inbox.replyDraft = "";
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
      const query = params.toString();
      const data = await apiJson(
        API + "/dashboard" + (query ? "?" + query : ""),
        { signal: request.signal },
      );
      renderDashboard(data);
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
    list.innerHTML = "";
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
      cards.push(buildCard(
        "alerts",
        rollups.alert_count,
        rollups.alert_count > 0 ? "rollup-blocked" : "",
        // alert_count is intentionally global per DashboardRollups
        // docstring — never appears in scoped_fields, so no tag.
        false,
        dashboardCardAction({
          label: "alerts",
          value: rollups.alert_count,
          target: "detail",
          detail: "Shows the current alert count.",
        }, data),
        "Show alert count",
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

  function activityGrepPath() {
    const params = activityParams();
    params.set("limit", String(ACTIVITY_LIMIT));
    return API + "/audit/grep?" + params.toString();
  }

  function activityStatsPath() {
    return API + "/audit/stats?" + activityParams().toString();
  }

  async function loadActivity() {
    if (state.activityInFlight) {
      state.activityRefreshQueued = true;
      return;
    }
    state.activityInFlight = true;
    state.activityLoading = true;
    state.activityError = null;
    renderActivity();
    try {
      const [grepResult, statsResult] = await Promise.allSettled([
        apiJsonOptional(activityGrepPath()),
        apiJsonOptional(activityStatsPath()),
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
        state.activityError = statsResult.reason instanceof Error
          ? statsResult.reason
          : new Error(String(statsResult.reason));
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
      feed.appendChild(el("div", {
        class: "activity-empty",
        text: "activity unavailable: " + state.activityError.message,
      }));
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
    loadSurfaces();
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
    if (!input) return;
    input.addEventListener("input", () => {
      state.surfaceFilter = input.value || "";
      renderSurfaces();
    });
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
    return null;
  }

  function init() {
    maybeShowSessionExpiryBanner();
    wireProjectControls();
    wireSurfaceFilter();
    wireActivityControls();
    wireSendForm();
    wireStopAgentButton();
    if (initialUiRoute() === "inbox") {
      selectInbox();
    } else {
      renderNoSelection();
    }
    setStatus("warn", "connecting…");
    loadSurfaces();
    startEventStream();
    setInterval(loadSurfaces, 30000);
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
    renderActivity: renderActivity,
    dedupeTaskSurfaces: dedupeTaskSurfaces,
    state: state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
