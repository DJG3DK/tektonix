/* The control panel. Plain script on purpose: no bundler, no framework.
   Everything that does work lives in Rust (src-tauri/src); this page asks
   for it and shows what comes back. `window.__TAURI__` is on because
   tauri.conf.json sets withGlobalTauri. */
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const dialog = window.__TAURI__.dialog;
const opener = window.__TAURI__.opener;
const updater = window.__TAURI__.updater;
const proc = window.__TAURI__.process;

const $ = (id) => document.getElementById(id);
const show = (id) => { for (const p of document.querySelectorAll(".page")) p.classList.add("hidden"); $(id).classList.remove("hidden"); };
const log = $("log");
let following = false;
let pendingStackUpdate = null;
let appUpdate = null;

function append(stream, line) {
  const span = document.createElement("span");
  span.className = stream;
  span.textContent = line + "\n";
  log.appendChild(span);
  if (log.childNodes.length > 2000) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}
listen("stack-log", (e) => append(e.payload.stream, e.payload.line));

function say(line) { append("app", line); }
function fail(e) { append("err", String(e)); }

for (const a of document.querySelectorAll("a[data-open]")) {
  a.addEventListener("click", (ev) => { ev.preventDefault(); opener.openUrl(a.dataset.open); });
}

// ── Docker ───────────────────────────────────────────────────────────────────
async function checkDocker() {
  show("page-docker");
  $("docker-install").classList.add("hidden");
  $("docker-start").classList.add("hidden");
  $("docker-title").textContent = "Checking Docker…";
  $("docker-text").textContent = "";
  const state = await invoke("docker_state");
  if (state === "ready") return afterDocker();
  if (state === "missing") {
    $("docker-title").textContent = "Docker Desktop is not installed";
    $("docker-text").textContent = "Tektonix runs in containers. This installs WSL 2 if it is missing and then Docker Desktop, from Docker's own site. Windows will ask for permission, and a restart may be needed.";
    $("docker-install").classList.remove("hidden");
  } else if (state === "stopped") {
    $("docker-title").textContent = "Docker Desktop is not running";
    $("docker-text").textContent = "Start it and it comes up in a minute. If Docker Desktop itself says \"Virtualization support not detected\", the CPU's virtualization is switched off in the firmware (BIOS/UEFI: Intel VT-x or AMD SVM); no installer can turn that on.";
    $("docker-start").classList.remove("hidden");
  } else {
    $("docker-title").textContent = "This Docker has no compose command";
    $("docker-text").textContent = "Update Docker Desktop and check again.";
  }
}
$("docker-recheck").onclick = checkDocker;
$("docker-start").onclick = async () => {
  $("docker-start").disabled = true;
  try { await invoke("docker_start"); await afterDocker(); } catch (e) { fail(e); } finally { $("docker-start").disabled = false; }
};
$("docker-install").onclick = async () => {
  $("docker-install").disabled = true;
  try {
    const next = await invoke("docker_install");
    $("docker-text").textContent = next;
    say(next);
  } catch (e) { fail(e); } finally { $("docker-install").disabled = false; }
};

// ── Setup ────────────────────────────────────────────────────────────────────
let settings = null;
async function afterDocker() {
  settings = await invoke("settings_get");
  $("stack-version").textContent = "";
  const installed = await invoke("installed_version");
  if (installed) $("stack-version").textContent = "stack " + installed;
  if (!settings.openrouter_api_key_set || !settings.projects_dir) return showSetup(false);
  await showStack();
}

async function showSetup(cancellable) {
  show("page-setup");
  $("setup-key").value = "";
  $("setup-key-hint").textContent = settings && settings.openrouter_api_key_set ? `Set (${settings.openrouter_key_hint}). Leave blank to keep it.` : "";
  $("setup-dir").value = settings ? settings.projects_dir : "";
  $("setup-email").value = settings ? settings.admin_email : "";
  let gitName = settings ? settings.git_name : "";
  let gitEmail = settings ? settings.git_email : "";
  if (!gitName && !gitEmail) {
    try { [gitName, gitEmail] = await invoke("machine_git_identity"); } catch (e) { /* no git here; fine */ }
  }
  $("setup-git-name").value = gitName || "";
  $("setup-git-email").value = gitEmail || "";
  $("setup-cancel").classList.toggle("hidden", !cancellable);
  $("setup-error").classList.add("hidden");
}
$("setup-pick").onclick = async () => {
  const dir = await dialog.open({ directory: true, multiple: false, title: "Projects folder" });
  if (dir) $("setup-dir").value = dir;
};
$("setup-cancel").onclick = () => showStack();
$("setup-form").onsubmit = async (ev) => {
  ev.preventDefault();
  $("setup-save").disabled = true;
  $("setup-error").classList.add("hidden");
  try {
    const key = $("setup-key").value.trim();
    settings = await invoke("settings_save", {
      key: key || null, projectsDir: $("setup-dir").value, adminEmail: $("setup-email").value,
      gitName: $("setup-git-name").value, gitEmail: $("setup-git-email").value,
    });
    await showStack();
    if (!(await invoke("installed_version"))) await runStack("stack_install");
  } catch (e) {
    $("setup-error").textContent = String(e);
    $("setup-error").classList.remove("hidden");
  } finally {
    $("setup-save").disabled = false;
  }
};

