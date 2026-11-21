// VOLT v2 phone app. No framework, no build step: edit and reload.
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ── pairing / token ─────────────────────────────────────────
const m = location.hash.match(/pair=([\w-]+)/);
if (m) { localStorage.setItem("volt.token", m[1]); history.replaceState(null, "", "/"); }
let TOKEN = localStorage.getItem("volt.token") || "";

function showPair() { $("#app").hidden = true; $("#pair").hidden = false; }
$("#pairBtn").onclick = () => {
  TOKEN = $("#pairToken").value.trim();
  localStorage.setItem("volt.token", TOKEN);
  boot();
};

async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { "x-volt-token": TOKEN, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) { showPair(); throw new Error("not paired"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
  return data;
}
const tryApi = async (...a) => { try { return await api(...a); } catch (e) { toast(e.message, true); throw e; } };

// ── ui helpers ──────────────────────────────────────────────
let toastT;
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastT); toastT = setTimeout(() => (t.className = "toast"), 2600);
}
function confirmSheet(html, yes = "Go") {
  return new Promise((res) => {
    $("#sheetMsg").innerHTML = html; $("#sheetYes").textContent = yes; $("#sheet").hidden = false;
    const done = (v) => { $("#sheet").hidden = true; res(v); };
    $("#sheetYes").onclick = () => done(true); $("#sheetNo").onclick = () => done(false);
  });
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (a) => (a == null ? "–" : a.map((x) => x.toFixed(1)).join("  "));

// ── state ───────────────────────────────────────────────────
let S = {};          // settings
let R = {};          // latest robot snapshot
let jogMode = "joint";
let ws, wsOpen = false;

// ── websocket ───────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { wsOpen = true; };
  ws.onclose = (e) => {
    wsOpen = false; setConn("bad", "Offline");
    if (e.code === 4401) return showPair();
    setTimeout(connectWS, 1500);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.t === "state") render(msg);
    else if (msg.t === "refused" || msg.t === "error") { toast(msg.error, true); markBlocked(); }
  };
}
const send = (o) => wsOpen && ws.send(JSON.stringify(o));

function setConn(cls, text) { $("#connDot").className = "dot " + cls; $("#connText").textContent = text; }

function render(s) {
  R = s;
  const busy = s.busy ? ` · ${s.busy}` : "";
  const hm = s.homed === false ? " · not homed" : "";
  if (s.state === "connected") setConn(s.estop || s.homed === false ? "warn" : "ok", (s.estop ? "E-STOP" : s.enabled ? "Ready" : "Disabled") + hm + busy);
  else setConn(s.state === "connecting" ? "warn" : "bad", s.state === "connecting" ? "Connecting…" : "Not connected");
  $("#homeBanner").hidden = !(s.state === "connected" && s.homed === false);
  const tag = $("#modeTag");
  tag.textContent = s.mock ? "MOCK" : s.sim ? "SIM" : "REAL";
  tag.classList.toggle("real", !s.sim);
  const en = $("#enableBtn"); en.classList.toggle("on", !!s.enabled); en.textContent = s.enabled ? "ENABLED" : "ENABLE";
  updateJogReadout();
  $("#gripVal").textContent = s.gripper_calibrated === false ? "not calibrated"
    : s.gripper == null ? "–" : `${Math.round(s.gripper * 100)}% closed`;
  $("#camPose").textContent = s.pose ? `X ${s.pose[0].toFixed(0)}  Y ${s.pose[1].toFixed(0)}  Z ${s.pose[2].toFixed(0)}` : "";
}

// ── header buttons ──────────────────────────────────────────
$("#stopBtn").addEventListener("pointerdown", () => {
  send({ t: "stop" });
  api("POST", "/api/stop").catch(() => {});
  toast("Stopped");
});
$("#enableBtn").onclick = async () => {
  if (R.enabled) return toast("Already enabled. STOP disables.");
  if (!R.sim && !(await confirmSheet("<b>Enable motors on the real arm?</b><br><span class='muted'>Make sure the workspace is clear.</span>", "Enable"))) return;
  tryApi("POST", "/api/enable").then(() => toast("Motors enabled"));
};

