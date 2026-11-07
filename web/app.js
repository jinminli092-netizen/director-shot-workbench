const $ = (selector) => document.querySelector(selector);
const state = { media: [], selected: null, busy: false };

function fmt(seconds) {
  return `${Number(seconds || 0).toFixed(1)}s`;
}

function statusText(job) {
  if (!job) return "未开始";
  const labels = { pending: "待分析", running: "处理中", done: "已完成", failed: "失败" };
  return `${labels[job.status] || job.status}${job.progress ? ` · ${job.progress}%` : ""}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || `请求失败 (${response.status})`);
  return data;
}

function renderList() {
  const list = $("#list");
  if (!state.media.length) {
    list.innerHTML = '<p class="muted">还没有视频。</p>';
    return;
  }
  list.innerHTML = state.media.map((item) => {
    const thumb = item.thumb_path ? `/media/${item.id}/${item.thumb_path}` : "";
    const active = state.selected?.id === item.id ? " active" : "";
    const job = item.job || { status: item.analysis_status, progress: item.analysis_progress };
    return `<button class="item${active}" data-id="${item.id}">
      ${thumb ? `<img src="${thumb}" alt="" loading="lazy">` : '<span class="empty">—</span>'}
      <span><strong>${escapeHtml(item.title)}</strong><small>${item.shot_count || 0} 镜 · ${fmt(item.duration_seconds)}</small>
      <small>${statusText(job)}</small>
      ${job.status === "running" ? `<span class="progress"><i style="width:${job.progress || 0}%"></i></span>` : ""}</span>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-id]").forEach((node) => node.addEventListener("click", () => select(node.dataset.id)));
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function analysisLine(label, value) {
  return value ? `<p><span class="dim">${label}：</span>${escapeHtml(value)}</p>` : "";
}

function renderDetail(item) {
  state.selected = item;
  const detail = $("#detail");
  if (!item) {
    detail.innerHTML = '<p class="muted">选择左侧一条视频，查看分镜和分析。</p>';
    return;
  }
  const shots = item.shots || [];
  detail.innerHTML = `<div class="detail-head">
    <div><h2>${escapeHtml(item.title)}</h2><span class="muted">${shots.length} 镜 · ${fmt(item.duration_seconds)} · ${statusText(item.job)}</span></div>
    <div class="detail-actions">
      <button class="button" id="cut">重新切镜</button>
      <button class="button primary" id="analyze" ${item.job?.status === "running" ? "disabled" : ""}>逐镜分析</button>
    </div>
  </div>
  ${item.job?.error ? `<p class="status error">${escapeHtml(item.job.error)}</p>` : ""}
  <div class="shots">${shots.map((shot) => {
    const image = shot.thumb_path ? `/media/${item.id}/${shot.thumb_path}` : "";
    const a = shot.analysis || {};
    return `<article class="shot">
      ${image ? `<img src="${image}" alt="第${shot.index_no}镜" loading="lazy">` : ""}
      <div class="body"><span class="time">第 ${shot.index_no} 镜 · ${fmt(shot.start_seconds)}–${fmt(shot.end_seconds)}</span>
        <div class="meta">${escapeHtml(a.size || "未分析")}${a.move ? ` · ${escapeHtml(a.move)}` : ""}</div>
        ${analysisLine("主体", a.subject)}${analysisLine("走位", a.blocking)}${analysisLine("微表情", a.micro_expression)}
        ${analysisLine("内容", a.content)}${analysisLine("为什么", a.why_note || a.why)}
      </div>
    </article>`;
  }).join("")}</div>`;
  $("#cut").onclick = () => recut(item.id);
  $("#analyze").onclick = () => analyze(item.id);
}

async function refresh(selectId) {
  const data = await api("/api/media");
  state.media = data.media || [];
  renderList();
  if (selectId) {
    const item = await api(`/api/media/${selectId}`);
    renderDetail(item.media);
  } else if (state.selected) {
    const item = await api(`/api/media/${state.selected.id}`);
    renderDetail(item.media);
  }
}

async function select(id) {
  const data = await api(`/api/media/${id}`);
  renderDetail(data.media);
  renderList();
}

async function upload(file) {
  if (!file || state.busy) return;
  state.busy = true;
  const status = $("#status");
  status.className = "status";
  status.textContent = `上传 ${file.name}…`;
  try {
    const data = await api("/api/media/upload", {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    });
    status.textContent = `已上传 ${file.name}，正在切镜…`;
    await refresh(data.media.id);
  } catch (error) {
    status.className = "status error";
    status.textContent = error.message;
  } finally {
    state.busy = false;
  }
}

async function recut(id) {
  try {
    await api(`/api/media/${id}/cut`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: $("#mode").value }) });
    await refresh(id);
  } catch (error) {
    alert(error.message);
  }
}

async function analyze(id) {
  try {
    await api(`/api/media/${id}/analyze`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    await refresh(id);
  } catch (error) {
    alert(error.message);
  }
}

async function boot() {
  try {
    const health = await api("/api/health");
    $("#model-status").textContent = health.model ? "视觉模型已配置" : "未配置视觉模型 · 仍可切镜";
  } catch {
    $("#model-status").textContent = "服务未连接";
  }
  await refresh();
  const initialId = new URLSearchParams(window.location.search).get("id");
  if (initialId) await select(initialId);
  setInterval(() => {
    if (state.media.some((item) => ["running", "pending"].includes(item.job?.status))) refresh();
  }, 2500);
}

$("#file").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  event.target.value = "";
  upload(file);
});
$("#refresh").addEventListener("click", () => refresh());
boot();
