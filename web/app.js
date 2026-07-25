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
  const list = $("#dirOptions");
  list.innerHTML = "";
  for (const d of state.settings.directories) {
    const o = document.createElement("option");
    o.value = d;
    list.appendChild(o);
  }
  if (!$("#scanDir").value && state.settings.directories.length) {
    $("#scanDir").value = state.settings.directories[0];
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
  try {
    await api.send("PUT", "/api/settings", patch);
    await loadSettings();
    toast(t("toast.saved", "설정이 저장되었습니다"));
  } catch (e) { toast(e.message, true); }
};

$("#langSelect").onchange = async (e) => {
  await api.send("PUT", "/api/settings", { language: e.target.value });
  await loadLang(e.target.value);
};

/* ---------------------------------------------------------- scan jobs */

$("#scanStart").onclick = async () => {
  const directory = $("#scanDir").value.trim();
  if (!directory) { toast(t("scan.needDir", "디렉토리 경로를 입력하세요"), true); return; }
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
  let jobs;
  try {
    ({ jobs } = await api.get("/api/jobs"));
  } catch {
    // Server briefly unreachable (restart, sleep) — retry quietly.
    state.jobsTimer = setTimeout(pollJobs, 3000);
    return;
  }
  renderJobs(jobs);
  if (jobs.some((j) => ["queued", "scanning", "extracting"].includes(j.status))) {
    state.jobsTimer = setTimeout(pollJobs, 1200);
  }
}

/* -------- single image analyze (drag & drop / click) -------- */

const dropzone = $("#dropzone");
dropzone.onclick = () => $("#fileInput").click();
dropzone.ondragover = (e) => { e.preventDefault(); dropzone.classList.add("drag"); };
dropzone.ondragleave = () => dropzone.classList.remove("drag");
dropzone.ondrop = (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  const file = e.dataTransfer.files?.[0];
  if (file) analyzeFile(file);
};
$("#fileInput").onchange = (e) => {
  const file = e.target.files?.[0];
  if (file) analyzeFile(file);
  e.target.value = "";
};

async function analyzeFile(file) {
  dropzone.classList.add("busy");
  try {
    const form = new FormData();
    form.append("file", file);
    const r = await fetch("/api/analyze", { method: "POST", body: form });
    if (!r.ok) throw await err(r);
    const result = await r.json();
    toast(t("toast.analyzed", "분석 완료"));
    openDetailData(result);
  } catch (e) {
    toast(`${t("toast.analyzeFailed", "분석 실패")}: ${e.message}`, true);
  } finally {
    dropzone.classList.remove("busy");
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
  let jobs;
  try {
    ({ jobs } = await api.get("/api/jobs"));
  } catch (e) { toast(e.message, true); return; }
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
  let data;
  try {
    data = await api.get(
      `/api/jobs/${jobId}/results?offset=${state.offset}&limit=${state.pageSize}` +
      `&q=${encodeURIComponent(q)}&tool=${encodeURIComponent(tool)}`
    );
  } catch (e) { toast(e.message, true); return; }
  $("#resultCount").textContent = t("results.count", "결과") + `: ${data.total}`;
  const grid = $("#resultGrid");
  for (const r of data.items) grid.appendChild(renderItem(jobId, r));
  state.offset += data.items.length;
  $("#loadMore").classList.toggle("hidden", state.offset >= data.total);
}

const BROKEN_IMG =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' +
    '<rect width="100" height="100" fill="#0d0f14"/>' +
    '<text x="50" y="55" font-size="30" text-anchor="middle" fill="#3a4158">🖼</text></svg>'
  );

