/* GenSight frontend */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = {
  settings: null,
  i18n: {},
  jobsTimer: null,
  currentJob: null,
  offset: 0,
  pageSize: 60,
  detail: null,
};

const api = {
  async get(url) { const r = await fetch(url); if (!r.ok) throw await err(r); return r.json(); },
  async send(method, url, body) {
    const r = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw await err(r);
    return r.json();
  },
};
async function err(r) {
  let d = ""; try { d = (await r.json()).detail || ""; } catch {}
  return new Error(d || `HTTP ${r.status}`);
}

/* ---------------------------------------------------------- i18n */

function t(key, fallback) {
  return state.i18n[key] ?? fallback ?? key;
}

async function loadLang(lang) {
  state.i18n = await api.get(`/api/i18n/${lang}`);
  document.documentElement.lang = lang;
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n, el.textContent); });
  $$("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh, el.placeholder); });
}

/* ---------------------------------------------------------- tabs */

$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "results") refreshResultJobs();
    if (btn.dataset.tab === "settings") renderSettings();
  })
);

/* ---------------------------------------------------------- settings */

async function loadSettings() {
  state.settings = await api.get("/api/settings");
  state.pageSize = state.settings.page_size || 60;
  $("#langSelect").value = state.settings.language;
  renderScanDirs();
  return state.settings;
}

function renderScanDirs() {
  const sel = $("#scanDir");
  sel.innerHTML = "";
  for (const d of state.settings.directories) {
    const o = document.createElement("option");
    o.value = o.textContent = d;
    sel.appendChild(o);
  }
  if (!state.settings.directories.length) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = t("scan.noDirs", "설정에서 디렉토리를 먼저 추가하세요");
    sel.appendChild(o);
  }
  $("#scanRecursive").checked = state.settings.recursive;
  $("#scanWorkers").value = state.settings.workers.extract;
}

async function renderSettings() {
  await loadSettings();
  const s = state.settings;
  $("#setScanWorkers").value = s.workers.scan;
  $("#setExtractWorkers").value = s.workers.extract;
  $("#setThumbWorkers").value = s.workers.thumbnail;
  $("#setMaxJobs").value = s.max_concurrent_jobs;
  $("#setJobsPerGpu").value = s.gpu.jobs_per_gpu;

  const list = $("#dirList");
  list.innerHTML = "";
  for (const d of s.directories) {
    const li = document.createElement("li");
    li.textContent = "📁 " + d;
    const del = document.createElement("button");
    del.textContent = t("settings.remove", "삭제");
    del.onclick = async () => {
      await api.send("DELETE", `/api/settings/directories?path=${encodeURIComponent(d)}`);
      renderSettings();
    };
    li.appendChild(del);
    list.appendChild(li);
  }

  try {
    const g = await api.get("/api/gpus");
    $("#cpuInfo").textContent = `CPU cores: ${g.cpu_count}`;
    const box = $("#gpuList");
    box.innerHTML = "";
    if (!g.gpus.length) {
      box.innerHTML = `<p class="hint">${t("settings.noGpu", "감지된 NVIDIA GPU가 없습니다.")}</p>`;
    }
    for (const gpu of g.gpus) {
      const row = document.createElement("div");
      row.className = "gpu-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.dataset.gpuIndex = gpu.index;
      cb.checked = s.gpu.enabled_devices.includes(gpu.index);
      const name = document.createElement("strong");
      name.textContent = `#${gpu.index} ${gpu.name}`;
      const meter = document.createElement("span");
      meter.className = "gpu-meter";
      meter.textContent = `${gpu.memory_used_mb}/${gpu.memory_total_mb} MB · util ${gpu.utilization}%`;
      row.append(cb, name, meter);
      box.appendChild(row);
    }
  } catch { /* gpu info is optional */ }
}

$("#addDir").onclick = async () => {
  const path = $("#newDir").value.trim();
  if (!path) return;
  try {
    await api.send("POST", "/api/settings/directories", { path });
    $("#newDir").value = "";
    toast(t("toast.dirAdded", "디렉토리가 추가되었습니다"));
    renderSettings();
  } catch (e) { toast(e.message, true); }
};

$("#saveSettings").onclick = async () => {
  const patch = {
    language: $("#langSelect").value,
    workers: {
      scan: +$("#setScanWorkers").value,
      extract: +$("#setExtractWorkers").value,
      thumbnail: +$("#setThumbWorkers").value,
    },
    max_concurrent_jobs: +$("#setMaxJobs").value,
    gpu: {
      enabled_devices: $$("#gpuList input[type=checkbox]")
        .filter((c) => c.checked)
        .map((c) => +c.dataset.gpuIndex),
      jobs_per_gpu: +$("#setJobsPerGpu").value,
    },
  };
  await api.send("PUT", "/api/settings", patch);
  await loadSettings();
  toast(t("toast.saved", "설정이 저장되었습니다"));
};

