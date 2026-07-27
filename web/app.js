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
  viewFmt: "readable",
  viewTable: localStorage.getItem("gensight.viewTable") === "1",
};

/* ---------------------------------------------------------- theme */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const sel = $("#themeSelect");
  if (sel) sel.value = theme;
}
applyTheme(localStorage.getItem("gensight.theme") || "dark");
$("#themeSelect").onchange = (e) => {
  localStorage.setItem("gensight.theme", e.target.value);
  applyTheme(e.target.value);
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
  initSortSelects(); // rebuild JS-generated option labels in the new language
}

/* ---------------------------------------------------------- tabs */

$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "settings") renderSettings();
    if (btn.dataset.tab === "library") loadLibrary(true);
    if (btn.dataset.tab === "stats") loadStats();
    if (btn.dataset.tab === "audit") loadAudit(true);
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
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent = "📁 " + d;
    li.appendChild(label);

    const purgeBtn = document.createElement("button");
    purgeBtn.textContent = t("settings.purgeRecords", "기록 정리");
    purgeBtn.onclick = () => {
      // Prefill the purge card rather than acting immediately — the
      // preview is what makes this safe.
      $("#purgeRoot").value = d;
      $("#purgeMode").value = "all";
      $("#purgePreview").click();
      $("#purgeRoot").scrollIntoView({ block: "center" });
    };
    const del = document.createElement("button");
    del.textContent = t("settings.remove", "삭제");
    del.onclick = async () => {
      // Unregistering leaves the catalog alone; say so with the real
      // number instead of letting the records silently linger.
      let rows = null;
      try {
        rows = (await api.get(
          `/api/library?limit=1&directory=${encodeURIComponent(d)}`)).total;
      } catch { /* fall back to the generic warning */ }
      const msg = rows
        ? t("settings.dirRemoveConfirmRows",
            "등록을 해제합니다. 이 경로의 라이브러리 기록 {n}개는 그대로 남습니다 — 계속할까요?")
            .replace("{n}", rows)
        : t("settings.dirRemoveConfirm", "이 디렉토리 등록을 해제할까요?");
      if (!confirm(msg)) return;
      try {
        await api.send("DELETE",
          `/api/settings/directories?path=${encodeURIComponent(d)}`);
        if (rows) {
          toast(t("settings.dirRemovedRecordsKept",
                  "등록 해제됨 — 기록은 남아 있습니다. '기록 정리'로 지울 수 있습니다"));
          $("#purgeRoot").value = d;
        }
        renderSettings();
      } catch (e) { toast(e.message, true); }
    };
    li.append(purgeBtn, del);
    list.appendChild(li);
  }

  $("#archiveRetention").value =
    (s.archive && s.archive.retention_days) ?? 30;

  renderWatches();
  renderGroups();
  renderTagger();
  renderQuality();
  renderArchive();
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

async function installPreset(preset) {
  try {
    const r = await api.send("POST", `/api/groups/install-preset?preset=${preset}`);
    $("#presetResult").textContent =
      `${t("settings.presetInstalled", "설치됨")}: ${r.installed.length}`;
    renderGroups();
  } catch (e) { toast(e.message, true); }
}
$("#presetStandard").onclick = () => installPreset("standard");
$("#presetExample").onclick = () => installPreset("example");

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
      ` · ${s.gpu ? "GPU" : "CPU"}` +
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
    text = `${t("status.extracting", "추출중")} ${s.job.processed}/${s.job.total}` +
      ` · ${t("settings.qualityPending", "미분석")}: ${s.pending}`;
    qualityTimer = setTimeout(renderQuality, 1500);
  } else {
    text = `${t("settings.qualityPending", "미분석")}: ${s.pending}`;
    if (s.job) {
      text += ` · ${t("settings.qualityLast", "지난 작업")}: ` +
        `${t("status." + s.job.status, s.job.status)} ` +
        `${s.job.processed}/${s.job.total}` +
        (s.job.errors ? ` (${t("status.error", "오류")} ${s.job.errors})` : "");
    }
  }
  $("#qualityStatus").textContent = text;
  if (state.settings) {
    $("#qualityAuto").checked = !!state.settings.quality?.auto;
  }
}

$("#qualityAuto").onchange = async (e) => {
  try {
    await api.send("PUT", "/api/settings", { quality: { auto: e.target.checked } });
    await loadSettings();
    toast(t("toast.saved", "설정이 저장되었습니다"));
  } catch (err2) { toast(err2.message, true); }
};

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
  if (!s.enabled) return;
  $("#authInfo").textContent =
    `${t("settings.authOn", "활성화됨")} — ${s.username} (${t("role." + s.role, s.role)})`;
  renderUsers();
}

