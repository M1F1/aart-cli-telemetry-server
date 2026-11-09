"use strict";

// The Activity log and Summary tabs, and the tabs themselves; the Explore tab is dashboard.js,
// which loads first and lends `query`, `read`, `cell`, `row`, `draw` and `color`. Everything here
// reads the API the server already answers: /api/v1/events, /api/v1/breakdown, /api/v1/artifacts.
(() => {
  const $ = (id) => document.getElementById(id);
  const DAY = 86400000;
  //: Before every event, so the log reaches back to the first one kept.
  const EPOCH = "1970-01-01T00:00:00Z";
  const LOG_PAGE = 100;
  //: The Users table counts at most this many of a period's newest events (pages of 500).
  const USER_PAGE = 500;
  const USER_PAGES = 10;
  const USER_EVENTS = USER_PAGE * USER_PAGES;

  const VERBS = {
    "artifact.installed": ["Installed", "installed"],
    "artifact.updated": ["Updated", "updated"],
    "artifact.removed": ["Removed", "removed"],
    "artifact.used": ["Used", "used"],
    "unmanaged.used": ["Used", "unmanaged"],
    "aart_cli.failed": ["Error", "failed"],
  };
  //: A use that names no harness is its source's, as the server counts it.
  const SOURCE_HARNESS = { claude_hook: "claude", opencode_plugin: "opencode" };

  const LOCALE = "en-GB";
  const timeOfDay = new Intl.DateTimeFormat(LOCALE, { hour: "2-digit", minute: "2-digit" });
  const dayOfYear = new Intl.DateTimeFormat(LOCALE, { weekday: "long", day: "numeric", month: "long" });
  const dayAndYear = new Intl.DateTimeFormat(LOCALE, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const moment = new Intl.DateTimeFormat(LOCALE, { dateStyle: "medium", timeStyle: "medium" });
  const shortDay = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", timeZone: "UTC" });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function plural(n, one, many) {
    return `${n.toLocaleString(LOCALE)} ${n === 1 ? one : many}`;
  }

  function midnight(date) {
    const day = new Date(date);
    day.setHours(0, 0, 0, 0);
    return day;
  }

  function localDay(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function dayTitle(date) {
    const today = midnight(new Date());
    const day = midnight(date);
    const back = Math.round((today - day) / DAY);
    if (back === 0) return "Today";
    if (back === 1) return "Yesterday";
    return (day.getFullYear() === today.getFullYear() ? dayOfYear : dayAndYear).format(day);
  }

  function ago(date) {
    const seconds = (Date.now() - date) / 1000;
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < DAY / 1000) return `${Math.floor(seconds / 3600)} h ago`;
    return `${plural(Math.floor(seconds / (DAY / 1000)), "day", "days")} ago`;
  }

  function newestFirst(a, b) {
    return new Date(b.occurred_at) - new Date(a.occurred_at) || b.event_id.localeCompare(a.event_id);
  }

  // ---- One event, as the log and a user's detail show it ----

  function what(item) {
    const span = el("span", "what");
    span.append(el("strong", null, item.artifact_id), el("span", "kind", item.artifact_type));
    const version =
      item.event_type === "artifact.updated" && item.previous_version
        ? `${item.previous_version} → ${item.artifact_version ?? "?"}`
        : item.artifact_version;
    if (version) span.append(el("span", "version", version));
    return span;
  }

  function where(item) {
    const came =
      item.via === "collection" ? `with the Collection ${item.collection ?? ""}`.trim()
        : item.via === "plugin" ? "with its plugin"
          : null;
    return [
      item.event_type === "unmanaged.used" ? "not installed by aart-cli" : null,
      item.tool ? `tool ${item.tool}` : null,
      came,
      item.harness ?? SOURCE_HARNESS[item.source] ?? null,
      item.scope ? `${item.scope} scope` : null,
      item.interface ? item.interface.toUpperCase() : null,
      item.registry ? `Registry ${item.registry}` : null,
    ].filter(Boolean).join(" · ");
  }

  function user(id, automated, version) {
    const span = el("span", "who");
    const code = el("code", null, id.slice(0, 8));
    code.title = `installation ${id}${version ? `, aart-cli ${version}` : ""}`;
    span.append(code);
    if (automated) span.append(el("span", "tag", "CI"));
    return span;
  }

  function entry(item, withUser) {
    const at = new Date(item.occurred_at);
    const li = el("li", "entry");
    const time = el("time", "when", timeOfDay.format(at));
    time.dateTime = item.occurred_at;
    time.title = `${moment.format(at)} (${item.occurred_at})`;
    const [verb, tone] = VERBS[item.event_type] ?? [item.event_type, ""];
    const body = el("div", "body");
    body.append(what(item));
    const context = where(item);
    if (context) body.append(el("div", "context", context));
    li.append(time, el("span", `badge ${tone}`, verb), body);
    if (withUser) li.append(user(item.client_id, item.automated, item.client_version));
    return li;
  }

  function byDay(items, withUser) {
    const days = [];
    for (const item of items) {
      const at = new Date(item.occurred_at);
      const key = localDay(at);
      if (days.length === 0 || days.at(-1).key !== key) days.push({ key, at, items: [] });
      days.at(-1).items.push(item);
    }
    return days.map((day) => {
      const section = el("section", "day");
      const list = el("ol", "entries");
      list.append(...day.items.map((item) => entry(item, withUser)));
      section.append(el("h3", null, dayTitle(day.at)), list);
      return section;
    });
  }

  // ---- Activity log ----

  const log = { events: [], next: null, key: null, asked: 0 };

  function logParams() {
    return {
      since: EPOCH,
      event_type: $("log-event").value,
      automated: $("log-who").value,
      artifact_id: $("log-artifact").value.trim(),
      limit: LOG_PAGE,
    };
  }

  function drawLog() {
    $("log").replaceChildren(...byDay(log.events, true));
    $("log-empty").hidden = log.events.length > 0;
    $("log-more").hidden = !log.next;
  }

  async function refreshLog() {
    const params = logParams();
    const key = JSON.stringify(params);
    const asked = ++log.asked;
    const page = await read("events", params);
    if (asked !== log.asked) return;
    const known = new Set(log.events.map((item) => item.event_id));
    const fresh = page.events.filter((item) => !known.has(item.event_id));
    // Another filter, or more new events than a page holds: start over from the newest page.
    // Otherwise keep the older pages already shown and add what arrived.
    if (key !== log.key || fresh.length === page.events.length) {
      log.events = page.events;
      log.next = page.next_before;
      log.key = key;
    } else {
      log.events = [...fresh, ...log.events].sort(newestFirst);
    }
    drawLog();
  }

  async function olderLog() {
    if (!log.next) return;
    const asked = log.asked;
    const page = await read("events", { ...logParams(), before: log.next });
    if (asked !== log.asked) return;
    const known = new Set(log.events.map((item) => item.event_id));
    log.events.push(...page.events.filter((item) => !known.has(item.event_id)));
    log.next = page.next_before;
    drawLog();
  }

  // ---- Summary ----

  let period = "30";
  const open = new Set();

  function since() {
    return period === "today" ? midnight(new Date()) : new Date(Date.now() - Number(period) * DAY);
  }

  async function groups(by, params) {
    const search = new URLSearchParams(query({ limit: 1000, ...params }));
    for (const dimension of by) search.append("by", dimension);
    const response = await fetch(`/api/v1/breakdown?${search}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`breakdown: ${response.status}`);
    return (await response.json()).groups;
  }

  async function peoplesEvents(from) {
    const events = [];
    let before = null;
    let pages = 0;
    do {
      const page = await read("events", { since: from, automated: "false", limit: USER_PAGE, before });
      events.push(...page.events);
      before = page.next_before;
      pages += 1;
    } while (before && pages < USER_PAGES);
    return { events, cut: Boolean(before) };
  }

  function tile(value, label, note, className) {
    const div = el("div", `tile${className ? ` ${className}` : ""}`);
    div.append(el("div", "value", value.toLocaleString(LOCALE)), el("div", "label", label));
    if (note) div.append(el("div", "note", note));
    return div;
  }

  function tiles(byWho, byType) {
    const who = Object.fromEntries(byWho.map((group) => [group.automated, group]));
    const kind = Object.fromEntries(byType.map((group) => [group.event_type, group]));
    const events = (type) => kind[type]?.events ?? 0;
    const reach = (type) => kind[type]?.clients ?? 0;
    const by = (type) => (reach(type) ? `by ${plural(reach(type), "user", "users")}` : null);
    $("summary-tiles").replaceChildren(
      tile(who.false?.clients ?? 0, "Active users", "did anything at all", "lead"),
      tile(reach("artifact.used"), "Users using artifacts", "used at least one"),
      tile(events("artifact.used"), "Uses", by("artifact.used")),
      tile(events("artifact.installed"), "Installs", by("artifact.installed")),
      tile(events("artifact.updated"), "Updates", by("artifact.updated")),
      tile(events("artifact.removed"), "Removals", by("artifact.removed")),
      tile(events("aart_cli.failed"), "Errors", by("aart_cli.failed")),
      tile(who.true?.clients ?? 0, "Automated installations", "CI jobs and the like", "aside")
    );
  }

  function chart(from, perDay, usesPerDay) {
    const days = [];
    const day = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()));
    for (const now = new Date(); day <= now; day.setUTCDate(day.getUTCDate() + 1)) {
      days.push(day.toISOString().slice(0, 10));
    }
    const active = Object.fromEntries(perDay.map((group) => [group.day, group.clients]));
    const uses = Object.fromEntries(usesPerDay.map((group) => [group.day, group.events]));
    const count = { beginAtZero: true, ticks: { precision: 0 } };
    draw(
      "summary-daily",
      "bar",
      days.map((name) => shortDay.format(new Date(`${name}T00:00:00Z`))),
      [
        { label: "Active users", data: days.map((name) => active[name] ?? 0), backgroundColor: color("--accent"), yAxisID: "y", order: 2 },
        { type: "line", label: "Uses", data: days.map((name) => uses[name] ?? 0), borderColor: color("--accent-2"), backgroundColor: color("--accent-2"), pointRadius: days.length > 60 ? 0 : 2, yAxisID: "uses", order: 1 },
      ],
      {
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { ...count, title: { display: true, text: "users" } },
          uses: { ...count, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "uses" } },
        },
      }
    );
  }

  function users(events) {
    const found = new Map();
    // Events come newest first, so a user's and an artifact's first event is their latest.
    for (const item of events) {
      if (!found.has(item.client_id)) {
        found.set(item.client_id, { id: item.client_id, last: item.occurred_at, version: item.client_version, days: new Set(), counts: {}, artifacts: new Map(), events: [] });
      }
      const one = found.get(item.client_id);
      one.days.add(localDay(new Date(item.occurred_at)));
      one.counts[item.event_type] = (one.counts[item.event_type] ?? 0) + 1;
      one.events.push(item);
      if (item.event_type === "aart_cli.failed") continue;
      const unmanaged = item.event_type === "unmanaged.used";
      const key = `${unmanaged}\u0000${item.artifact_type}\u0000${item.artifact_id}`;
      if (!one.artifacts.has(key)) {
        one.artifacts.set(key, { id: item.artifact_id, kind: item.artifact_type, unmanaged, version: item.artifact_version, last: item.occurred_at, counts: {} });
      }
      const artifact = one.artifacts.get(key);
      artifact.counts[item.event_type] = (artifact.counts[item.event_type] ?? 0) + 1;
    }
    return [...found.values()];
  }

  function counted(counts, type) {
    return cell(counts[type] ?? "", "number");
  }

  function userDetail(one) {
    const td = el("td");
    td.colSpan = 10;
    const artifacts = [...one.artifacts.values()];
    const table = el("table", "inner");
    const head = el("tr");
    for (const [name, number] of [["Artifact", false], ["Kind", false], ["Version", false], ["Uses", true], ["Installs", true], ["Updates", true], ["Removals", true], ["Last", false]]) {
      head.append(el("th", number ? "number" : null, name));
    }
    const thead = el("thead");
    thead.append(head);
    const tbody = el("tbody");
    tbody.append(
      ...artifacts.map((artifact) => {
        const name = cell(artifact.id);
        if (artifact.unmanaged) name.append(el("span", "tag", "not via aart-cli"));
        return row([
          name,
          cell(artifact.kind),
          cell(artifact.version ?? ""),
          cell((artifact.counts["artifact.used"] ?? 0) + (artifact.counts["unmanaged.used"] ?? 0) || "", "number"),
          counted(artifact.counts, "artifact.installed"),
          counted(artifact.counts, "artifact.updated"),
          counted(artifact.counts, "artifact.removed"),
          cell(ago(new Date(artifact.last))),
        ]);
      })
    );
    table.append(thead, tbody);
    const latest = one.events.slice(0, 20);
    td.append(
      el("h3", null, `What ${one.id.slice(0, 8)} did with each artifact`),
      artifacts.length ? table : el("p", "muted", "Nothing but errors."),
      el("h3", null, latest.length < one.events.length ? `Its latest ${latest.length} events` : "Its events"),
      ...byDay(latest, false)
    );
    const tr = el("tr", "user-detail");
    tr.append(td);
    return tr;
  }

  function drawUsers(found, cut) {
    const rows = [];
    for (const one of found) {
      const most = [...one.artifacts.values()]
        .filter((artifact) => artifact.counts["artifact.used"])
        .sort((a, b) => b.counts["artifact.used"] - a.counts["artifact.used"] || a.id.localeCompare(b.id))
        .slice(0, 3)
        .map((artifact) => artifact.id);
      const last = cell(ago(new Date(one.last)));
      last.title = moment.format(new Date(one.last));
      const who = el("td");
      who.append(user(one.id, false, one.version));
      const tr = row([
        who,
        last,
        cell(one.days.size, "number"),
        counted(one.counts, "artifact.used"),
        counted(one.counts, "artifact.installed"),
        counted(one.counts, "artifact.updated"),
        counted(one.counts, "artifact.removed"),
        cell([...one.artifacts.values()].filter((artifact) => !artifact.unmanaged).length, "number"),
        cell(most.join(", ")),
        cell(one.version),
      ]);
      tr.className = open.has(one.id) ? "user open" : "user";
      tr.tabIndex = 0;
      tr.setAttribute("aria-expanded", String(open.has(one.id)));
      const toggle = () => {
        if (open.has(one.id)) open.delete(one.id);
        else open.add(one.id);
        drawUsers(found, cut);
      };
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggle();
        }
      });
      rows.push(tr);
      if (open.has(one.id)) rows.push(userDetail(one));
    }
    $("users").replaceChildren(...rows);
    $("users-empty").hidden = found.length > 0;
    $("users-note").textContent = cut
      ? `This period has more than ${USER_EVENTS.toLocaleString(LOCALE)} events from people, so this table counts only the newest ${USER_EVENTS.toLocaleString(LOCALE)}: older activity is missing from it. The tiles above count everything.`
      : "Each installation that did something in the period, most recently active first. Choose a row for what it did.";
  }

  function drawArtifacts(found) {
    $("summary-artifacts").replaceChildren(
      ...found.map((item) =>
        row([
          cell(item.artifact_id),
          cell(item.artifact_type),
          cell(item.uses, "number"),
          cell(item.used_by, "number"),
          cell(item.installs, "number"),
          cell(item.holders, "number"),
        ])
      )
    );
    $("summary-artifacts-empty").hidden = found.length > 0;
  }

  let summaryAsked = 0;

  async function refreshSummary() {
    const asked = ++summaryAsked;
    const from = since();
    const at = from.toISOString();
    const people = { since: at, automated: "false" };
    const today = period === "today";
    const [byWho, byType, perDay, usesPerDay, top, events] = await Promise.all([
      groups(["automated"], { since: at }),
      groups(["event_type"], people),
      today ? [] : groups(["day"], people),
      today ? [] : groups(["day"], { ...people, event_type: "artifact.used" }),
      read("artifacts", { ...people, sort: "uses", limit: 10 }),
      peoplesEvents(at),
    ]);
    if (asked !== summaryAsked) return;
    $("summary-range").textContent = today
      ? "since midnight, your time"
      : `since ${moment.format(from)}`;
    for (const button of $("summary-period").querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.period === period));
    }
    tiles(byWho, byType);
    $("summary-chart-panel").hidden = today;
    if (!today) chart(from, perDay, usesPerDay);
    drawUsers(users(events.events), events.cut);
    drawArtifacts(top.artifacts.filter((item) => item.uses || item.installs));
  }

  // ---- Tabs ----

  const VIEWS = {
    log: refreshLog,
    summary: refreshSummary,
    explore: async () => window.explore.refresh(),
  };

  function current() {
    const name = location.hash.slice(1);
    return Object.hasOwn(VIEWS, name) ? name : "log";
  }

  function refresh() {
    const name = current();
    VIEWS[name]()
      .then(() => {
        // Explore says for itself when it was updated or why it could not be.
        if (name !== "explore") $("updated").textContent = `updated ${timeOfDay.format(new Date())}`;
      })
      .catch((error) => {
        $("updated").textContent = `could not load: ${error.message}`;
      });
  }

  function show() {
    const name = current();
    for (const key of Object.keys(VIEWS)) {
      const chosen = key === name;
      $(`view-${key}`).hidden = !chosen;
      $(`tab-${key}`).setAttribute("aria-selected", String(chosen));
    }
    refresh();
  }

  for (const id of ["log-event", "log-who"]) $(id).addEventListener("change", refresh);
  let typing = null;
  $("log-artifact").addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(refresh, 300);
  });
  $("log-more").addEventListener("click", () => {
    olderLog().catch((error) => {
      $("updated").textContent = `could not load: ${error.message}`;
    });
  });
  $("summary-period").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-period]");
    if (!button) return;
    period = button.dataset.period;
    open.clear();
    refresh();
  });
  window.addEventListener("hashchange", show);
  setInterval(() => {
    if (!document.hidden) refresh();
  }, 60000);
  show();
})();