$("#langSelect").onchange = async (e) => {
  await api.send("PUT", "/api/settings", { language: e.target.value });
  await loadLang(e.target.value);
};

/* ---------------------------------------------------------- scan jobs */

$("#scanStart").onclick = async () => {
  const directory = $("#scanDir").value;
  if (!directory) { toast(t("scan.noDirs", "설정에서 디렉토리를 먼저 추가하세요"), true); return; }
  try {
    await api.send("POST", "/api/scan", {
      directory,
      recursive: $("#scanRecursive").checked,
      workers: +$("#scanWorkers").value,
    });
    toast(t("toast.scanStarted", "스캔을 시작했습니다"));
    pollJobs();
  } catch (e) { toast(e.message, true); }
};

async function pollJobs() {
  clearTimeout(state.jobsTimer);
  const { jobs } = await api.get("/api/jobs");
  renderJobs(jobs);
  if (jobs.some((j) => ["queued", "scanning", "extracting"].includes(j.status))) {
    state.jobsTimer = setTimeout(pollJobs, 1200);
  }
}

function renderJobs(jobs) {
  const box = $("#jobList");
  box.innerHTML = "";
  if (!jobs.length) {
    box.innerHTML = `<p class="hint">${t("scan.noJobs", "아직 작업이 없습니다.")}</p>`;
    return;
  }
  for (const j of jobs) {
    const el = document.createElement("div");
    el.className = "job";
    const pct = j.total ? Math.round((j.processed / j.total) * 100) : 0;
    el.innerHTML = `
      <span class="dir">${escapeHtml(j.directory)}</span>
      <span class="badge ${j.status}">${t("status." + j.status, j.status)}</span>
      <div class="progress"><div style="width:${pct}%"></div></div>
      <span>${j.processed}/${j.total}${j.with_metadata ? ` · 🏷 ${j.with_metadata}` : ""}</span>`;
    const view = document.createElement("button");
    view.textContent = t("scan.view", "결과 보기");
    view.onclick = () => openResults(j.id);
    el.appendChild(view);
    if (["queued", "scanning", "extracting"].includes(j.status)) {
      const cancel = document.createElement("button");
      cancel.textContent = t("scan.cancel", "취소");
      cancel.onclick = async () => { await api.send("POST", `/api/jobs/${j.id}/cancel`); pollJobs(); };
      el.appendChild(cancel);
    } else {
      const del = document.createElement("button");
      del.textContent = "🗑";
      del.onclick = async () => { await api.send("DELETE", `/api/jobs/${j.id}`); pollJobs(); };
      el.appendChild(del);
    }
    if (j.error) {
      const msg = document.createElement("p");
      msg.className = "hint";
      msg.textContent = j.error;
      el.appendChild(msg);
    }
    box.appendChild(el);
  }
}

/* ---------------------------------------------------------- results */

async function refreshResultJobs() {
  const { jobs } = await api.get("/api/jobs");
  const sel = $("#resultJob");
  const prev = sel.value;
  sel.innerHTML = "";
  for (const j of jobs) {
    const o = document.createElement("option");
    o.value = j.id;
    o.textContent = `${j.directory} (${j.processed})`;
    sel.appendChild(o);
  }
  if (jobs.length) {
    sel.value = jobs.some((j) => j.id === prev) ? prev : jobs[0].id;
    if (sel.value !== state.currentJob) loadResults(true);
  }
}

function openResults(jobId) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === "results"));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-results"));
  refreshResultJobs().then(() => {
    $("#resultJob").value = jobId;
    loadResults(true);
  });
}

async function loadResults(reset) {
  const jobId = $("#resultJob").value;
  if (!jobId) return;
  if (reset) { state.offset = 0; $("#resultGrid").innerHTML = ""; }
  state.currentJob = jobId;
  const q = $("#resultSearch").value;
  const tool = $("#resultTool").value;
  const data = await api.get(
    `/api/jobs/${jobId}/results?offset=${state.offset}&limit=${state.pageSize}` +
    `&q=${encodeURIComponent(q)}&tool=${encodeURIComponent(tool)}`
  );
  $("#resultCount").textContent = t("results.count", "결과") + `: ${data.total}`;
  const grid = $("#resultGrid");
  for (const r of data.items) grid.appendChild(renderItem(jobId, r));
  state.offset += data.items.length;
  $("#loadMore").classList.toggle("hidden", state.offset >= data.total);
}