async function renderUsers() {
  let data;
  try { data = await api.get("/api/auth/users"); } catch { return; }
  const box = $("#userList");
  box.innerHTML = "";
  for (const u of data.users) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent =
      `${u.role === "admin" ? "🛡" : "👤"} ${u.username} — ${t("role." + u.role, u.role)}`;
    const del = document.createElement("button");
    del.textContent = t("settings.remove", "삭제");
    del.onclick = async () => {
      if (!confirm(`${u.username}?`)) return;
      try {
        await api.send("DELETE", `/api/auth/users/${encodeURIComponent(u.username)}`);
        renderUsers();
      } catch (e) { toast(e.message, true); }
    };
    li.append(label, del);
    box.appendChild(li);
  }
}

$("#addUser").onclick = async () => {
  const username = $("#newUserName").value.trim();
  const password = $("#newUserPass").value;
  if (!username || password.length < 4) {
    toast(t("auth.weak", "사용자명과 4자 이상 비밀번호가 필요합니다"), true);
    return;
  }
  try {
    await api.send("POST", "/api/auth/users", {
      username, password, role: $("#newUserRole").value,
    });
    $("#newUserName").value = $("#newUserPass").value = "";
    toast(t("toast.saved", "설정이 저장되었습니다"));
    renderUsers();
  } catch (e) { toast(e.message, true); }
};

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

function renderUserChip() {
  const s = state.authStatus;
  const signedIn = !!(s && s.enabled && s.authenticated);
  $("#logoutBtn").classList.toggle("hidden", !signedIn);
  const chip = $("#userChip");
  chip.classList.toggle("hidden", !signedIn);
  chip.textContent = signedIn
    ? `👤 ${s.username} · ${t("role." + s.role, s.role)}` : "";
}

async function checkLogin() {
  try {
    const s = await api.get("/api/auth/status");
    state.authStatus = s;
    state.role = s.enabled ? s.role : "admin";
    const need = s.enabled && !s.authenticated;
    $("#loginOverlay").classList.toggle("hidden", !need);
    renderUserChip();
    if (!need) applyRole(state.role);
    return !need;
  } catch { state.role = "admin"; return true; }
}

function applyRole(role) {
  const restricted = role === "user";
  // Hide admin-only surfaces: settings tab, audit log, scanning, job list
  $('[data-tab="settings"]').classList.toggle("hidden", restricted);
  $('[data-tab="audit"]').classList.toggle("hidden", restricted);
  $("#scanCard").classList.toggle("hidden", restricted);
  $("#jobsCard").classList.toggle("hidden", restricted);
  if (restricted) {
    const active = document.querySelector(".tab.active");
    if (active && active.dataset.tab === "settings") activateTab("library");
  }
}

$("#logoutBtn").onclick = async () => {
  try { await api.send("POST", "/api/auth/logout"); } catch {}
  location.reload();
};

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

/* -------- record purge (preview -> execute) -------- */

// Holds the token from the last preview. Cleared whenever the inputs
// change so a stale plan can never be executed by accident.
let purgeToken = null;

function resetPurgePlan() {
  purgeToken = null;
  $("#purgeRun").classList.add("hidden");
}
["#purgeRoot", "#purgeMode", "#purgeRecursive"].forEach((sel) => {
  $(sel).addEventListener("input", resetPurgePlan);
  $(sel).addEventListener("change", resetPurgePlan);
});

