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
    if (btn.dataset.tab === "library") loadLibrary(true);
    if (btn.dataset.tab === "stats") loadStats();
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

  renderWatches();
  renderGroups();
  renderTagger();
  renderQuality();
  renderAuth();

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

/* -------- watches / groups / tagger (settings) -------- */

async function renderWatches() {
  let data;
  try { data = await api.get("/api/watches"); } catch { return; }
  const box = $("#watchList");
  box.innerHTML = "";
  for (const w of data.watches) {
    const li = document.createElement("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!w.enabled;
    cb.onchange = async () => {
      await api.send("PATCH", `/api/watches/${w.id}`, { enabled: cb.checked });
    };
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent = `👁 ${w.directory} · ${w.poll_interval}s` +
      (w.recursive ? "" : ` · ${t("settings.noRecursive", "하위 폴더 제외")}`);
    const del = document.createElement("button");
    del.textContent = t("settings.remove", "삭제");
    del.onclick = async () => {
      await api.send("DELETE", `/api/watches/${w.id}`);
      renderWatches();
    };
    li.append(cb, label, del);
    box.appendChild(li);
  }
  const st = data.watcher;
  $("#watcherStatus").textContent =
    `${t("settings.watcher", "감시 상태")}: ${st.running ? "✓" : "✗"}` +
    ` · ${st.realtime ? "watchdog + polling" : "polling only"}` +
    (st.last_error ? ` · ${st.last_error}` : "");
}

$("#addWatch").onclick = async () => {
  const directory = $("#watchDir").value.trim();
  if (!directory) return;
  try {
    await api.send("POST", "/api/watches", {
      directory,
      recursive: $("#watchRecursive").checked,
      poll_interval: +$("#watchInterval").value || 30,
    });
    $("#watchDir").value = "";
    toast(t("toast.saved", "설정이 저장되었습니다"));
    renderWatches();
  } catch (e) { toast(e.message, true); }
};

async function renderGroups() {
  let data;
  try { data = await api.get("/api/groups"); } catch { return; }
  const box = $("#groupList");
  box.innerHTML = "";
  for (const g of data.groups) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent =
      `🏷 ${g.name} ← ${g.is_regex ? "regex" : "text"} "${g.pattern}" @ ${g.target}`;
    const del = document.createElement("button");
    del.textContent = t("settings.remove", "삭제");
    del.onclick = async () => {
      await api.send("DELETE", `/api/groups/${g.id}`);
      renderGroups();
    };
    li.append(label, del);
    box.appendChild(li);
  }
}

$("#addGroup").onclick = async () => {
  const name = $("#groupName").value.trim();
  const pattern = $("#groupPattern").value.trim();
  if (!name || !pattern) return;
  try {
    await api.send("POST", "/api/groups", {
      name,
      pattern,
      is_regex: $("#groupRegex").checked,
      target: $("#groupTarget").value,
    });
    $("#groupName").value = $("#groupPattern").value = "";
    renderGroups();
  } catch (e) { toast(e.message, true); }
};

$("#applyGroups").onclick = async () => {
  try {
    const r = await api.send("POST",
      `/api/groups/apply?overwrite=${$("#groupOverwrite").checked}`);
    $("#applyResult").textContent =
      `${t("settings.applied", "적용됨")}: ${r.updated}`;
  } catch (e) { toast(e.message, true); }
};

let taggerTimer;
async function renderTagger() {
  clearTimeout(taggerTimer);
  let s;
  try { s = await api.get("/api/tagger/status"); } catch { return; }
  let text;
  if (!s.available) {
    text = s.reason;
  } else if (s.job && s.job.status === "running") {
    text = `${t("status.extracting", "추출중")} ${s.job.processed}/${s.job.total}`;
    taggerTimer = setTimeout(renderTagger, 1500);
  } else {
    text = `${t("settings.untagged", "미태깅")}: ${s.untagged}` +
      (s.job ? ` · ${t("status." + s.job.status, s.job.status)}` : "");
  }
  $("#taggerStatus").textContent = text;
}

$("#taggerRun").onclick = async () => {
  try {
    await api.send("POST", "/api/tagger/run", {});
    renderTagger();
  } catch (e) { toast(e.message, true); }
};
$("#taggerCancel").onclick = async () => {
  try { await api.send("POST", "/api/tagger/cancel"); } catch {}
  renderTagger();
};

