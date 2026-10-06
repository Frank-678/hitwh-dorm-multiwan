const state = {
  paused: false,
  timer: null,
  payload: null,
  busy: false,
  previousRaw: null,
  history: [],
  totals: {},
};

const colors = ["#2563eb", "#0f766e", "#7c3aed", "#c2410c", "#0369a1", "#a21caf", "#4d7c0f"];

const $ = (selector) => document.querySelector(selector);

const authMessages = {
  missing_credentials: "未配置凭据", no_dhcp: "未获得 DHCP 地址", mac_mismatch: "MAC 已改变，请重新保存凭据",
  insecure_storage: "凭据文件权限不安全", captcha_required: "门户需要验证码", encryption_required: "门户要求尚未支持的密码加密",
  service_required: "门户要求选择服务", network_error: "网络请求失败", portal_not_detected: "未发现配置的可信认证门户",
  authentication_failed: "认证失败，请核对凭据和账号设备数量限制", verification_failed: "认证后仍未通过 204 检查",
  address_changed: "DHCP 地址已改变，请再次手动刷新", invalid_credentials: "账号或密码无效",
  invalid_interface: "不是受管线路", helper_unavailable: "认证组件未安装"
};

function resultMessage(result) {
  const errors = (result.details || []).filter(item => authMessages[item.code]).map(item => `${item.interface}：${authMessages[item.code]}`);
  if (result.retained) return `${result.interface || "新线路"} 已保留，当前离线。${errors.join("；") || "请修正凭据后手动刷新"}`;
  if (errors.length) return errors.join("；");
  if (Number.isInteger(result.attempted)) return result.attempted === 0 ? "线路已在线，无需认证" : `已恢复 ${result.recovered}/${result.attempted} 条线路`;
  return result.message || (result.ok ? "操作完成" : "操作失败，请检查线路状态");
}

function nativePayload(result) {
  const raw = result.raw, previous = state.previousRaw;
  const dt = previous ? Math.max(0.25, raw.clock - previous.clock) : 0;
  const deltas = raw.paths.map(path => {
    const old = previous?.paths.find(item => item.interface === path.interface && item.mac === path.mac && item.device === path.device);
    return {path,rx:old ? Math.max(0,path.rx_counter-old.rx_counter) : 0,tx:old ? Math.max(0,path.tx_counter-old.tx_counter) : 0};
  });
  const virtualRx = deltas.filter(d => !d.path.is_main).reduce((sum,d) => sum+d.rx,0);
  const virtualTx = deltas.filter(d => !d.path.is_main).reduce((sum,d) => sum+d.tx,0);
  const paths = deltas.map(d => {
    const rx = d.path.is_main && d.rx >= virtualRx ? d.rx - virtualRx : d.rx;
    const tx = d.path.is_main && d.tx >= virtualTx ? d.tx - virtualTx : d.tx;
    const total = state.totals[d.path.interface] ||= {rx:0,tx:0}; total.rx+=rx; total.tx+=tx;
    return {...d.path,rx_bytes_per_second:dt ? rx/dt : 0,tx_bytes_per_second:dt ? tx/dt : 0,rx_bytes_since_view:total.rx};
  });
  let cpu = 0;
  if (previous) {
    const sum = a => a.slice(0,8).reduce((s,v) => s+v,0);
    const elapsed = sum(raw.cpu)-sum(previous.cpu);
    const idle = (raw.cpu[3]||0)+(raw.cpu[4]||0)-(previous.cpu[3]||0)-(previous.cpu[4]||0);
    cpu = elapsed > 0 ? Math.max(0,Math.min(100,100*(elapsed-idle)/elapsed)) : 0;
  }
  const sample = {timestamp:raw.timestamp,paths,cpu_percent:cpu,memory_used_percent:raw.memory_used_percent,load:raw.load,
    conntrack:{...raw.conntrack,used_percent:raw.conntrack.max ? 100*raw.conntrack.count/raw.conntrack.max : 0},
    total:{rx_bytes_per_second:paths.reduce((s,p)=>s+p.rx_bytes_per_second,0),tx_bytes_per_second:paths.reduce((s,p)=>s+p.tx_bytes_per_second,0)}};
  state.previousRaw = raw;
  state.nativeSettings = result.settings;
  state.history.push({timestamp:raw.timestamp,rx_bytes_per_second:sample.total.rx_bytes_per_second,
    paths:Object.fromEntries(paths.map(p=>[p.interface,p.rx_bytes_per_second]))});
  if (state.history.length > 180) state.history.shift();
  return {state:"connected",sample,history:state.history,interval_seconds:2};
}