$("#purgePreview").onclick = async () => {
  const root = $("#purgeRoot").value.trim();
  if (!root) {
    toast(t("settings.purgeNeedRoot", "정리할 경로를 입력하세요"), true);
    return;
  }
  resetPurgePlan();
  const box = $("#purgeResult");
  try {
    const p = await api.send("POST", "/api/admin/library/purge/preview", {
      root, recursive: $("#purgeRecursive").checked, mode: $("#purgeMode").value,
    });
    purgeToken = p.token;
    const risk = p.at_risk;
    const lines = [
      `${t("settings.purgeScope", "대상 경로")}: ${p.root}` +
        (p.recursive ? "" : ` (${t("settings.noRecursive", "하위 폴더 제외")})`),
      `${t("settings.purgeTargets", "정리 대상")}: ${p.targets} / ` +
        `${t("results.count", "결과")} ${p.total}`,
      `  ${t("settings.purgePresent", "파일 있음")}: ${p.present}` +
        `   ${t("settings.purgeMissing", "파일 없음")}: ${p.missing}` +
        `   ${t("settings.purgeInaccessible", "읽기 실패")}: ${p.inaccessible}`,
      `${t("settings.purgeAtRisk", "함께 사라지는 작업 결과")}:`,
      `  ${t("settings.purgeRiskTags", "태그")}: ${risk.tagged}` +
        `   ${t("settings.purgeRiskQuality", "품질")}: ${risk.quality_analysed}` +
        `   ${t("settings.purgeRiskGroup", "그룹")}: ${risk.grouped}` +
        `   ★ ${risk.rated}   ♥ ${risk.favorite}`,
      `${t("settings.purgeFilesUntouched", "삭제되는 파일")}: ${p.files_deleted}`,
    ];
    const ov = p.overlaps;
    if (ov.active_scans.length) {
      lines.push(`⚠ ${t("settings.purgeActiveScan", "이 경로에 스캔이 실행 중입니다")}`);
    }
    if (ov.watches.length) {
      lines.push(`⚠ ${t("settings.purgeWatched", "감시 중인 경로")}: ` +
        ov.watches.map((w) => w.directory).join(", "));
    }
    box.textContent = lines.join("\n");
    box.classList.remove("hidden");
    if (p.targets > 0) $("#purgeRun").classList.remove("hidden");
    else toast(t("settings.purgeNothing", "정리할 기록이 없습니다"));
  } catch (e) {
    box.classList.add("hidden");
    toast(e.message, true);
  }
};

$("#purgeRun").onclick = async () => {
  if (!purgeToken) {
    toast(t("settings.purgeNeedPreview", "먼저 미리보기를 실행하세요"), true);
    return;
  }
  if (!confirm(t("settings.purgeConfirm",
                 "미리보기 내용대로 라이브러리 기록을 정리할까요? (파일은 삭제되지 않고, 아카이브에서 복구할 수 있습니다)"))) {
    return;
  }
  try {
    const r = await api.send("POST", "/api/admin/library/purge",
                             { token: purgeToken });
    toast(`${t("settings.purgeDone", "정리된 기록")}: ${r.archived}`);
    resetPurgePlan();
    $("#purgeResult").classList.add("hidden");
    renderArchive();
  } catch (e) {
    // 409 = the plan no longer matches the library; force a re-preview.
    resetPurgePlan();
    toast(e.message, true);
  }
};

/* -------- archive -------- */

async function renderArchive() {
  let s;
  try { s = await api.get("/api/admin/library/archive"); } catch { return; }
  const parts = [`${t("settings.archiveStored", "보관 중")}: ${s.total}`];
  if (s.oldest) {
    parts.push(`${t("settings.archiveOldest", "가장 오래된 항목")}: ` +
      new Date(s.oldest * 1000).toLocaleDateString());
  }
  if (s.expired) {
    parts.push(`${t("settings.archiveExpired", "만료됨")}: ${s.expired}`);
  }
  $("#archiveStatus").textContent = parts.join(" · ");

  const box = $("#archiveBatches");
  box.innerHTML = "";
  for (const b of s.batches) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent =
      `🗄 ${new Date(b.archived_at * 1000).toLocaleString()} · ` +
      `${b.rows}${t("lib.files", "개 파일")} · ${b.archived_reason || ""}`;
    const restore = document.createElement("button");
    restore.textContent = t("lib.restore", "복구");
    restore.onclick = async () => {
      try {
        const r = await api.send("POST", "/api/admin/library/archive/restore",
                                 { batch_id: b.batch_id });
        toast(`${t("lib.restored", "복구되었습니다")}: ${r.restored}`);
        renderArchive();
      } catch (e) { toast(e.message, true); }
    };
    li.append(label, restore);
    box.appendChild(li);
  }
}

$("#archiveSaveRetention").onclick = async () => {
  try {
    await api.send("PUT", "/api/settings",
      { archive: { retention_days: +$("#archiveRetention").value } });
    toast(t("toast.saved", "설정이 저장되었습니다"));
    renderArchive();
  } catch (e) { toast(e.message, true); }
};

$("#archivePruneExpired").onclick = async () => {
  try {
    const r = await api.send("POST", "/api/admin/library/archive/prune",
                             { all: false });
    toast(`${t("lib.purged", "영구 삭제됨")}: ${r.removed}`);
    renderArchive();
  } catch (e) { toast(e.message, true); }
};

$("#archivePruneAll").onclick = async () => {
  if (!confirm(t("settings.archivePruneAllConfirm",
                 "아카이브를 전부 영구 삭제할까요? 복구할 수 없습니다."))) return;
  try {
    const r = await api.send("POST", "/api/admin/library/archive/prune",
                             { all: true });
    toast(`${t("lib.purged", "영구 삭제됨")}: ${r.removed}`);
    renderArchive();
  } catch (e) { toast(e.message, true); }
};