function renderItem(jobId, r) {
  const el = document.createElement("div");
  el.className = "item";
  const img = document.createElement("img");
  img.loading = "lazy";
  img.src = `/api/image?path=${encodeURIComponent(r.file)}&thumb=true`;
  const body = document.createElement("div");
  body.className = "body";
  body.innerHTML = `
    <div class="name">${escapeHtml(r.filename)}</div>
    <div class="prompt">${escapeHtml(r.prompt || "—")}</div>
    <div class="tools">
      <span class="tool-badge ${r.tool}">${r.tool}</span>
      ${r.params["Size"] ? `<span class="tool-badge">${escapeHtml(r.params["Size"])}</span>` : ""}
    </div>`;
  const quick = document.createElement("button");
  quick.className = "quick-copy";
  quick.textContent = "📋 JSON";
  quick.onclick = (e) => { e.stopPropagation(); copyText(formatResult(r, "json")); };
  body.querySelector(".tools").appendChild(quick);
  el.append(img, body);
  el.onclick = () => openDetail(jobId, r.file);
  return el;
}

$("#resultJob").onchange = () => loadResults(true);
$("#resultTool").onchange = () => loadResults(true);
$("#loadMore").onclick = () => loadResults(false);
let searchTimer;
$("#resultSearch").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadResults(true), 300);
};
$("#exportJson").onclick = () => exportJob("json");
$("#exportCsv").onclick = () => exportJob("csv");
function exportJob(format) {
  const jobId = $("#resultJob").value;
  if (jobId) window.open(`/api/jobs/${jobId}/export?format=${format}`, "_blank");
}

/* ---------------------------------------------------------- detail modal */

async function openDetail(jobId, file) {
  const r = await api.get(`/api/jobs/${jobId}/result?file=${encodeURIComponent(file)}`);
  state.detail = r;
  $("#modalTitle").textContent = r.filename;
  $("#modalImg").src = `/api/image?path=${encodeURIComponent(r.file)}`;
  $("#modalMeta").textContent = formatResult(r, "json");
  $("#modal").classList.remove("hidden");
}
$("#modalClose").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#modal").classList.add("hidden");
});
$$("[data-copy]").forEach((b) => {
  b.onclick = () => {
    if (!state.detail) return;
    copyText(formatResult(state.detail, b.dataset.copy));
    $("#modalMeta").textContent = formatResult(state.detail, b.dataset.copy);
  };
});

/* ---------------------------------------------------------- formatters */

const PARAM_ORDER = ["Sampler", "Scheduler", "Steps", "CFG scale", "Seed", "Size", "Denoise", "Model hash", "Model", "VAE", "Clip skip"];

function orderedParams(r) {
  const out = {};
  for (const k of PARAM_ORDER) if (r.params[k] !== undefined) out[k] = r.params[k];
  for (const [k, v] of Object.entries(r.params)) if (!(k in out)) out[k] = v;
  return out;
}

function formatResult(r, fmt) {
  const params = orderedParams(r);
  if (fmt === "prompt") return r.prompt;
  if (fmt === "json") {
    return JSON.stringify(
      { prompt: r.prompt, negative_prompt: r.negative_prompt, ...params },
      null, 2
    );
  }
  if (fmt === "markdown") {
    let s = `**Prompt**\n\`\`\`\n${r.prompt}\n\`\`\`\n`;
    if (r.negative_prompt) s += `**Negative prompt**\n\`\`\`\n${r.negative_prompt}\n\`\`\`\n`;
    s += `| Key | Value |\n|---|---|\n`;
    for (const [k, v] of Object.entries(params)) s += `| ${k} | ${v} |\n`;
    return s;
  }
  if (fmt === "bbcode") {
    let s = `[b]Prompt[/b]\n[code]${r.prompt}[/code]\n`;
    if (r.negative_prompt) s += `[b]Negative prompt[/b]\n[code]${r.negative_prompt}[/code]\n`;
    for (const [k, v] of Object.entries(params)) s += `[b]${k}[/b]: ${v}\n`;
    return s;
  }
  return JSON.stringify(r, null, 2);
}

/* ---------------------------------------------------------- utils */

function copyText(text) {
  navigator.clipboard.writeText(text)
    .then(() => toast(t("toast.copied", "클립보드에 복사되었습니다")))
    .catch(() => toast("copy failed", true));
}

function toast(msg, isErr) {
  const el = $("#toast");
  el.textContent = msg;
  el.style.borderColor = isErr ? "var(--err)" : "var(--accent)";
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 2400);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------- init */

(async function init() {
  await loadSettings();
  await loadLang(state.settings.language);
  pollJobs();
})();
