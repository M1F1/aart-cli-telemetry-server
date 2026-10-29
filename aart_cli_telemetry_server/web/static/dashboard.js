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

function tiles(summary) {
  const items = [
    ["Uses", summary.uses],
    ["Artifacts used", summary.used_artifacts],
    ["Installations using artifacts", summary.using_clients],
    ["Active installations", summary.active_clients],
    ["Installs", summary.installs],
    ["Updates", summary.updates],
    ["Removals", summary.removals],
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
  const [summary, daily, top, sources] = await Promise.all([
    stats("summary", days),
    stats("daily", days),
    stats("top-artifacts", days),
    stats("by-source", days),
  ]);
  tiles(summary);
  const labels = daily.daily.map((row) => row.day);
  draw("daily", "line", labels, [
    { label: "Uses", data: daily.daily.map((row) => row.uses), borderColor: color("--accent"), backgroundColor: color("--accent") },
    { label: "Active installations", data: daily.daily.map((row) => row.active_clients), borderColor: color("--muted"), backgroundColor: color("--muted") },
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
  const body = document.getElementById("top");
  body.replaceChildren(
    ...top.artifacts.map((row) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(row.artifact_id),
        cell(row.artifact_type),
        cell(row.uses, "number"),
        cell(row.clients, "number"),
        cell(row.installs, "number"),
        cell(row.last_used)
      );
      return tr;
    })
  );
  document.getElementById("updated").textContent = `updated ${new Date().toISOString().slice(0, 19)}Z`;
}

function run() {
  refresh().catch((error) => {
    document.getElementById("updated").textContent = `could not load: ${error.message}`;
  });
}

document.getElementById("days").addEventListener("change", run);
run();
setInterval(run, 60000);