$("#cleanupMissing").onclick = async () => {
  try {
    const r = await api.send("POST", "/api/library/cleanup");
    toast(`${t("settings.cleanupDone", "제거된 항목")}: ${r.removed}`);
  } catch (e) { toast(e.message, true); }
};

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
  localStorage.setItem("gensight.lang", e.target.value);
  if (state.role !== "user") {
    try {
      await api.send("PUT", "/api/settings", { language: e.target.value });
    } catch { /* restricted role — local preference only */ }
  }
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
    view.onclick = () => openLibraryForDir(j.directory);
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

/* ---------------------------------------------------------- shared */

const BROKEN_IMG =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' +
    '<rect width="100" height="100" fill="#0d0f14"/>' +
    '<text x="50" y="55" font-size="30" text-anchor="middle" fill="#3a4158">🖼</text></svg>'
  );

/* ---------------------------------------------------------- library */

const lib = {
  page: 1,
  size: +localStorage.getItem("gensight.pageSize") || 50,
  dupes: false,
  trash: false,
  dir: "",
};

function openLibraryForDir(directory) {
  lib.dir = directory;
  lib.dupes = lib.trash = false;
  $("#libDupes").classList.remove("on");
  $("#libTrash").classList.remove("on");
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === "library"));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-library"));
  loadLibrary(true);
}

function updateDirChip() {
  const chip = $("#libDirChip");
  chip.classList.toggle("hidden", !lib.dir);
  chip.textContent = lib.dir ? `📁 ${lib.dir} ✕` : "";
}
$("#libDirChip").onclick = () => { lib.dir = ""; loadLibrary(true); };

/* Multi-level sort: three chained selects -> "key1,key2,key3" */
const SORT_OPTIONS = [
  ["recent", "sort.recent", "등록 최신순"],
  ["oldest", "sort.oldest", "등록 오래된순"],
  ["mtime_desc", "sort.mtimeDesc", "파일 날짜 최신순"],
  ["mtime_asc", "sort.mtimeAsc", "파일 날짜 오래된순"],
  ["rating", "sort.rating", "평점 높은순"],
  ["rating_asc", "sort.ratingAsc", "평점 낮은순"],
  ["quality", "sort.quality", "품질 낮은순"],
  ["quality_desc", "sort.qualityDesc", "품질 높은순"],
  ["name", "sort.nameAsc", "이름 ↑"],
  ["name_desc", "sort.nameDesc", "이름 ↓"],
  ["size_desc", "sort.sizeDesc", "용량 큰순"],
  ["size_asc", "sort.sizeAsc", "용량 작은순"],
];

function initSortSelects() {
  ["#libSort1", "#libSort2", "#libSort3"].forEach((id, i) => {
    const sel = $(id);
    sel.innerHTML = "";
    if (i > 0) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "—";
      sel.appendChild(none);
    }
    for (const [value, key, fallback] of SORT_OPTIONS) {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = t(key, fallback);
      sel.appendChild(o);
    }
    sel.value = localStorage.getItem(`gensight.sort${i + 1}`) ??
      (i === 0 ? "recent" : "");
    sel.onchange = () => {
      localStorage.setItem(`gensight.sort${i + 1}`, sel.value);
      loadLibrary(true);
    };
  });
}

function sortChain() {
  return ["#libSort1", "#libSort2", "#libSort3"]
    .map((id) => $(id).value)
    .filter(Boolean)
    .join(",") || "recent";
}

function libFilters() {
  const f = {
    q: $("#libSearch").value,
    tool: $("#libTool").value,
    min_rating: +$("#libRating").value,
    group: $("#libGroup").value,
    quality: $("#libQuality").value,
    content_rating: $("#libCRating").value,
    directory: lib.dir,
  };
  if ($("#libFav").classList.contains("on")) f.favorite = true;
  return f;
}

function libParams() {
  const f = libFilters();
  const p = new URLSearchParams({
    q: f.q, tool: f.tool, min_rating: f.min_rating, group: f.group,
    quality: f.quality, content_rating: f.content_rating,
    directory: f.directory, sort: sortChain(),
    offset: (lib.page - 1) * lib.size, limit: lib.size,
  });
  if (f.favorite) p.set("favorite", "true");
  return p;
}