// ── tabs ────────────────────────────────────────────────────
const onShow = { poses: loadPoses, look: loadCam, ai: loadAI, system: loadSystem };
$$(".tabs button").forEach((b) => (b.onclick = () => {
  $$(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".view").forEach((v) => (v.hidden = v.dataset.view !== b.dataset.tab));
  $("main").scrollTop = 0;
  onShow[b.dataset.tab]?.();
}));

// ── drive: jog rows ─────────────────────────────────────────
const JOINTS = ["J1 base", "J2 shoulder", "J3 elbow", "J4 wrist roll", "J5 wrist pitch", "J6 tool roll"];
const AXES = ["X", "Y", "Z", "RX", "RY", "RZ"];

function buildJogRows() {
  const names = jogMode === "joint" ? JOINTS : AXES.map((a) => `${a} ${a.startsWith("R") ? "rotate" : "move"}`);
  $("#jogRows").innerHTML = names.map((n, i) => `
    <div class="jog" data-i="${i}">
      <button class="hold" data-dir="-1">−</button>
      <div class="mid">
        <div class="name"><span>${n}</span><b class="num" id="jv${i}">–</b></div>
        ${jogMode === "joint" ? `<div class="bar"><i id="jb${i}"></i></div>` : ""}
      </div>
      <button class="hold" data-dir="1">+</button>
    </div>`).join("");
  $$(".jog .hold").forEach(bindHold);
  updateJogReadout();
}

function updateJogReadout() {
  const vals = jogMode === "joint" ? R.joints : R.pose;
  const lim = S.motion?.joint_limits;
  for (let i = 0; i < 6; i++) {
    const el = $("#jv" + i); if (!el) continue;
    el.textContent = vals ? vals[i].toFixed(1) + (jogMode === "joint" || i > 2 ? "°" : "") : "–";
    const bar = $("#jb" + i);
    if (bar && vals && lim) {
      const [lo, hi] = lim[i];
      bar.style.left = Math.max(0, Math.min(100, ((vals[i] - lo) / (hi - lo)) * 100)) + "%";
    }
  }
}

let holdTimer = null, holdBtn = null;
function bindHold(btn) {
  const row = btn.closest(".jog");
  const start = (e) => {
    e.preventDefault();
    if (holdBtn) return;
    if (!wsOpen) return toast("Not connected", true);
    btn.setPointerCapture?.(e.pointerId);
    holdBtn = btn; btn.classList.add("active"); btn.classList.remove("blocked");
    const i = +row.dataset.i, dir = +btn.dataset.dir;
    const msg = jogMode === "joint" ? { t: "jog", joint: i + 1, dir } : { t: "jog", mode: "cart", axis: AXES[i], dir };
    const tick = () => send({ ...msg, speed: +$("#speed").value });
    tick();
    holdTimer = setInterval(tick, 100);
  };
  const end = () => {
    if (holdBtn !== btn) return;
    clearInterval(holdTimer); holdTimer = null; holdBtn = null;
    btn.classList.remove("active");
    send({ t: "jog_end" });
  };
  btn.addEventListener("pointerdown", start);
  ["pointerup", "pointercancel", "lostpointercapture"].forEach((ev) => btn.addEventListener(ev, end));
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
}
function markBlocked() {
  if (!holdBtn) return;
  holdBtn.classList.add("blocked");
  holdBtn.dispatchEvent(new Event("pointercancel"));
}
document.addEventListener("visibilitychange", () => { if (document.hidden && holdBtn) holdBtn.dispatchEvent(new Event("pointercancel")); });

$$("#jogMode button").forEach((b) => (b.onclick = () => {
  jogMode = b.dataset.v;
  $$("#jogMode button").forEach((x) => x.classList.toggle("on", x === b));
  buildJogRows();
}));
$("#speed").oninput = (e) => ($("#speedVal").textContent = e.target.value + "%");

// gripper: if parol6 says it isn't calibrated yet, offer to calibrate, then retry
async function gripper(body) {
  try { return await api("POST", "/api/gripper", body); }
  catch (e) {
    if (!String(e.message).startsWith("GRIPPER_NOT_CALIBRATED")) { toast(e.message, true); throw e; }
    const ok = await confirmSheet("<b>Calibrate the gripper first?</b><br><span class='muted'>It needs this once after every connect. The jaws open and close fully.</span>", "Calibrate");
    if (!ok) throw e;
    toast("Calibrating gripper…");
    await tryApi("POST", "/api/gripper", { action: "calibrate" });
    return await tryApi("POST", "/api/gripper", body);
  }
}
$$("[data-grip]").forEach((b) => (b.onclick = () =>
  gripper({ action: b.dataset.grip }).then(() => ($("#gripSlider").value = b.dataset.grip === "open" ? 0 : 100)).catch(() => {})));