function renderItem(jobId, r) {
  const el = document.createElement("div");
  el.className = "item";
  const img = document.createElement("img");
  img.loading = "lazy";
  img.onerror = () => { img.onerror = null; img.src = BROKEN_IMG; };
  img.src = `/api/image?path=${encodeURIComponent(r.file)}&thumb=true`;
  const body = document.createElement("div");
  body.className = "body";
  body.innerHTML = `
    <div class="name">${escapeHtml(r.filename)}</div>
    <div class="prompt">${escapeHtml((r.prompt || "—").replace(/\s+/g, " "))}</div>
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
  try {
    const r = await api.get(`/api/jobs/${jobId}/result?file=${encodeURIComponent(file)}`);
    openDetailData(r);
  } catch (e) { toast(e.message, true); }
}

function openDetailData(r) {
  state.detail = r;
  $("#modalTitle").textContent = r.original_name || r.filename;
  const img = $("#modalImg");
  img.onerror = () => { img.onerror = null; img.src = BROKEN_IMG; };
  img.src = `/api/image?path=${encodeURIComponent(r.file)}`;
  $("#modalMeta").textContent = formatResult(r, "readable");
  applyModalLayout();
  $("#modal").classList.remove("hidden");
}

function applyModalLayout() {
  const vertical = localStorage.getItem("gensight.layout") === "vertical";
  $("#modalBody").classList.toggle("vertical", vertical);
  $("#layoutToggle").textContent = vertical ? "⇆" : "⇅";
}
$("#layoutToggle").onclick = () => {
  const now = $("#modalBody").classList.contains("vertical") ? "horizontal" : "vertical";
  localStorage.setItem("gensight.layout", now);
  applyModalLayout();
};

$("#modalClose").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#modal").classList.add("hidden");
});

$$("[data-copy]").forEach((b) => {
  b.onclick = async () => {
    if (!state.detail) return;
    const text = formatResult(state.detail, b.dataset.copy);
    $("#modalMeta").textContent = text;
    if ($("#copyWithImage").checked) {
      await copyWithImage(state.detail, text);
    } else {
      copyText(text);
    }
  };
});

async function copyWithImage(r, text) {
  try {
    const resp = await fetch(`/api/image?path=${encodeURIComponent(r.file)}&thumb=true`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const dataUrl = await new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = reject;
      fr.readAsDataURL(blob);
    });
    const html =
      `<div><img src="${dataUrl}" alt="${escapeHtml(r.filename)}"><br>` +
      `<pre>${escapeHtml(text)}</pre></div>`;
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }),
      }),
    ]);
    toast(t("toast.copiedWithImage", "이미지와 함께 복사되었습니다"));
  } catch {
    // Clipboard image copy needs a secure context — fall back to text.
    copyText(text);
  }
}

/* ---------------------------------------------------------- formatters */

const PARAM_ORDER = ["Sampler", "Scheduler", "Steps", "CFG scale", "Seed", "Size", "Denoise", "Model hash", "Model", "VAE", "Clip skip"];

function orderedParams(r) {
  const out = {};
  for (const k of PARAM_ORDER) if (r.params[k] !== undefined) out[k] = r.params[k];
  for (const [k, v] of Object.entries(r.params)) if (!(k in out)) out[k] = v;
  return out;
}

// Some tools store prompts with escaped newlines ("\n" as two chars);
// normalize them to real line breaks for display and copy.
function normalizeText(s) {
  return String(s ?? "").replace(/\\n/g, "\n").replace(/\r\n/g, "\n");
}

function formatResult(r, fmt) {
  const params = orderedParams(r);
  const prompt = normalizeText(r.prompt);
  const negative = normalizeText(r.negative_prompt);
  if (fmt === "prompt") return prompt;
  if (fmt === "readable") {
    let s = `Prompt:\n${prompt || "—"}\n`;
    if (negative) s += `\nNegative prompt:\n${negative}\n`;
    s += "\n";
    for (const [k, v] of Object.entries(params)) s += `${k}: ${v}\n`;
    return s;
  }
  r = { ...r, prompt, negative_prompt: negative };
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
  try {
    await loadSettings();
    await loadLang(state.settings.language);
  } catch (e) {
    toast(`${t("toast.loadFailed", "불러오기 실패")}: ${e.message}`, true);
  }
  pollJobs();
})();