async function loadLibrary(reset) {
  updateDirChip();
  if (lib.trash) { $("#libPager").innerHTML = ""; loadTrash(); return; }
  if (lib.dupes) { $("#libPager").innerHTML = ""; loadDuplicates(); return; }
  if (reset) lib.page = 1;
  let data;
  try {
    data = await api.get(`/api/library?${libParams()}`);
  } catch (e) { toast(e.message, true); return; }

  // The last page may vanish after deletions — snap back
  const pages = Math.max(1, Math.ceil(data.total / lib.size));
  if (lib.page > pages) { lib.page = pages; loadLibrary(false); return; }

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
  grid.innerHTML = "";
  for (const r of data.items) grid.appendChild(renderLibItem(r));
  renderPager(data.total, pages);
}

function renderPager(total, pages) {
  const pager = $("#libPager");
  pager.innerHTML = "";
  if (pages <= 1) return;

  const goto = (p) => {
    lib.page = p;
    loadLibrary(false);
    window.scrollTo({ top: 0 });
  };
  const btn = (label, page, opts = {}) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (opts.current) b.classList.add("current");
    b.disabled = !!opts.disabled;
    if (!opts.disabled && !opts.current) b.onclick = () => goto(page);
    pager.appendChild(b);
  };
  const ellipsis = () => {
    const s = document.createElement("span");
    s.className = "ellipsis";
    s.textContent = "…";
    pager.appendChild(s);
  };

  btn("«", lib.page - 1, { disabled: lib.page === 1 });
  // Window of pages around the current one, always showing first/last
  const windowPages = new Set([1, pages]);
  for (let p = lib.page - 2; p <= lib.page + 2; p++) {
    if (p >= 1 && p <= pages) windowPages.add(p);
  }
  let prev = 0;
  for (const p of [...windowPages].sort((a, b) => a - b)) {
    if (p - prev > 1) ellipsis();
    btn(String(p), p, { current: p === lib.page });
    prev = p;
  }
  btn("»", lib.page + 1, { disabled: lib.page === pages });

  const totalSpan = document.createElement("span");
  totalSpan.className = "total";
  totalSpan.textContent = `${lib.page}/${pages} · ${total}`;
  pager.appendChild(totalSpan);
}

async function loadDuplicates() {
  $("#libGrid").innerHTML = "";

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
      ${r.content_rating ? `<span class="tool-badge cr-${escapeHtml(r.content_rating).replace(/[^A-Za-z0-9]/g, "")}">${escapeHtml(r.content_rating)}</span>` : ""}
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
  if (isNsfw(r)) {
    const cover = document.createElement("div");
    cover.className = "nsfw-cover";
    cover.textContent = `🔞 ${r.content_rating} — ${t("lib.nsfwShow", "클릭하여 표시")}`;
    cover.onclick = (e) => { e.stopPropagation(); cover.remove(); };
    el.appendChild(cover);
  }
  el.onclick = () => openLibraryDetail(r.file);
  return el;
}