$("#gripSlider").onchange = (e) => gripper({ action: "set", position: e.target.value / 100 }).catch(() => {});

$("#homeBtn").onclick = async () => {
  if (!(await confirmSheet("<b>Home the arm?</b><br><span class='muted'>All joints move to the home position.</span>", "Home"))) return;
  toast("Homing…"); tryApi("POST", "/api/home").then(() => toast("Home done"));
};
$("#savePoseQuick").onclick = () => { $$(".tabs button")[1].click(); $("#poseName").focus(); };

// ── poses ───────────────────────────────────────────────────
async function loadPoses() {
  const p = await tryApi("GET", "/api/poses");
  const names = Object.keys(p);
  $("#poseList").innerHTML = names.length ? names.map((n) => `
    <div class="pose">
      <div><div class="pn">${esc(n)}</div><div class="pj">${fmt(p[n])}</div></div>
      <button class="btn small accent" data-go="${esc(n)}">Go</button>
      <button class="del" data-del="${esc(n)}">×</button>
    </div>`).join("") : `<p class="muted center">No poses yet. Jog somewhere and save one.</p>`;
  $$("[data-go]").forEach((b) => (b.onclick = async () => {
    const sp = +$("#speed").value;
    if (!(await confirmSheet(`<b>Move to “${esc(b.dataset.go)}”?</b><br><span class="muted">at ${sp}% speed</span>`))) return;
    tryApi("POST", `/api/poses/${encodeURIComponent(b.dataset.go)}/go`, { speed: sp }).then(() => toast("Arrived"));
  }));
  $$("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmSheet(`Delete pose <b>${esc(b.dataset.del)}</b>?`, "Delete"))) return;
    await tryApi("DELETE", `/api/poses/${encodeURIComponent(b.dataset.del)}`); loadPoses();
  }));
}
$("#poseSave").onclick = async () => {
  const n = $("#poseName").value.trim(); if (!n) return toast("Name it first", true);
  await tryApi("POST", "/api/poses", { name: n }); $("#poseName").value = ""; toast("Saved " + n); loadPoses();
};

// ── look ────────────────────────────────────────────────────
async function loadCam() {
  const c = await tryApi("GET", "/api/camera");
  const img = $("#camImg");
  $("#camSrc").textContent = c.source === "remote" ? "DELL" : c.source === "local" ? "PI" : "NONE";
  if (!c.url) { img.hidden = true; $("#camEmpty").hidden = false; return; }
  const url = c.source === "local" ? `${c.url}?token=${encodeURIComponent(TOKEN)}&_=${Date.now()}` : c.url;
  img.onload = () => ($("#camEmpty").hidden = true);
  img.onerror = () => { img.hidden = true; $("#camEmpty").hidden = false; $("#camEmpty").textContent = "Camera offline"; };
  img.hidden = false; img.src = url;
}
$("#camReload").onclick = loadCam;