// ── Stack ────────────────────────────────────────────────────────────────────
let busy = false;
async function showStack() {
  show("page-stack");
  await refreshStatus();
}

function setState(text, tone, lead) {
  const pill = $("stack-state");
  pill.textContent = text;
  pill.className = "pill " + tone;
  $("stack-text").textContent = lead || "";
}

async function refreshStatus() {
  let rows = [];
  try { rows = await invoke("stack_status"); } catch (e) { /* not up yet */ }
  const tbody = $("containers").querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const st = r.health ? `${r.state} (${r.health})` : r.state;
    tr.innerHTML = `<td>${r.service}</td><td class="state-${r.state}">${st}</td>`;
    tbody.appendChild(tr);
  }
  const agent = rows.find((r) => r.service === "agent");
  const running = agent && agent.state === "running";
  if (busy) return;
  if (running) setState("running", agent.health === "healthy" || !agent.health ? "good" : "warn", "The dashboard is at http://localhost:8100. Close this window; the tray icon keeps it reachable.");
  else if (rows.length) setState("stopped", "warn", "The stack is installed and stopped.");
  else setState("not started", "warn", "Nothing is running yet.");
  $("btn-open").disabled = !running;
  $("btn-password").disabled = !running;
  $("btn-stop").disabled = !running;
  $("btn-start").disabled = !!running;
}
setInterval(() => { if (!$("page-stack").classList.contains("hidden")) refreshStatus(); }, 10000);

async function runStack(command, args) {
  busy = true;
  setState("working", "warn", "Docker's own progress is in the log below. The first start pulls about 3 GB.");
  for (const id of ["btn-start", "btn-stop", "btn-update-stack"]) $(id).disabled = true;
  try {
    await invoke(command, args || {});
    say("Done.");
  } catch (e) {
    fail(e);
    setState("error", "bad", String(e));
  } finally {
    busy = false;
    await refreshStatus();
  }
}
$("btn-start").onclick = () => runStack("stack_up");
$("btn-stop").onclick = () => runStack("stack_down");
$("btn-open").onclick = () => invoke("open_dashboard").catch(fail);
$("btn-settings").onclick = () => { void showSetup(true); };
$("btn-folder").onclick = () => { if (settings && settings.projects_dir) opener.openPath(settings.projects_dir).catch(fail); };
$("btn-password").onclick = async () => {
  try {
    const pw = await invoke("stack_password");
    $("password-value").textContent = pw;
    $("password-box").classList.remove("hidden");
  } catch (e) {
    fail("No one-time password to show: it was shown already, or the agent has not started yet. " + e);
  }
};

// ── Updates ──────────────────────────────────────────────────────────────────
$("btn-check-stack").onclick = async () => {
  $("update-text").textContent = "Checking…";
  $("btn-update-stack").classList.add("hidden");
  $("btn-update-app").classList.add("hidden");
  try {
    const info = await invoke("stack_check_update");
    const installed = await invoke("installed_version");
    $("stack-version").textContent = installed ? "stack " + installed : "";
    if (info.available) {
      pendingStackUpdate = info.latest;
      $("update-text").textContent = `Tektonix ${info.latest} is out; you have ${info.installed}.`;
      $("btn-update-stack").classList.remove("hidden");
    } else {
      $("update-text").textContent = `Tektonix is up to date (${info.installed || info.latest}).`;
    }
  } catch (e) { $("update-text").textContent = String(e); }
  try {
    appUpdate = await updater.check();
    if (appUpdate) {
      $("update-text").textContent += ` This app has a new version too (${appUpdate.version}).`;
      $("btn-update-app").classList.remove("hidden");
    }
  } catch (e) { /* no updater endpoint reachable; the stack check above is the one that matters */ }
};
$("btn-update-stack").onclick = async () => {
  if (!pendingStackUpdate) return;
  $("btn-update-stack").classList.add("hidden");
  await runStack("stack_update", { tag: pendingStackUpdate });
  const installed = await invoke("installed_version");
  $("stack-version").textContent = installed ? "stack " + installed : "";
  pendingStackUpdate = null;
};
$("btn-update-app").onclick = async () => {
  if (!appUpdate) return;
  $("btn-update-app").disabled = true;
  try {
    say(`Downloading app ${appUpdate.version}…`);
    await appUpdate.downloadAndInstall((ev) => { if (ev.event === "Finished") say("Installed; restarting."); });
    await proc.relaunch();
  } catch (e) { fail(e); $("btn-update-app").disabled = false; }
};

// ── Logs ─────────────────────────────────────────────────────────────────────
$("btn-logs").onclick = async () => {
  try { await invoke("logs_follow", { service: $("log-service").value }); following = true; say(`Following ${$("log-service").value}…`); } catch (e) { fail(e); }
};
$("btn-logs-stop").onclick = async () => { await invoke("logs_stop"); if (following) say("Stopped following."); following = false; };
$("btn-logs-clear").onclick = () => { log.textContent = ""; };

// ── Boot ─────────────────────────────────────────────────────────────────────
(async () => {
  try {
    const v = await window.__TAURI__.app.getVersion();
    $("app-version").textContent = "app v" + v;
  } catch (e) { /* fine */ }
  await checkDocker();
})();