function isNsfw(r) {
  return r.content_rating === "R" || r.content_rating === "X";
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
$("#libGroup").onchange = $("#libCRating").onchange = () => loadLibrary(true);
initSortSelects();
$("#libPageSize").value = String(lib.size);
$("#libPageSize").onchange = (e) => {
  lib.size = +e.target.value;
  localStorage.setItem("gensight.pageSize", lib.size);
  loadLibrary(true);
};
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
$("#libExportJson").onclick = () => exportLibrary("json");
$("#libExportCsv").onclick = () => exportLibrary("csv");
function exportLibrary(format) {
  const f = libFilters();
  const p = new URLSearchParams({
    format, q: f.q, tool: f.tool, min_rating: f.min_rating,
    group: f.group, quality: f.quality, directory: f.directory,
  });
  if (f.favorite) p.set("favorite", "true");
  window.open(`/api/library/export?${p}`, "_blank");
}

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

/* ---------------------------------------------------------- audit log */

const auditView = { page: 1, size: 100 };

function auditParams() {
  return new URLSearchParams({
    action: $("#auditAction").value,
    q: $("#auditSearch").value,
    offset: (auditView.page - 1) * auditView.size,
    limit: auditView.size,
  });
}

async function loadAudit(reset) {
  if (reset) auditView.page = 1;
  renderWorkerStatus();
  let data;
  try {
    data = await api.get(`/api/audit?${auditParams()}`);
  } catch (e) { toast(e.message, true); return; }

  const sel = $("#auditAction");
  const prev = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  for (const a of data.actions) {
    const o = document.createElement("option");
    o.value = o.textContent = a;
    sel.appendChild(o);
  }
  sel.value = prev;

  $("#auditCount").textContent = `${t("results.count", "결과")}: ${data.total}`;
  const box = $("#auditList");
  box.innerHTML = "";
  if (!data.items.length) {
    box.innerHTML = `<p class="hint">${t("audit.empty", "기록이 없습니다.")}</p>`;
  }
  for (const r of data.items) {
    const el = document.createElement("div");
    el.className = "audit-row" + (r.ok ? "" : " fail");
    const when = new Date(r.ts * 1000).toLocaleString();
    el.innerHTML = `
      <span class="ts">${escapeHtml(when)}</span>
      <span class="actor">${escapeHtml(r.actor || "system")}</span>
      <span class="action">${escapeHtml(r.action)}</span>
      <span class="target" title="${escapeHtml(r.target || "")}">${escapeHtml(r.target || "")}</span>
      <span class="badge-ok">${r.ok ? "✓" : "✕"}</span>`;
    if (r.detail) {
      const d = document.createElement("span");
      d.className = "detail";
      d.textContent = typeof r.detail === "string"
        ? r.detail : JSON.stringify(r.detail);
      el.appendChild(d);
    }
    box.appendChild(el);
  }
  renderAuditPager(data.total);
}

function renderAuditPager(total) {
  const pager = $("#auditPager");
  pager.innerHTML = "";
  const pages = Math.max(1, Math.ceil(total / auditView.size));
  if (pages <= 1) return;
  const mk = (label, page, opts = {}) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (opts.current) b.classList.add("current");
    b.disabled = !!opts.disabled;
    if (!opts.disabled && !opts.current) {
      b.onclick = () => { auditView.page = page; loadAudit(false); };
    }
    pager.appendChild(b);
  };
  mk("«", auditView.page - 1, { disabled: auditView.page === 1 });
  mk(String(auditView.page), auditView.page, { current: true });
  mk("»", auditView.page + 1, { disabled: auditView.page === pages });
  const span = document.createElement("span");
  span.className = "total";
  span.textContent = `${auditView.page}/${pages} · ${total}`;
  pager.appendChild(span);
}

async function renderWorkerStatus() {
  let s;
  try { s = await api.get("/api/status/workers"); } catch { return; }
  const scan = s.scan, w = s.watcher;
  const cards = [
    [t("worker.runningJobs", "실행 중 스캔"), scan.running_jobs,
     `/ ${t("worker.max", "최대")} ${scan.max_concurrent_jobs}`],
    [t("worker.queuedJobs", "대기 중 스캔"), scan.queued_jobs, ""],
    [t("worker.extractWorkers", "활성 추출 워커"), scan.active_extract_workers, ""],
    [t("settings.watcher", "감시 상태"), w.running ? "✓" : "✗",
     w.realtime ? "watchdog + polling" : "polling"],
    [t("worker.watchPending", "감시 대기"), w.pending, ""],
  ];
  const box = $("#workerStatus");
  box.innerHTML = "";
  for (const [k, v, note] of cards) {
    const el = document.createElement("div");
    el.className = "worker-card";
    const busy = typeof v === "number" ? v > 0 : v === "✓";
    el.innerHTML = `<div class="k">${escapeHtml(k)}</div>` +
      `<div class="v ${busy ? "busy" : "idle"}">${escapeHtml(String(v))}</div>` +
      (note ? `<div class="k">${escapeHtml(note)}</div>` : "");
    box.appendChild(el);
  }
  if (w.last_error) {
    const el = document.createElement("div");
    el.className = "worker-card";
    el.innerHTML = `<div class="k">${t("status.error", "오류")}</div>` +
      `<div class="k">${escapeHtml(w.last_error)}</div>`;
    box.appendChild(el);
  }
}

$("#auditRefresh").onclick = () => loadAudit(true);
$("#auditAction").onchange = () => loadAudit(true);
let auditTimer;
$("#auditSearch").oninput = () => {
  clearTimeout(auditTimer);
  auditTimer = setTimeout(() => loadAudit(true), 300);
};
$("#auditExport").onclick = () => {
  const p = new URLSearchParams({
    action: $("#auditAction").value, q: $("#auditSearch").value,
  });
  window.open(`/api/audit/export?${p}`, "_blank");
};

/* ---------------------------------------------------------- detail modal */

