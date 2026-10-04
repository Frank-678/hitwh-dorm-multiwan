const state = {
  paused: false,
  timer: null,
  payload: null,
};

const colors = ["#2563eb", "#0f766e", "#7c3aed", "#c2410c", "#0369a1", "#a21caf", "#4d7c0f"];

const $ = (selector) => document.querySelector(selector);

function formatRate(bytesPerSecond = 0) {
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let value = Math.max(0, bytesPerSecond);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

function formatBytes(bytes = 0) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Math.max(0, bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function formatDuration(seconds = 0) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setConnection(stateName, text) {
  const pill = $("#connection-pill");
  pill.dataset.state = stateName;
  pill.textContent = text;
}

function render(payload) {
  state.payload = payload;
  const sample = payload.sample;
  $("#router-target").textContent = payload.target || "—";
  $("#polling-note").textContent = `每 ${payload.interval_seconds || 2} 秒刷新`;

  if (payload.state === "connected") setConnection("connected", "已连接");
  else if (payload.state === "disconnected") setConnection("disconnected", "连接失败");
  else if (payload.state === "idle") setConnection("idle", "已暂停");
  else setConnection("connecting", "正在连接");

  const errorBox = $("#error-box");
  if (payload.error) {
    errorBox.hidden = false;
    errorBox.textContent = `SSH：${payload.error}`;
  } else {
    errorBox.hidden = true;
  }

  if (!sample) {
    $("#path-list").innerHTML = '<div class="empty-paths">等待路由器返回首个样本</div>';
    drawChart([]);
    return;
  }

  const active = sample.paths.filter((path) => path.state === "active").length;
  $("#total-download").textContent = formatRate(sample.total.rx_bytes_per_second);
  $("#total-upload").textContent = formatRate(sample.total.tx_bytes_per_second);
  $("#active-paths").textContent = `${active} / ${sample.paths.length} 条线路在线`;
  $("#cpu-usage").textContent = `${sample.cpu_percent.toFixed(1)}%`;
  $("#load-average").textContent = `负载 ${Number(sample.load[0] || 0).toFixed(2)}`;
  $("#conntrack-count").textContent = Number(sample.conntrack.count || 0).toLocaleString("zh-CN");
  $("#conntrack-note").textContent = `占用 ${sample.conntrack.used_percent.toFixed(1)}%`;
  $("#memory-usage").textContent = `${sample.memory_used_percent.toFixed(1)}%`;
  $("#uptime").textContent = formatDuration(sample.uptime_seconds);
  $("#updated-at").textContent = `更新于 ${new Date(sample.timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false })}`;

  renderPaths(sample.paths, sample.total.rx_bytes_per_second);
  drawChart(payload.history || []);
}

function renderPaths(paths, totalRx) {
  const list = $("#path-list");
  if (!paths.length) {
    list.innerHTML = '<div class="empty-paths">没有可显示的线路</div>';
    return;
  }

  list.innerHTML = paths.map((path, index) => {
    const share = totalRx > 0 ? Math.min(100, path.rx_bytes_per_second / totalRx * 100) : 0;
    const color = colors[index % colors.length];
    const stateClass = path.state === "active" ? "active" : "inactive";
    const stateText = path.state === "active" ? "在线" : path.state === "inactive" ? "离线" : path.state;
    return `
      <article class="path-row">
        <div class="path-main">
          <span class="path-state ${stateClass}" aria-label="${escapeHtml(stateText)}"></span>
          <div>
            <div class="path-name">${escapeHtml(path.interface)} · ${escapeHtml(stateText)}</div>
            <div class="path-address">${escapeHtml(path.ip)} · ${escapeHtml(path.mac)}</div>
          </div>
        </div>
        <div>
          <div class="share-track"><div class="share-fill" style="width:${share.toFixed(1)}%;background:${color}"></div></div>
          <div class="share-text">当前下载占比 ${share.toFixed(1)}%</div>
        </div>
        <div class="path-numbers">
          <div class="path-number"><span>下载</span><strong>${formatRate(path.rx_bytes_per_second)}</strong></div>
          <div class="path-number"><span>上传</span><strong>${formatRate(path.tx_bytes_per_second)}</strong></div>
          <div class="path-number"><span>本次累计</span><strong>${formatBytes(path.rx_bytes_since_view)}</strong></div>
        </div>
      </article>`;
  }).join("");
}

function drawChart(history) {
  const canvas = $("#traffic-chart");
  const empty = $("#chart-empty");
  const legend = $("#chart-legend");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  ctx.clearRect(0, 0, width, height);

  if (history.length < 2) {
    empty.hidden = false;
    legend.innerHTML = "";
    return;
  }
  empty.hidden = true;

  const names = [...new Set(history.flatMap((point) => Object.keys(point.paths || {})))];
  const series = [
    { name: "总下载", color: "#172033", values: history.map((point) => point.rx_bytes_per_second || 0), width: 2.4 },
    ...names.map((name, index) => ({
      name,
      color: colors[index % colors.length],
      values: history.map((point) => point.paths?.[name] || 0),
      width: 1.6,
    })),
  ];
  legend.innerHTML = series.map((item) => `<span class="legend-item"><i class="legend-dot" style="background:${item.color}"></i>${escapeHtml(item.name)}</span>`).join("");

  const padding = { top: 12, right: 8, bottom: 26, left: 58 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const maxValue = Math.max(1024, ...series.flatMap((item) => item.values));
  const niceMax = Math.ceil(maxValue / (1024 * 1024)) * 1024 * 1024 || 1024 * 1024;

  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const y = padding.top + plotHeight * i / 4;
    const value = niceMax * (1 - i / 4);
    ctx.strokeStyle = "#e8edf3";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    ctx.fillStyle = "#64748b";
    ctx.fillText(formatRate(value), padding.left - 9, y);
  }

  series.forEach((item) => {
    ctx.beginPath();
    item.values.forEach((value, index) => {
      const x = padding.left + plotWidth * index / Math.max(1, item.values.length - 1);
      const y = padding.top + plotHeight * (1 - value / niceMax);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = item.color;
    ctx.lineWidth = item.width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  });

  ctx.fillStyle = "#64748b";
  ctx.textBaseline = "bottom";
  ctx.textAlign = "left";
  ctx.fillText(new Date(history[0].timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false, minute: "2-digit", second: "2-digit" }), padding.left, height);
  ctx.textAlign = "right";
  ctx.fillText("现在", width - padding.right, height);
}

async function poll() {
  if (state.paused || document.hidden) return;
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    setConnection("disconnected", "本地服务异常");
    const box = $("#error-box");
    box.hidden = false;
    box.textContent = String(error);
  } finally {
    clearTimeout(state.timer);
    if (!state.paused && !document.hidden) state.timer = setTimeout(poll, 2000);
  }
}

function pauseSampling() {
  fetch("/api/pause", { method: "POST", keepalive: true }).catch(() => {});
}

async function reconnectPaths() {
  const button = $("#reconnect-button");
  const status = $("#refresh-status");
  button.disabled = true;
  button.textContent = "重连中…";
  status.dataset.state = "";
  status.textContent = "正在检查并重连离线线路";
  try {
    const response = await fetch("/api/reconnect", {
      method: "POST",
      headers: { "X-Dashboard-Action": "reconnect-paths" },
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || `HTTP ${response.status}`);
    status.dataset.state = "success";
    status.textContent = result.message;
    if (!state.paused) await poll();
  } catch (error) {
    status.dataset.state = "error";
    status.textContent = `重连失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    button.disabled = false;
    button.textContent = "重连离线线路";
  }
}

$("#reconnect-button").addEventListener("click", reconnectPaths);

$("#pause-button").addEventListener("click", () => {
  state.paused = !state.paused;
  $("#pause-button").textContent = state.paused ? "继续" : "暂停";
  if (state.paused) {
    clearTimeout(state.timer);
    pauseSampling();
    setConnection("idle", "已暂停");
  } else {
    poll();
  }
});

document.addEventListener("visibilitychange", () => {
  clearTimeout(state.timer);
  if (document.hidden) pauseSampling();
  else if (!state.paused) poll();
});

window.addEventListener("pagehide", pauseSampling);

window.addEventListener("resize", () => {
  if (state.payload) drawChart(state.payload.history || []);
});

poll();