let qualityTimer;
async function renderQuality() {
  clearTimeout(qualityTimer);
  let s;
  try { s = await api.get("/api/quality/status"); } catch { return; }
  let text;
  if (s.job && s.job.status === "running") {
    text = `${t("status.extracting", "추출중")} ${s.job.processed}/${s.job.total}`;
    qualityTimer = setTimeout(renderQuality, 1500);
  } else {
    text = `${t("settings.qualityPending", "미분석")}: ${s.pending}` +
      (s.job ? ` · ${t("status." + s.job.status, s.job.status)}` : "");
  }
  $("#qualityStatus").textContent = text;
}

$("#qualityRun").onclick = async () => {
  try {
    await api.send("POST", "/api/quality/run", {});
    renderQuality();
  } catch (e) { toast(e.message, true); }
};
$("#qualityCancel").onclick = async () => {
  try { await api.send("POST", "/api/quality/cancel"); } catch {}
  renderQuality();
};

/* -------- organize -------- */

async function runOrganize(dryRun) {
  const target_root = $("#orgRoot").value.trim();
  if (!target_root) { toast(t("settings.orgNeedRoot", "대상 디렉토리를 입력하세요"), true); return; }
  if (!dryRun && !confirm(t("settings.orgConfirm", "미리보기 내용대로 파일을 이동할까요?"))) return;
  try {
    const r = await api.send("POST", "/api/organize", {
      target_root,
      template: $("#orgTemplate").value.trim() || "{model}/{date}",
      dry_run: dryRun,
      ...libFilters(),
    });
    const box = $("#orgResult");
    box.classList.remove("hidden");
    if (r.dry_run) {
      const lines = r.moves.map((m) => `${m.from}\n  → ${m.to}`).join("\n");
      box.textContent =
        `${t("settings.orgPlanned", "이동 예정")}: ${r.count}\n\n${lines}` +
        (r.count > r.moves.length ? `\n... (+${r.count - r.moves.length})` : "");
    } else {
      box.textContent = `${t("settings.orgMoved", "이동 완료")}: ${r.count}` +
        (r.errors.length ? `\n${t("status.error", "오류")}: ${JSON.stringify(r.errors, null, 2)}` : "");
      toast(`${t("settings.orgMoved", "이동 완료")}: ${r.count}`);
    }
  } catch (e) { toast(e.message, true); }
}
$("#orgPreview").onclick = () => runOrganize(true);
$("#orgApply").onclick = () => runOrganize(false);

/* -------- auth -------- */

async function renderAuth() {
  let s;
  try { s = await api.get("/api/auth/status"); } catch { return; }
  $("#authDisabledBox").classList.toggle("hidden", s.enabled);
  $("#authEnabledBox").classList.toggle("hidden", !s.enabled);
  if (s.enabled) {
    $("#authInfo").textContent =
      `${t("settings.authOn", "활성화됨")} — ${s.username}`;
  }
}

$("#authEnable").onclick = async () => {
  const username = $("#authUser").value.trim();
  const password = $("#authPass").value;
  if (!username || password.length < 4) {
    toast(t("auth.weak", "사용자명과 4자 이상 비밀번호가 필요합니다"), true);
    return;
  }
  try {
    await api.send("POST", "/api/auth/setup", { username, password });
    $("#authPass").value = "";
    toast(t("settings.authOn", "활성화됨"));
    renderAuth();
  } catch (e) { toast(e.message, true); }
};

$("#authDisable").onclick = async () => {
  try {
    await api.send("POST", "/api/auth/disable",
      { password: $("#authDisablePass").value });
    $("#authDisablePass").value = "";
    toast(t("settings.authOff", "인증이 해제되었습니다"));
    renderAuth();
  } catch (e) { toast(e.message, true); }
};

/* -------- login overlay -------- */

async function checkLogin() {
  try {
    const s = await api.get("/api/auth/status");
    const need = s.enabled && !s.authenticated;
    $("#loginOverlay").classList.toggle("hidden", !need);
    return !need;
  } catch { return true; }
}