function operationBusy(busy) {
  state.busy = busy;
  for (const button of document.querySelectorAll("#add-button, #settings-button, #reconnect-button, .path-actions button")) button.disabled = busy;
}

async function perform(path, values) {
  const response = await MwanAPI.request(path, {method:"POST",headers:{"Content-Type":"application/json","X-Dashboard-Action":"manage-paths"},body:JSON.stringify(values)});
  const result = await response.json();
  if (!response.ok && !result.retained) throw new Error(resultMessage(result));
  return result;
}

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
  $("#polling-note").textContent = `每 ${payload.interval_seconds || 2} 秒刷新`;

  if (payload.state === "connected") setConnection("connected", "已连接");
  else if (payload.state === "disconnected") setConnection("disconnected", "连接失败");
  else if (payload.state === "idle") setConnection("idle", "已暂停");
  else setConnection("connecting", "正在连接");

  const errorBox = $("#error-box");
  if (payload.error) {
    errorBox.hidden = false;
    errorBox.textContent = payload.error;
  } else {
    errorBox.hidden = true;
  }

  if (!sample) {
    $("#path-list").innerHTML = '<div class="empty-paths">正在读取路由器状态</div>';
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
  $("#updated-at").textContent = `更新于 ${new Date(sample.timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false })}`;

  renderPaths(sample.paths, sample.total.rx_bytes_per_second);
  drawChart(payload.history || []);
}

