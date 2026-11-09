"use strict";

const charts = {};
const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function stats(path, days) {
  const response = await fetch(`/api/v1/stats/${path}?days=${days}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = String(text);
  if (className) td.className = className;
  return td;
}

let chosen = null;

// What a breakdown can group by, as `/api/v1/breakdown` names it, and what the panel calls it.
const DIMENSIONS = [
  ["harness", "harness"],
  ["scope", "scope"],
  ["registry", "Registry"],
  ["interface", "TUI or CLI"],
  ["via", "how it came"],
  ["collection", "Collection"],
  ["event_type", "event"],
  ["artifact_type", "kind"],
  ["artifact_id", "artifact"],
  ["artifact_version", "version"],
  ["previous_version", "updated from"],
  ["tool", "MCP tool"],
  ["source", "source"],
  ["automated", "automation"],
  ["client_version", "aart-cli"],
  ["day", "day"],
  ["os", "OS"],
  ["arch", "architecture"],
  ["python", "Python"],
];
const DIMENSION_NAMES = Object.fromEntries(DIMENSIONS);

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined) search.set(key, value);
  }
  return search.toString();
}

async function read(path, params) {
  const response = await fetch(`/api/v1/${path}?${query(params)}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

function row(cells) {
  const tr = document.createElement("tr");
  tr.append(...cells);
  return tr;
}

async function artifacts(days) {
  const found = await read("artifacts", {
    days,
    sort: document.getElementById("order").value,
    q: document.getElementById("search").value.trim(),
    artifact_type: document.getElementById("kind").value,
    limit: 200,
  });
  document.getElementById("artifacts").replaceChildren(
    ...found.artifacts.map((item) => {
      const tr = row([
        cell(item.artifact_id),
        cell(item.artifact_type),
        cell(item.latest_version ?? ""),
        cell(item.installs, "number"),
        cell(item.updates, "number"),
        cell(item.removals, "number"),
        cell(item.holders, "number"),
        cell(item.uses, "number"),
        cell(item.member_uses || "", "number"),
        cell(item.used_by, "number"),
        cell(item.last_seen ?? ""),
      ]);
      if (chosen && chosen.type === item.artifact_type && chosen.id === item.artifact_id) tr.className = "chosen";
      tr.addEventListener("click", () => {
        chosen = { type: item.artifact_type, id: item.artifact_id };
        run();
      });
      return tr;
    })
  );
}

function dimensionOptions() {
  for (const [index, select] of document.querySelectorAll("select.dimension").entries()) {
    select.append(
      ...DIMENSIONS.map(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        return option;
      })
    );
    if (index === 0) select.value = "harness";
  }
}

async function breakdown(days) {
  const by = ["by-1", "by-2", "by-3"]
    .map((id) => document.getElementById(id).value)
    .filter((value, index, all) => value && all.indexOf(value) === index);
  const search = new URLSearchParams(
    query({
      days,
      event_type: document.getElementById("breakdown-event").value,
      automated: document.getElementById("breakdown-who").value,
      limit: 200,
    })
  );
  for (const dimension of by) search.append("by", dimension);
  const response = await fetch(`/api/v1/breakdown?${search}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`breakdown: ${response.status}`);
  const found = await response.json();
  const head = document.createElement("th");
  document.getElementById("breakdown-head").replaceChildren(
    ...found.by.map((name) => {
      const th = head.cloneNode();
      th.textContent = DIMENSION_NAMES[name] ?? name;
      return th;
    }),
    Object.assign(head.cloneNode(), { className: "number", textContent: "Events" }),
    Object.assign(head.cloneNode(), { className: "number", textContent: "Installations" })
  );
  document.getElementById("breakdown").replaceChildren(
    ...found.groups.map((group) =>
      row([
        ...found.by.map((name) => cell(group[name] ?? "not said", group[name] === null ? "muted" : "")),
        cell(group.events, "number"),
        cell(group.clients, "number"),
      ])
    )
  );
}

async function detail(days) {
  const panel = document.getElementById("detail");
  if (!chosen) {
    panel.hidden = true;
    return;
  }
  const path = `artifacts/${encodeURIComponent(chosen.type)}/${encodeURIComponent(chosen.id)}`;
  const response = await fetch(`/api/v1/${path}?${query({ days })}`, { cache: "no-store" });
  if (response.status === 404) {
    panel.hidden = true;
    return;
  }
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  const found = await response.json();
  const events = await read("events", { days, artifact_id: chosen.id, members: true, limit: 50 });
  panel.hidden = false;
  document.getElementById("detail-title").textContent = `${found.artifact_type} ${found.artifact_id}`;
  document.getElementById("detail-meta").textContent = [
    found.plugin ? `a skill of the plugin ${found.plugin}` : null,
    `latest version ${found.latest_version ?? "unknown"}`,
    `${found.holders} installations hold it now`,
    `${found.installed_by} installed it, ${found.used_by} used it`,
    found.sources.map((item) => `${item.source}: ${item.events}`).join(", "),
  ].filter(Boolean).join(" · ");
  document.getElementById("detail-versions").replaceChildren(
    ...found.versions.map((item) =>
      row([
        cell(item.artifact_version ?? "unknown"),
        cell(item.installs, "number"),
        cell(item.updates, "number"),
        cell(item.removals, "number"),
        cell(item.uses, "number"),
        cell(item.last_seen),
      ])
    )
  );
  document.getElementById("detail-members-block").hidden = found.members.length === 0;
  document.getElementById("detail-members").replaceChildren(
    ...found.members.map((item) =>
      row([
        cell(item.artifact_id),
        cell(item.installs, "number"),
        cell(item.holders, "number"),
        cell(item.uses, "number"),
        cell(item.used_by, "number"),
        cell(item.last_seen ?? ""),
      ])
    )
  );
  document.getElementById("detail-events").replaceChildren(
    ...events.events.map((item) =>
      row([
        cell(item.occurred_at),
        cell(item.event_type),
        cell(item.artifact_id),
        cell(item.artifact_version ?? ""),
        cell(item.source),
        cell(item.harness ?? ""),
        cell(item.scope ?? ""),
        cell(item.registry ?? ""),
        cell([item.interface, item.via === "direct" ? null : item.via, item.collection].filter(Boolean).join(" ")),
        cell(item.client_id.slice(0, 8)),
        cell(item.client_version),
      ])
    )
  );
}

function tiles(summary) {
  const items = [
    ["Uses", summary.uses],
    ["Artifacts used", summary.used_artifacts],
    ["People's installations using artifacts", summary.using_clients],
    ["People's active installations", summary.active_clients],
    ["Automated installations (CI)", summary.automated_clients],
    ["Installs", summary.installs],
    ["Updates", summary.updates],
    ["Removals", summary.removals],
    ["Uses of tools aart-cli did not install", summary.unmanaged_uses],
    ["Errors aart-cli met", summary.failures],
  ];
  const root = document.getElementById("tiles");
  root.replaceChildren(
    ...items.map(([label, value]) => {
      const tile = document.createElement("div");
      tile.className = "tile";
      const number = document.createElement("div");
      number.className = "value";
      number.textContent = String(value);
      const text = document.createElement("div");
      text.className = "label";
      text.textContent = label;
      tile.append(number, text);
      return tile;
    })
  );
}

function draw(id, type, labels, datasets, options = {}) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), {
    type,
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      ...options,
    },
  });
}

async function refresh() {
  const days = document.getElementById("days").value;
  const [summary, daily, unmanaged, sources, failures] = await Promise.all([
    stats("summary", days),
    stats("daily", days),
    stats("top-unmanaged", days),
    stats("by-source", days),
    stats("failures", days),
  ]);
  tiles(summary);
  const labels = daily.daily.map((row) => row.day);
  draw("daily", "line", labels, [
    { label: "Uses", data: daily.daily.map((row) => row.uses), borderColor: color("--accent"), backgroundColor: color("--accent") },
    { label: "People's active installations", data: daily.daily.map((row) => row.active_clients), borderColor: color("--muted"), backgroundColor: color("--muted") },
    { label: "Automated installations", data: daily.daily.map((row) => row.automated_clients), borderColor: color("--accent-3"), backgroundColor: color("--accent-3"), borderDash: [4, 4] },
  ]);
  draw("lifecycle", "bar", labels, [
    { label: "Installs", data: daily.daily.map((row) => row.installs), backgroundColor: color("--accent-2") },
    { label: "Updates", data: daily.daily.map((row) => row.updates), backgroundColor: color("--accent-3") },
    { label: "Removals", data: daily.daily.map((row) => row.removals), backgroundColor: color("--accent-4") },
  ]);
  draw(
    "sources",
    "bar",
    sources.by_source.map((row) => `${row.source} · ${row.artifact_type}`),
    [{ label: "Uses", data: sources.by_source.map((row) => row.uses), backgroundColor: color("--accent") }],
    { indexAxis: "y", scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } }
  );
  document.getElementById("unmanaged").replaceChildren(
    ...unmanaged.tools.map((row) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(row.artifact_id),
        cell(row.artifact_type),
        cell(row.uses, "number"),
        cell(row.clients, "number"),
        cell(row.last_used)
      );
      return tr;
    })
  );
  document.getElementById("failures").replaceChildren(
    ...failures.failures.map((row) => {
      const tr = document.createElement("tr");
      const raised = row.causes.map((cause) => `from ${cause.exception}`);
      const what = [[row.exception, ...raised].filter(Boolean).join(" "), ...row.diagnostics]
        .filter(Boolean)
        .join(", ");
      const where = cell(row.place);
      where.title = [
        ...row.frames,
        ...row.causes.flatMap((cause) => [`raised from ${cause.exception}:`, ...cause.frames]),
      ].join("\n");
      tr.append(
        where,
        cell(row.kind),
        cell(what),
        cell(row.occurrences, "number"),
        cell(row.clients, "number"),
        cell(row.versions.join(", ")),
        cell(row.platforms.join(", ")),
        cell(row.last_seen)
      );
      return tr;
    })
  );
  await artifacts(days);
  await breakdown(days);
  await detail(days);
  document.getElementById("updated").textContent = `updated ${new Date().toISOString().slice(0, 19)}Z`;
}

function run() {
  refresh().catch((error) => {
    document.getElementById("updated").textContent = `could not load: ${error.message}`;
  });
}

dimensionOptions();
for (const id of ["by-1", "by-2", "by-3", "breakdown-event", "breakdown-who"]) {
  document.getElementById(id).addEventListener("change", run);
}
document.getElementById("days").addEventListener("change", run);
document.getElementById("kind").addEventListener("change", run);
document.getElementById("order").addEventListener("change", run);
let typing = null;
document.getElementById("search").addEventListener("input", () => {
  clearTimeout(typing);
  typing = setTimeout(run, 250);
});
// The Explore tab. activity.js shows the tabs and refreshes the one that is open.
window.explore = { refresh: run };
