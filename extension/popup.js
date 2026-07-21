// Slack Harvester Creds — Popup
//
// Renders the status the background worker records in chrome.storage.local
// under "lastStatus". Shows logged-in state, last successful push time, the
// last push result (ok / 401 / unreachable), a manual "Push now" button, and a
// logout note. NEVER displays token/cookie values (the worker never stores them).

function fmtTime(iso) {
  if (!iso) return "never";
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

function render(status, cfg) {
  const bar = document.getElementById("statusBar");
  const dot = document.getElementById("statusDot");
  const text = document.getElementById("statusText");
  const logoutNote = document.getElementById("logoutNote");
  const lastPush = document.getElementById("lastPush");
  const harvester = document.getElementById("harvester");
  const pushStatus = document.getElementById("pushStatus");

  // Config / harvester line.
  if (!cfg) {
    bar.className = "status-bar noconfig";
    dot.className = "dot yellow";
    text.textContent = "No config.json — paste your api-token";
    harvester.textContent = "config.json missing";
    return;
  }
  harvester.textContent =
    `127.0.0.1:${cfg.harvesterPort} · every ${cfg.refreshIntervalMinutes} min` +
    (cfg.hasApiToken ? "" : " · NO apiToken set");

  if (!status) {
    bar.className = "status-bar warn";
    dot.className = "dot yellow";
    text.textContent = "No data yet — click Push now";
    return;
  }

  // Logged-in state.
  if (status.loggedIn) {
    bar.className = "status-bar ok";
    dot.className = "dot green";
    text.textContent = "Signed in to Slack web";
    logoutNote.style.display = "none";
  } else {
    bar.className = "status-bar out";
    dot.className = "dot red";
    text.textContent = "Logged out — re-sign-in needed";
    logoutNote.style.display = "block";
  }

  // Last push result.
  const p = status.lastPush;
  if (p && p.ok) {
    pushStatus.className = "push-status ok";
    pushStatus.textContent = "Push status: OK (200)";
    lastPush.textContent = fmtTime(p.at);
  } else if (p) {
    pushStatus.className = "push-status bad";
    const label = p.status === 401 ? "401 unauthorized" : p.error || "failed";
    pushStatus.textContent = `Push status: ${label}`;
    // lastPush shows last SUCCESSFUL push time — leave as-is if this one failed.
  } else {
    pushStatus.className = "push-status bad";
    pushStatus.textContent = status.loggedIn
      ? "Push status: not attempted"
      : "Push status: skipped (logged out)";
  }
}

function loadAndRender() {
  Promise.all([
    new Promise((resolve) => chrome.runtime.sendMessage({ type: "getConfig" }, resolve)),
    new Promise((resolve) => chrome.runtime.sendMessage({ type: "getStatus" }, resolve)),
  ]).then(([cfg, status]) => render(status, cfg));
}

document.getElementById("pushNow").addEventListener("click", () => {
  const btn = document.getElementById("pushNow");
  btn.disabled = true;
  btn.textContent = "Pushing...";
  Promise.all([
    new Promise((resolve) => chrome.runtime.sendMessage({ type: "getConfig" }, resolve)),
    new Promise((resolve) => chrome.runtime.sendMessage({ type: "pushNow" }, resolve)),
  ]).then(([cfg, status]) => {
    render(status, cfg);
    btn.disabled = false;
    btn.textContent = "Push now";
  });
});

loadAndRender();