function renderPaths(paths, totalRx) {
  const list = $("#path-list");
  if (!paths.length) {
    list.innerHTML = '<div class="empty-paths"><strong>还没有配置线路</strong><p>点击“新增线路”，使用校园网账号认证一个随机 MAC，或添加已认证的 MAC。</p></div>';
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
        <div class="path-actions">
          <button class="credentials-button" type="button" data-interface="${escapeHtml(path.interface)}" ${state.busy ? 'disabled' : ''}>${path.credentials_saved ? "查看凭据" : "添加凭据"}</button>
          <button class="path-refresh" type="button" data-interface="${escapeHtml(path.interface)}" ${state.busy ? 'disabled' : ''}>刷新</button>
          <button class="path-edit" type="button" data-interface="${escapeHtml(path.interface)}" ${state.busy ? 'disabled' : ''}>修改 MAC</button>
          <button class="path-remove" type="button" data-interface="${escapeHtml(path.interface)}" ${state.busy ? 'disabled' : ''}>删除线路</button>
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
    const response = await MwanAPI.request("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    render(result.raw ? nativePayload(result) : result);
  } catch (error) {
    setConnection("disconnected", "连接失败");
    const box = $("#error-box");
    box.hidden = false;
    box.textContent = String(error);
  } finally {
    clearTimeout(state.timer);
    if (!state.paused && !document.hidden) state.timer = setTimeout(poll, 2000);
  }
}

async function reconnectPaths(interfaceName = "") {
  const button = $("#reconnect-button");
  const status = $("#refresh-status");
  button.disabled = true;
  operationBusy(true);
  button.textContent = "刷新中…";
  status.dataset.state = "";
  status.textContent = "正在检查线路、恢复 DHCP 并认证离线出口";
  try {
    const response = await MwanAPI.request("/api/reconnect", {
      method: "POST",
      headers: { "X-Dashboard-Action": "reconnect-paths", "Content-Type": "application/json" },
      body: JSON.stringify({interface: interfaceName}),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(resultMessage(result));
    status.dataset.state = "success";
    status.textContent = resultMessage(result);
    if (!state.paused) await poll();
  } catch (error) {
    status.dataset.state = "error";
    status.textContent = `刷新失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    button.disabled = false;
    button.textContent = "刷新离线线路";
    operationBusy(false);
  }
}

$("#reconnect-button").addEventListener("click", () => reconnectPaths());

let credentialInterface = "";
let credentialGeneration = 0;

async function credentialRequest(action, values = {}) {
  const response = await MwanAPI.request("/api/credentials", {
    method: "POST", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Dashboard-Action": "credentials" },
    body: JSON.stringify({ action, interface: credentialInterface, ...values }),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(resultMessage(result));
  return result;
}

function credentialBusy(busy) {
  for (const id of ["credential-username", "credential-password", "credentials-save", "credentials-remove"]) $("#" + id).disabled = busy;
}

function credentialMessage(message, error = false) {
  $("#credentials-message").textContent = message;
  $("#credentials-message").dataset.state = error ? "error" : "";
}

$("#path-list").addEventListener("click", async (event) => {
  const button = event.target.closest(".credentials-button");
  if (!button) return;
  const dialog = $("#credentials-dialog");
  $("#credentials-form").reset();
  $("#credential-password").type = "password";
  $("#credentials-remove").hidden = true;
  credentialInterface = button.dataset.interface;
  const generation = ++credentialGeneration;
  $("#credentials-title").textContent = `${credentialInterface} · 认证凭据`;
  credentialMessage("正在读取凭据…");
  credentialBusy(true);
  dialog.showModal();
  try {
    const result = await credentialRequest("get");
    if (generation !== credentialGeneration || !dialog.open) return;
    $("#credential-username").value = result.username;
    $("#credential-password").value = result.password;
    $("#credentials-remove").hidden = !result.configured;
    const path = state.payload?.sample?.paths.find((item) => item.interface === credentialInterface);
    credentialMessage(result.configured
      ? (result.mac && path?.mac && result.mac !== path.mac ? "线路 MAC 已改变，请重新保存以更新绑定。" : "已读取保存的凭据，可修改后保存。")
      : "尚未配置凭据，请输入账号和密码。");
  } catch (error) {
    if (generation === credentialGeneration && dialog.open) credentialMessage(error.message || "无法读取凭据", true);
  } finally {
    if (generation === credentialGeneration && dialog.open) {
      credentialBusy(false);
      $("#credential-username").focus();
    }
  }
});

$("#credential-visible").addEventListener("change", (event) => {
  $("#credential-password").type = event.target.checked ? "text" : "password";
});
function clearCredentialFields() {
  $("#credentials-form").reset();
  $("#credential-password").value = "";
  $("#credential-password").type = "password";
}
function closeCredentials() {
  clearCredentialFields();
  $("#credentials-dialog").close();
}
for (const id of ["credentials-close", "credentials-cancel"]) $("#" + id).addEventListener("click", closeCredentials);
$("#credentials-dialog").addEventListener("cancel", clearCredentialFields);
$("#credentials-dialog").addEventListener("close", () => {
  ++credentialGeneration;
  const previousInterface = credentialInterface;
  credentialInterface = "";
  clearCredentialFields();
  credentialMessage("");
  const button = [...document.querySelectorAll(".credentials-button")].find((item) => item.dataset.interface === previousInterface);
  button?.focus();
});
$("#credentials-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const generation = credentialGeneration;
  credentialBusy(true);
  credentialMessage("正在保存…");
  try {
    await credentialRequest("save", { username: $("#credential-username").value, password: $("#credential-password").value });
    if (generation !== credentialGeneration) return;
    const path = state.payload?.sample?.paths.find((item) => item.interface === credentialInterface);
    if (path) { path.credentials_saved = true; renderPaths(state.payload.sample.paths, state.payload.sample.total.rx_bytes_per_second); }
    closeCredentials();
    $("#refresh-status").textContent = "凭据已保存，点击该线路的“刷新”进行认证";
    $("#refresh-status").dataset.state = "success";
  } catch (error) {
    if (generation === credentialGeneration) credentialMessage(error.message || "保存失败", true);
  } finally { if (generation === credentialGeneration) credentialBusy(false); }
});
$("#credentials-remove").addEventListener("click", async () => {
  const generation = credentialGeneration;
  credentialBusy(true);
  try {
    await credentialRequest("remove");
    if (generation !== credentialGeneration) return;
    const path = state.payload?.sample?.paths.find((item) => item.interface === credentialInterface);
    if (path) { path.credentials_saved = false; renderPaths(state.payload.sample.paths, state.payload.sample.total.rx_bytes_per_second); }
    closeCredentials();
  } catch (error) {
    if (generation === credentialGeneration) credentialMessage(error.message || "删除失败", true);
  } finally { if (generation === credentialGeneration) credentialBusy(false); }
});

document.addEventListener("visibilitychange", () => {
  clearTimeout(state.timer);
  if (!document.hidden) poll();
});

for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $("#" + button.dataset.close).close());
for (const id of ['add-dialog','path-dialog','settings-dialog']) $("#"+id).addEventListener('close', () => {
  $("#"+id).querySelector('form').reset();
  for (const input of $("#"+id).querySelectorAll('input[type="password"], #add-password')) {input.value='';input.type='password';}
});

function addMode() {
  const random = document.querySelector('input[name="mode"]:checked').value === 'random';
  $("#add-mac-fields").hidden = random;
  $("#add-credential-fields").hidden = !random;
  $("#add-mac").required = !random;
  $("#add-username").required = random;
  $("#add-password").required = random;
  $("#add-submit").textContent = random ? '添加并认证' : '添加线路';
}
for (const input of document.querySelectorAll('input[name="mode"]')) input.addEventListener('change',addMode);
$("#add-visible").addEventListener('change',e => {$("#add-password").type=e.target.checked?'text':'password';});
$("#add-button").addEventListener('click', () => {$("#add-form").reset();$("#add-password").type='password';addMode();$("#add-message").textContent='';$("#add-dialog").showModal();});
$("#add-form").addEventListener('submit',async event => {
  event.preventDefault();
  const random = document.querySelector('input[name="mode"]:checked').value === 'random';
  const values = random ? {action:'add-random',username:$("#add-username").value,password:$("#add-password").value} : {action:'add-mac',mac:$("#add-mac").value.trim()};
  operationBusy(true);$("#add-submit").disabled=true;$("#add-message").textContent=random?'正在创建线路、申请 DHCP 并认证…':'正在创建线路并申请 DHCP…';
  $("#add-password").type='password';$("#add-visible").checked=false;
  try {
    const result = await perform('/api/paths',values);
    if (!result.ok && !result.retained) throw new Error(resultMessage(result));
    $("#add-password").value='';$("#add-username").value='';$("#add-dialog").close();
    $("#refresh-status").textContent=resultMessage(result);$("#refresh-status").dataset.state=result.ok?'success':'error';await poll();
  } catch(error) {$("#add-message").textContent=error.message;}
  finally {operationBusy(false);$("#add-submit").disabled=false;}
});

let pathAction;
$("#path-list").addEventListener('click',event => {
  const refresh = event.target.closest('.path-refresh');
  if (refresh) {reconnectPaths(refresh.dataset.interface);return;}
  const button = event.target.closest('.path-remove, .path-edit');
  if (!button) return;
  const iface=button.dataset.interface,path=state.payload.sample.paths.find(p=>p.interface===iface);
  const edit=button.classList.contains('path-edit');pathAction={action:edit?'edit':'remove',interface:iface};
  $("#path-action-form").reset();$("#path-action-title").textContent=`${iface} · ${edit?'修改 MAC':'删除线路'}`;
  $("#path-action-note").textContent=edit?'改变 MAC 会中断这条线路，原凭据绑定将失效。请重新保存凭据后手动刷新。':
    (path.is_main?'该线路将退出池，凭据会删除；共享的物理 WAN 保留作底层接口。':'删除该线路的网络配置和凭据，已有连接会中断。MAC 历史继续保留以避免重复。');
  if (edit && path.is_main) $("#path-action-note").textContent+='修改物理主 WAN 可能短暂影响共享父设备的副线路。';
  $("#edit-mac-field").hidden=!edit;$("#edit-mac").required=edit;$("#edit-mac").value=path.mac;
  $("#path-action-submit").textContent=edit?'保存 MAC':'删除线路';$("#path-action-message").textContent='';$("#path-dialog").showModal();
});
$("#path-action-form").addEventListener('submit',async event => {
  event.preventDefault();operationBusy(true);$("#path-action-submit").disabled=true;
  $("#path-action-message").textContent='正在执行…';
  try {
    const result=await perform('/api/paths',{...pathAction,mac:$("#edit-mac").value.trim()});
    if (!result.ok) throw new Error(resultMessage(result));
    $("#path-dialog").close();$("#refresh-status").textContent=resultMessage(result);await poll();
  } catch(error) {$("#path-action-message").textContent=error.message;}
  finally {operationBusy(false);$("#path-action-submit").disabled=false;}
});

$("#settings-button").addEventListener('click',async () => {
  $("#settings-message").textContent='正在读取设置…';$("#settings-dialog").showModal();
  try {
    const response=await MwanAPI.request('/api/config');const result=await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || '读取设置失败');
    const config=result.settings;
    for (const [id,key] of [['parent','parent_device'],['lan','lan_device'],['portal','portal_url'],['health','health_url']]) $("#setting-"+id).value=config[key];
    $("#setting-balance").value=config.balance_mode || 'round_robin';$("#setting-failover").checked=config.router_failover !== false;
    $("#setting-parent").disabled=!!state.payload?.sample?.paths?.length;
    $("#settings-message").textContent='已有线路时不能改变 WAN 父设备。保存设置不会提交认证。';
  } catch(error) {$("#settings-message").textContent=error.message;}
});
$("#settings-form").addEventListener('submit',async event => {
  event.preventDefault();operationBusy(true);$("#settings-submit").disabled=true;
  try {
    const result=await perform('/api/config',{parent_device:$("#setting-parent").value,lan_device:$("#setting-lan").value,
      portal_url:$("#setting-portal").value,health_url:$("#setting-health").value,
      balance_mode:$("#setting-balance").value,router_failover:$("#setting-failover").checked?'1':'0'});
    if (!result.ok) throw new Error(resultMessage(result));
    $("#settings-dialog").close();$("#refresh-status").textContent='接入设置已保存';
  } catch(error) {$("#settings-message").textContent=error.message;}
  finally {operationBusy(false);$("#settings-submit").disabled=false;}
});
$("#portal-discover").addEventListener('click',async () => {
  $("#portal-discover").disabled=true;
  try {
    const result=await perform('/api/paths',{action:'discover'});
    if (!result.ok || !result.portal_url) throw new Error(resultMessage(result));
    $("#setting-portal").value=result.portal_url;
    $("#settings-message").textContent='仅检测地址，未提交凭据。请核对学校的认证服务器，再点击“保存设置”。';
  } catch(error) {$("#settings-message").textContent=error.message;}
  finally {$("#portal-discover").disabled=false;}
});

window.addEventListener("resize", () => {
  if (state.payload) drawChart(state.payload.history || []);
});

poll();