// ── AI ──────────────────────────────────────────────────────
const AI_HELP = {
  off: "AI is off. Manual control only.",
  chat: "AI can talk and look through the camera. It never moves the arm.",
  supervised: "AI proposes moves. Nothing moves until you tap Approve.",
  autonomous: "AI runs allowed actions on its own, capped at the AI speed limit. STOP always works.",
};
async function loadAI() {
  await loadSettings();
  paintAIMode();
  const st = await api("GET", "/api/ai/status").catch(() => ({ brain: false }));
  $("#brainDot").className = "dot " + (st.brain ? "ok" : "bad");
  $("#brainText").textContent = st.brain ? "Brain online (Dell)" : "Brain offline. Start it on the Dell";
}
function paintAIMode() {
  const mode = S.ai.mode;
  $$("#aiMode button").forEach((b) => b.classList.toggle("on", b.dataset.v === mode));
  $("#aiModeHelp").textContent = AI_HELP[mode];
}
$$("#aiMode button").forEach((b) => (b.onclick = async () => {
  if (b.dataset.v === "autonomous" && !(await confirmSheet("<b>Autonomous mode?</b><br><span class='muted'>The AI can move the arm without asking, using only the actions you allowed.</span>", "Turn on"))) return;
  S = await tryApi("PATCH", "/api/settings", { ai: { mode: b.dataset.v } }); paintAIMode();
}));
function say(text, who) {
  const d = document.createElement("div"); d.className = "msg " + who; d.textContent = text;
  $("#chat").appendChild(d); d.scrollIntoView({ block: "end" });
}
const stepText = (s) => `${s.action}${s.args && Object.keys(s.args).length ? " " + JSON.stringify(s.args) : ""}${s.why ? " · " + s.why : ""}`;
$("#aiSend").onclick = async () => {
  const text = $("#aiText").value.trim(); if (!text) return;
  $("#aiText").value = ""; say(text, "me"); $("#planBox").innerHTML = "";
  const r = await tryApi("POST", "/api/ai/command", { text });
  say(r.reply || "(no reply)", r.offline ? "sys" : "bot");
  if (!r.plan?.length && !r.rejected?.length) return;
  $("#planBox").innerHTML = `<div class="card plan">
      <div class="card-h"><span>Plan</span><span class="tag">${S.ai.mode.toUpperCase()}</span></div>
      <ol>${r.plan.map((s) => `<li>${esc(stepText(s))}</li>`).join("")}</ol>
      ${r.rejected.map((s) => `<div class="rej">Blocked: ${esc(s.action)} (${esc(s.reason)})</div>`).join("")}
      ${r.needs_approval ? `<div class="btn-row"><button class="btn" id="planNo">Reject</button><button class="btn accent" id="planYes">Approve</button></div>` : ""}
      ${r.results ? `<div class="muted small">${r.results.map((x) => (x.ok ? "✓ " : "✗ ") + x.step.action + (x.error ? ": " + esc(x.error) : "")).join("<br>")}</div>` : ""}
    </div>`;
  if (r.needs_approval) {
    $("#planNo").onclick = () => { $("#planBox").innerHTML = ""; say("Plan rejected.", "sys"); };
    $("#planYes").onclick = async () => {
      $("#planBox").innerHTML = "";
      const res = await tryApi("POST", "/api/ai/execute", { plan: r.plan });
      say(res.map((x) => (x.ok ? "✓ " : "✗ ") + x.step.action + (x.error ? ": " + x.error : "")).join("\n"), "sys");
    };
  }
};
$("#aiText").addEventListener("keydown", (e) => e.key === "Enter" && $("#aiSend").click());

// ── system / settings ───────────────────────────────────────
async function loadSettings() {
  S = await api("GET", "/api/settings");
  $("#speed").max = S.motion.speed_cap_pct;
  if (+$("#speed").value > S.motion.speed_cap_pct) $("#speed").value = S.motion.speed_cap_pct;
  $("#speedVal").textContent = $("#speed").value + "%";
}
const getPath = (o, p) => p.split(".").reduce((a, k) => a?.[k], o);
const patchFor = (p, v) => { const [a, b] = p.split("."); return { [a]: { [b]: v } }; };