function openDetailData(r, opts = {}) {
  state.detail = r;
  $("#modalTitle").textContent = r.original_name || r.filename;
  const img = $("#modalImg");
  img.onerror = () => { img.onerror = null; img.src = BROKEN_IMG; };
  img.src = `/api/image?path=${encodeURIComponent(r.file)}`;
  img.classList.toggle("nsfw-blur", isNsfw(r));
  img.onclick = isNsfw(r) ? () => img.classList.remove("nsfw-blur") : null;
  renderModalMeta("readable");

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
  if (r.content_rating) issues.unshift(`🔞 ${r.content_rating}`);
  const chips = [...issues, ...tags];
  tagsBox.classList.toggle("hidden", !chips.length);
  tagsBox.innerHTML = chips.map((x) => `<span>${escapeHtml(x)}</span>`).join("");

  // Danbooru-style prompt from tags (WD Tagger output is already
  // danbooru vocabulary — usable directly as a generation prompt)
  const copyTags = $("#copyTagsBtn");
  copyTags.classList.toggle("hidden", !tags.length);
  copyTags.onclick = () =>
    copyText(tags.map((x) => x.replace(/^character:/, "")).join(", "));

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
    const fmt = b.dataset.copy;
    const text = formatResult(state.detail, fmt);
    renderModalMeta(fmt);
    const withImage = $("#copyWithImage").checked;
    if (fmt === "arca") {
      await copyArca(state.detail, text, withImage);
    } else if (withImage) {
      await copyWithImage(state.detail, text);
    } else {
      copyText(text);
    }
  };
});

async function copyArca(r, text, withImage) {
  let dataUrl = null;
  if (withImage) {
    try {
      dataUrl = (await fetchImageDataUrl(r)).dataUrl;
    } catch { /* fall through: metadata still copies */ }
  }
  const html = arcaHtml(r, dataUrl);
  try {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }),
      }),
    ]);
    toast(t("toast.copiedArca", "아카라이브 형식으로 복사되었습니다"));
  } catch {
    // Clipboard HTML needs a secure context (localhost/HTTPS).
    copyText(text);
  }
}

$("#viewTable").onclick = () => {
  state.viewTable = !state.viewTable;
  localStorage.setItem("gensight.viewTable", state.viewTable ? "1" : "0");
  renderModalMeta(state.viewFmt);
};

/* -------- metadata rendering: syntax highlight + table view -------- */

function jsonObject(r) {
  return {
    prompt: normalizeText(r.prompt),
    negative_prompt: normalizeText(r.negative_prompt),
    ...orderedParams(r),
  };
}

function highlightJsonFlat(obj) {
  const entries = Object.entries(obj);
  let out = "{\n";
  entries.forEach(([k, v], i) => {
    const key = `<span class="syn-key">${escapeHtml(JSON.stringify(k))}</span>`;
    const raw = JSON.stringify(v);
    const cls = typeof v === "number" ? "syn-num" : "syn-str";
    out += `  ${key}: <span class="${cls}">${escapeHtml(raw)}</span>` +
      (i < entries.length - 1 ? "," : "") + "\n";
  });
  return out + "}";
}

function highlightReadable(text) {
  return escapeHtml(text).split("\n").map((line) => {
    if (/^(Prompt|Negative prompt):$/.test(line)) {
      return `<span class="syn-head">${line}</span>`;
    }
    const m = line.match(/^([A-Za-z][\w .+]*): (.*)$/);
    if (m) {
      const cls = /^-?[\d.]+(x[\d.]+)?$/.test(m[2]) ? "syn-num" : "syn-str";
      return `<span class="syn-key">${m[1]}</span>: <span class="${cls}">${m[2]}</span>`;
    }
    return line;
  }).join("\n");
}

/* Arcalive's post editor is a WYSIWYG that pastes text/html, so this
   builds self-contained markup: no classes, no external CSS, inline
   styles only, and a <table> the editor keeps intact. */
function arcaHtml(r, imageDataUrl) {
  const prompt = normalizeText(r.prompt);
  const negative = normalizeText(r.negative_prompt);
  const box = "border:1px solid #d0d0d0;background:#fafafa;padding:8px;" +
    "white-space:pre-wrap;word-break:break-word;font-family:monospace;" +
    "font-size:13px;";
  let html = "<div>";
  if (imageDataUrl) {
    html += `<p><img src="${imageDataUrl}" alt="${escapeHtml(r.filename)}"></p>`;
  }
  html += `<p><b>Prompt</b></p><div style="${box}">${escapeHtml(prompt || "—")}</div>`;
  if (negative) {
    html += `<p><b>Negative prompt</b></p>` +
      `<div style="${box}">${escapeHtml(negative)}</div>`;
  }
  const params = orderedParams(r);
  if (Object.keys(params).length) {
    html += '<p><b>Settings</b></p>' +
      '<table style="border-collapse:collapse;font-size:13px;">';
    const cell = "border:1px solid #d0d0d0;padding:3px 8px;";
    for (const [k, v] of Object.entries(params)) {
      html += `<tr><td style="${cell}background:#f0f0f0;"><b>${escapeHtml(k)}</b></td>` +
        `<td style="${cell}">${escapeHtml(String(v))}</td></tr>`;
    }
    html += "</table>";
  }
  return html + "</div>";
}