$("#loginBtn").onclick = doLogin;
$("#loginPass").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
async function doLogin() {
  try {
    await api.send("POST", "/api/auth/login", {
      username: $("#loginUser").value.trim(),
      password: $("#loginPass").value,
    });
    location.reload();
  } catch (e) {
    const el = $("#loginError");
    el.textContent = t("auth.failed", "로그인 실패");
    el.classList.remove("hidden");
  }
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

/* ---------------------------------------------------------- library */

const lib = { offset: 0, dupes: false, trash: false };

function libFilters() {
  const f = {
    q: $("#libSearch").value,
    tool: $("#libTool").value,
    min_rating: +$("#libRating").value,
    group: $("#libGroup").value,
    quality: $("#libQuality").value,
  };
  if ($("#libFav").classList.contains("on")) f.favorite = true;
  return f;
}

function libParams() {
  const f = libFilters();
  const p = new URLSearchParams({
    q: f.q, tool: f.tool, min_rating: f.min_rating, group: f.group,
    quality: f.quality, sort: $("#libSort").value,
    offset: lib.offset, limit: state.pageSize,
  });
  if (f.favorite) p.set("favorite", "true");
  return p;
}

async function loadLibrary(reset) {
  if (lib.trash) { loadTrash(); return; }
  if (lib.dupes) { loadDuplicates(); return; }
  if (reset) { lib.offset = 0; $("#libGrid").innerHTML = ""; }
  let data;
  try {
    data = await api.get(`/api/library?${libParams()}`);
  } catch (e) { toast(e.message, true); return; }
  $("#libCount").textContent = t("results.count", "결과") + `: ${data.total}`;
  const groupSel = $("#libGroup");
  const prevGroup = groupSel.value;
  while (groupSel.options.length > 1) groupSel.remove(1);
  for (const g of data.groups) {
    const o = document.createElement("option");
    o.value = o.textContent = g;
    groupSel.appendChild(o);
  }
  groupSel.value = prevGroup;
  const grid = $("#libGrid");
  for (const r of data.items) grid.appendChild(renderLibItem(r));
  lib.offset += data.items.length;
  $("#libMore").classList.toggle("hidden", lib.offset >= data.total);
}

async function loadDuplicates() {
  $("#libGrid").innerHTML = "";
  $("#libMore").classList.add("hidden");
  let data;
  try {
    data = await api.get("/api/library/duplicates");
  } catch (e) { toast(e.message, true); return; }
  $("#libCount").textContent =
    t("lib.dupeGroups", "중복 그룹") + `: ${data.groups.length}`;
  const grid = $("#libGrid");
  if (!data.groups.length) {
    grid.innerHTML = `<p class="hint">${t("lib.noDupes", "중복 이미지가 없습니다.")}</p>`;
    return;
  }
  data.groups.forEach((g, i) => {
    const head = document.createElement("div");
    head.className = "dupe-head";
    head.textContent = `#${i + 1} — ${g.count} ${t("lib.files", "개 파일")}`;
    grid.appendChild(head);
    for (const r of g.items) grid.appendChild(renderLibItem(r));
  });
}

function starSpan(rating, onSet) {
  const span = document.createElement("span");
  span.className = "stars";
  for (let i = 1; i <= 5; i++) {
    const s = document.createElement("span");
    s.className = "s" + (i <= rating ? " on" : "");
    s.textContent = "★";
    s.onclick = (e) => { e.stopPropagation(); onSet(i === rating ? 0 : i); };
    span.appendChild(s);
  }
  return span;
}

async function loadTrash() {
  $("#libGrid").innerHTML = "";
  $("#libMore").classList.add("hidden");
  let data;
  try { data = await api.get("/api/trash"); }
  catch (e) { toast(e.message, true); return; }
  $("#libCount").textContent = t("lib.trash", "휴지통") + `: ${data.items.length}`;
  const grid = $("#libGrid");
  if (!data.items.length) {
    grid.innerHTML = `<p class="hint">${t("lib.trashEmpty", "휴지통이 비어 있습니다.")}</p>`;
    return;
  }
  const head = document.createElement("div");
  head.className = "dupe-head";
  const emptyBtn = document.createElement("button");
  emptyBtn.textContent = t("lib.emptyTrash", "휴지통 비우기 (영구 삭제)");
  emptyBtn.onclick = async () => {
    if (!confirm(t("lib.emptyConfirm", "휴지통의 모든 파일을 영구 삭제할까요?"))) return;
    try {
      const r = await api.send("DELETE", "/api/trash");
      toast(`${t("lib.purged", "영구 삭제됨")}: ${r.purged}`);
      loadTrash();
    } catch (e) { toast(e.message, true); }
  };
  head.appendChild(emptyBtn);
  grid.appendChild(head);
  for (const it of data.items) {
    const el = document.createElement("div");
    el.className = "item";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.onerror = () => { img.onerror = null; img.src = BROKEN_IMG; };
    img.src = `/api/image?path=${encodeURIComponent(it.trash_path)}&thumb=true`;
    const body = document.createElement("div");
    body.className = "body";
    body.innerHTML = `<div class="name">${escapeHtml(it.original_path)}</div>`;
    const row = document.createElement("div");
    row.className = "overlay";
    const restore = document.createElement("button");
    restore.textContent = t("lib.restore", "복구");
    restore.onclick = async () => {
      try {
        await api.send("POST", `/api/trash/${it.id}/restore`);
        toast(t("lib.restored", "복구되었습니다"));
        loadTrash();
      } catch (e) { toast(e.message, true); }
    };
    const purge = document.createElement("button");
    purge.textContent = t("lib.purge", "영구 삭제");
    purge.onclick = async () => {
      if (!confirm(t("lib.purgeConfirm", "이 파일을 영구 삭제할까요?"))) return;
      try {
        await api.send("DELETE", `/api/trash/${it.id}`);
        loadTrash();
      } catch (e) { toast(e.message, true); }
    };
    row.append(restore, purge);
    body.appendChild(row);
    el.append(img, body);
    grid.appendChild(el);
  }
}

function qualityBadge(r) {
  if (r.quality_score === null || r.quality_score === undefined) return "";
  const s = Math.round(r.quality_score);
  const cls = s >= 80 ? "good" : s >= 50 ? "mid" : "bad";
  return `<span class="q-badge ${cls}" title="${(r.quality_issues || []).join(", ")}">Q${s}</span>`;
}

function renderLibItem(r) {
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
      ${r.group_name ? `<span class="tool-badge">${escapeHtml(r.group_name)}</span>` : ""}
      ${qualityBadge(r)}
      ${typeof r.distance === "number" ? `<span class="tool-badge">d=${r.distance}</span>` : ""}
    </div>`;
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.appendChild(starSpan(r.rating, async (v) => {
    const updated = await patchMeta(r.file, { rating: v });
    if (updated) { r.rating = updated.rating; el.replaceWith(renderLibItem(r)); }
  }));
  const fav = document.createElement("button");
  fav.className = "fav-btn" + (r.favorite ? " on" : "");
  fav.textContent = r.favorite ? "♥" : "♡";
  fav.onclick = async (e) => {
    e.stopPropagation();
    const updated = await patchMeta(r.file, { favorite: !r.favorite });
    if (updated) { r.favorite = updated.favorite; el.replaceWith(renderLibItem(r)); }
  };
  overlay.appendChild(fav);
  const trashBtn = document.createElement("button");
  trashBtn.className = "trash-btn";
  trashBtn.textContent = "🗑";
  trashBtn.title = t("lib.toTrash", "휴지통으로 이동");
  trashBtn.onclick = async (e) => {
    e.stopPropagation();
    try {
      await api.send("POST", "/api/trash", { path: r.file });
      toast(t("lib.trashed", "휴지통으로 이동했습니다"));
      el.remove();
    } catch (err2) { toast(err2.message, true); }
  };
  overlay.appendChild(trashBtn);
  body.appendChild(overlay);
  el.append(img, body);
  el.onclick = () => openLibraryDetail(r.file);
  return el;
}

async function patchMeta(path, fields) {
  try {
    return await api.send("PATCH", "/api/library/item", { path, ...fields });
  } catch (e) { toast(e.message, true); return null; }
}

async function openLibraryDetail(path) {
  try {
    const r = await api.get(`/api/library/item?path=${encodeURIComponent(path)}`);
    openDetailData(r, { library: true });
  } catch (e) { toast(e.message, true); }
}

$("#libTool").onchange = $("#libRating").onchange = $("#libQuality").onchange =
$("#libGroup").onchange = $("#libSort").onchange = () => loadLibrary(true);
$("#libMore").onclick = () => loadLibrary(false);
$("#libFav").onclick = () => {
  $("#libFav").classList.toggle("on");
  loadLibrary(true);
};
$("#libDupes").onclick = () => {
  lib.dupes = !lib.dupes;
  lib.trash = false;
  $("#libTrash").classList.remove("on");
  $("#libDupes").classList.toggle("on", lib.dupes);
  loadLibrary(true);
};
$("#libTrash").onclick = () => {
  lib.trash = !lib.trash;
  lib.dupes = false;
  $("#libDupes").classList.remove("on");
  $("#libTrash").classList.toggle("on", lib.trash);
  loadLibrary(true);
};
let libSearchTimer;
$("#libSearch").oninput = () => {
  clearTimeout(libSearchTimer);
  libSearchTimer = setTimeout(() => loadLibrary(true), 300);
};

/* ---------------------------------------------------------- stats */

function barRows(container, rows, unit) {
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = `<p class="hint">—</p>`;
    return;
  }
  const max = rows[0].count || 1;
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "bar-row";
    el.innerHTML = `
      <span class="label" title="${escapeHtml(r.token)}">${escapeHtml(r.token)}</span>
      <span class="bar"><div style="width:${Math.round((r.count / max) * 100)}%"></div></span>
      <span class="count">${r.count}${unit || ""}</span>`;
    container.appendChild(el);
  }
}

async function loadStats() {
  let s;
  try {
    s = await api.get("/api/stats/prompts?top=40");
  } catch (e) { toast(e.message, true); return; }
  $("#statsSummary").textContent =
    `${t("stats.images", "분석된 이미지")}: ${s.images}`;
  barRows($("#statsPos"), s.positive);
  barRows($("#statsNeg"), s.negative);
  barRows($("#statsModels"), s.models);
  barRows($("#statsSamplers"), s.samplers);
}

/* ---------------------------------------------------------- detail modal */

async function openDetail(jobId, file) {
  try {
    const r = await api.get(`/api/jobs/${jobId}/result?file=${encodeURIComponent(file)}`);
    openDetailData(r);
  } catch (e) { toast(e.message, true); }
}

function openDetailData(r, opts = {}) {
  state.detail = r;
  $("#modalTitle").textContent = r.original_name || r.filename;
  const img = $("#modalImg");
  img.onerror = () => { img.onerror = null; img.src = BROKEN_IMG; };
  img.src = `/api/image?path=${encodeURIComponent(r.file)}`;
  $("#modalMeta").textContent = formatResult(r, "readable");

  // Library-only controls: rating / favorite / group / tags / similar
  const controls = $("#modalControls");
  controls.classList.toggle("hidden", !opts.library);
  if (opts.library) {
    const starsBox = $("#modalStars");
    starsBox.innerHTML = "";
    starsBox.appendChild(starSpan(r.rating || 0, async (v) => {
      const updated = await patchMeta(r.file, { rating: v });
      if (updated) openDetailData(updated, opts);
    }));
    const fav = $("#modalFav");
    fav.textContent = r.favorite ? "♥" : "♡";
    fav.classList.toggle("on", !!r.favorite);
    fav.onclick = async () => {
      const updated = await patchMeta(r.file, { favorite: !r.favorite });
      if (updated) openDetailData(updated, opts);
    };
    const groupBadge = $("#modalGroup");
    groupBadge.classList.toggle("hidden", !r.group_name);
    groupBadge.textContent = r.group_name || "";
  }

  const tagsBox = $("#modalTags");
  const tags = Array.isArray(r.tags) ? r.tags : [];
  const issues = (r.quality_issues || []).map((i) => `⚠ ${i}`);
  if (r.quality_score !== null && r.quality_score !== undefined) {
    issues.unshift(`Q ${Math.round(r.quality_score)}/100`);
  }
  const chips = [...issues, ...tags];
  tagsBox.classList.toggle("hidden", !chips.length);
  tagsBox.innerHTML = chips.map((x) => `<span>${escapeHtml(x)}</span>`).join("");

  const simBox = $("#similarBox");
  simBox.classList.add("hidden");
  if (r.phash) {
    api.get(`/api/library/similar?path=${encodeURIComponent(r.file)}&max_distance=10`)
      .then((d) => {
        if (state.detail !== r || !d.items.length) return;
        simBox.classList.remove("hidden");
        const strip = $("#similarStrip");
        strip.innerHTML = "";
        for (const s of d.items) {
          const im = document.createElement("img");
          im.title = `${s.filename} (d=${s.distance})`;
          im.onerror = () => { im.onerror = null; im.src = BROKEN_IMG; };
          im.src = `/api/image?path=${encodeURIComponent(s.file)}&thumb=true`;
          im.onclick = () => openLibraryDetail(s.file);
          strip.appendChild(im);
        }
      })
      .catch(() => {});
  }

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
  const loggedIn = await checkLogin();
  try {
    if (loggedIn) {
      await loadSettings();
      await loadLang(state.settings.language);
    }
  } catch (e) {
    toast(`${t("toast.loadFailed", "불러오기 실패")}: ${e.message}`, true);
  }
  if (loggedIn) pollJobs();
})();