async function loadSystem() {
  await loadSettings();
  const ports = await api("GET", "/api/ports").catch(() => []);
  const opts = (sel) => `<option value="">Auto-detect</option>` + ports.map((p) =>
    `<option value="${esc(p.device)}">${esc(p.device)}${p.vid === "0483" ? " (PAROL6)" : ""}</option>`).join("");
  $("#portSel").innerHTML = opts(); $("#gripPortSel").innerHTML = opts();

  $$("[data-set]").forEach((el) => {
    const path = el.dataset.set, v = getPath(S, path);
    if (el.classList.contains("seg")) {
      $$("button", el).forEach((b) => {
        b.classList.toggle("on", String(v) === b.dataset.v);
        b.onclick = () => save(path, b.dataset.v === "true" ? true : b.dataset.v === "false" ? false : b.dataset.v);
      });
    } else if (el.type === "checkbox") { el.checked = !!v; el.onchange = () => save(path, el.checked); }
    else {
      el.value = v ?? "";
      el.onchange = () => save(path, el.type === "number" ? +el.value : el.value);
    }
  });

  $("#limitsEd").innerHTML = S.motion.joint_limits.map((l, i) =>
    `<span>J${i + 1}</span><input type="number" step="0.5" data-lim="${i},0" value="${l[0]}"><input type="number" step="0.5" data-lim="${i},1" value="${l[1]}">`).join("");
  $$("[data-lim]").forEach((el) => (el.onchange = () => {
    const lim = S.motion.joint_limits.map((x) => [...x]);
    const [i, k] = el.dataset.lim.split(",").map(Number); lim[i][k] = +el.value;
    save("motion.joint_limits", lim);
  }));

  const ACT = ["goto_pose", "gripper", "home", "look"];
  $("#aiActions").innerHTML = ACT.map((a) => `<button class="chip ${S.ai.allowed_actions.includes(a) ? "on" : ""}" data-a="${a}">${a}</button>`).join("");
  $$("#aiActions .chip").forEach((c) => (c.onclick = () => {
    const set = new Set(S.ai.allowed_actions); set.has(c.dataset.a) ? set.delete(c.dataset.a) : set.add(c.dataset.a);
    save("ai.allowed_actions", [...set]);
  }));

  const log = await api("GET", "/api/log").catch(() => []);
  $("#logList").innerHTML = log.slice().reverse().map((e) =>
    `<div class="${e.level}">${new Date(e.t * 1000).toLocaleTimeString()}  ${esc(e.msg)}</div>`).join("") || "Nothing yet";
  loadMemory();
  const h = await fetch("/api/hello").then((r) => r.json());
  $("#version").textContent = `VOLT ${h.version} ${h.build || ""}`;
}
async function loadMemory() {
  const mem = await api("GET", "/api/memory?since=0&limit=5000").catch(() => []);
  $("#memList").innerHTML = mem.slice(-25).reverse().map((e) =>
    `<div>${new Date(e.t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}  <b>${esc(e.kind)}</b>  ${esc(e.text)}</div>`).join("") || "No memories yet";
}
$("#memAdd").onclick = async () => {
  const text = $("#memText").value.trim(); if (!text) return;
  await tryApi("POST", "/api/memory/note", { text }); $("#memText").value = ""; toast("Remembered"); loadMemory();
};

async function save(path, value) {
  try { S = await api("PATCH", "/api/settings", patchFor(path, value)); toast("Saved"); }
  catch (e) { toast(e.message, true); }
  loadSystem();
}
$("#reconnectBtn").onclick = () => tryApi("POST", "/api/reconnect").then(() => toast("Reconnecting…"));
$("#gripCal").onclick = async () => {
  if (!(await confirmSheet("<b>Calibrate the gripper?</b><br><span class='muted'>Jaws run through full travel.</span>", "Calibrate"))) return;
  tryApi("POST", "/api/gripper", { action: "calibrate" }).then(() => toast("Calibrating"));
};

const ICON = { pass: "✓", warn: "!", fail: "✗", skip: "–" };
$("#diagRun").onclick = async () => {
  $("#diagList").innerHTML = `<p class="muted">Running…</p>`;
  const res = await tryApi("GET", "/api/diagnostics");
  $("#diagList").innerHTML = res.map(drawDiag).join("");
};
const drawDiag = (d) => `<div class="d ${d.status}"><span class="ic">${ICON[d.status]}</span>
  <div><div class="dn">${esc(d.name)}</div><div class="dd">${esc(d.detail)}</div>${d.fix ? `<div class="df">${esc(d.fix)}</div>` : ""}</div></div>`;
$$("#motionTests .chip").forEach((c) => (c.onclick = async () => {
  if (!(await confirmSheet(`<b>Run “${c.textContent}” test?</b><br><span class='muted'>The ${c.dataset.t === "gripper" ? "gripper" : "joint"} moves a small amount at low speed.</span>`, "Run"))) return;
  const r = await tryApi("POST", `/api/diagnostics/motion/${c.dataset.t}`);
  $("#diagList").insertAdjacentHTML("afterbegin", drawDiag(r));
}));

// ── boot ────────────────────────────────────────────────────
async function boot() {
  const hello = await fetch("/api/hello").then((r) => r.json()).catch(() => ({}));
  if (hello.pairing && !TOKEN) return showPair();
  try { await loadSettings(); } catch { return showPair(); }
  $("#pair").hidden = true; $("#app").hidden = false;
  buildJogRows();
  connectWS();
}
boot();