function metaTableHtml(r) {
  let rows = `<tr><th class="syn-head">Prompt</th>` +
    `<td class="prompt-cell">${escapeHtml(normalizeText(r.prompt) || "—")}</td></tr>`;
  if (r.negative_prompt) {
    rows += `<tr><th class="syn-head">Negative</th>` +
      `<td class="prompt-cell">${escapeHtml(normalizeText(r.negative_prompt))}</td></tr>`;
  }
  for (const [k, v] of Object.entries(orderedParams(r))) {
    rows += `<tr><th>${escapeHtml(k)}</th><td>${escapeHtml(String(v))}</td></tr>`;
  }
  return `<table class="meta-table">${rows}</table>`;
}

function renderModalMeta(fmt) {
  state.viewFmt = fmt;
  $("#viewTable").classList.toggle("on", state.viewTable);
  const r = state.detail;
  if (!r) return;
  const box = $("#modalMeta");
  if (state.viewTable) {
    box.innerHTML = metaTableHtml(r);
  } else if (fmt === "json") {
    box.innerHTML = highlightJsonFlat(jsonObject(r));
  } else if (fmt === "readable") {
    box.innerHTML = highlightReadable(formatResult(r, "readable"));
  } else {
    box.textContent = formatResult(r, fmt);
  }
}

/* -------- copy with image (original, thumbnail fallback) -------- */

// data-URL embedding above this size makes clipboard/paste unusable
const MAX_COPY_ORIGINAL_BYTES = 30 * 1024 * 1024;

async function fetchDataUrl(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  const dataUrl = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = reject;
    fr.readAsDataURL(blob);
  });
  return { dataUrl, size: blob.size };
}

/** Original if it is small enough to embed, thumbnail otherwise. */
async function fetchImageDataUrl(r) {
  try {
    const img = await fetchDataUrl(
      `/api/image?path=${encodeURIComponent(r.file)}`);
    if (img.size <= MAX_COPY_ORIGINAL_BYTES) return img;
  } catch { /* fall through to the thumbnail */ }
  return fetchDataUrl(
    `/api/image?path=${encodeURIComponent(r.file)}&thumb=true`);
}

async function copyWithImage(r, text) {
  try {
    const img = await fetchImageDataUrl(r);
    const html =
      `<div><img src="${img.dataUrl}" alt="${escapeHtml(r.filename)}"><br>` +
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
  if (fmt === "arca") {
    // Plain-text fallback; the HTML flavour is what the editor uses.
    let s = `${prompt}\n`;
    if (negative) s += `\n[Negative] ${negative}\n`;
    s += "\n";
    for (const [k, v] of Object.entries(params)) s += `${k}: ${v}\n`;
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
    .catch((e) => {
      // The clipboard API needs a focused document and a secure context
      // (localhost or HTTPS) — say which, instead of "copy failed".
      const why = e && e.name === "NotAllowedError"
        ? t("toast.copyBlocked",
            "브라우저가 클립보드 접근을 막았습니다 (창 포커스 및 localhost/HTTPS 필요)")
        : (e && e.message) || "";
      toast(`${t("toast.copyFailed", "복사 실패")}${why ? ": " + why : ""}`, true);
    });
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

function activateTab(name) {
  const btn = $$(".tab").find((b) => b.dataset.tab === name);
  if (btn) btn.click();
}

(async function init() {
  const loggedIn = await checkLogin();
  try {
    if (loggedIn && state.role !== "user") {
      await loadSettings();
      await loadLang(state.settings.language);
    } else {
      // Restricted users cannot read settings — use the local choice
      await loadLang(localStorage.getItem("gensight.lang") || "ko");
    }
  } catch (e) {
    toast(`${t("toast.loadFailed", "불러오기 실패")}: ${e.message}`, true);
  }
  renderUserChip(); // re-render with the now-loaded i18n strings
  if (loggedIn) {
    if (state.role !== "user") pollJobs();
    // Deep links: /#library /#stats /#settings and ?detail=<path>
    const hashTab = location.hash.replace("#", "");
    if (["scan", "library", "stats", "settings", "audit"].includes(hashTab)) {
      activateTab(hashTab);
    }
    const detailPath = new URLSearchParams(location.search).get("detail");
    if (detailPath) {
      activateTab("library");
      openLibraryDetail(detailPath);
    }
  }
})();
