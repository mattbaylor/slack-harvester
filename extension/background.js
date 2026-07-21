// Slack Harvester Creds — Background Service Worker (ISSUES #17)
//
// Productionized from prototype/creds-extension-probe (SQ1+SQ2 proven). Reads
// the LIVE Slack session and pushes credentials to the local harvester:
//
//   - d cookie   via chrome.cookies.getAll({domain: '.slack.com'})   (proven)
//   - xoxc token via chrome.scripting against a Slack tab's localStorage
//                  key "localConfig_v2", regex /xoxc-[A-Za-z0-9._-]{40,}/  (proven)
//
// On an alarm (config.refreshIntervalMinutes, default 5) and on cookie change,
// it reads both and POSTs {token, cookie} to
//   http://127.0.0.1:<harvesterPort>/creds
// with  Authorization: Bearer <apiToken>  (the harvester's #14 bearer token).
//
// If the token is unreadable / logged out, it fires a chrome.notifications
// alert and records a logged-out state the popup surfaces (O2). It manages a
// Slack tab to keep the session warm rather than hoping one is open.
//
// All state the popup reads lives in chrome.storage.local under "lastStatus".
// NEVER stores or logs the token/cookie VALUES — only lengths and outcomes.

const SLACK_TAB_URL = "https://app.slack.com/";
const SLACK_TAB_MATCH = ["https://*.slack.com/*"];
const DEFAULT_REFRESH_MINUTES = 5;
const ALARM_NAME = "creds-refresh";
const LOGOUT_NOTIFICATION_ID = "slack-harvester-logout";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

/** @type {{harvesterPort:number, refreshIntervalMinutes:number, apiToken:string}|null} */
let config = null;

async function loadConfig() {
  try {
    const resp = await fetch(chrome.runtime.getURL("config.json"));
    config = await resp.json();
  } catch {
    console.error(
      "Slack Harvester Creds: config.json not found. Copy config.example.json " +
        "to config.json and paste your harvester api-token into it."
    );
    config = null;
  }
  return config;
}

function credsUrl() {
  if (!config) return null;
  const port = config.harvesterPort || 7777;
  return `http://127.0.0.1:${port}/creds`;
}

// ---------------------------------------------------------------------------
// Slack tab management (keep-warm) — ensure a signed-in Slack tab exists
// ---------------------------------------------------------------------------

async function findSlackTab() {
  const tabs = await chrome.tabs.query({ url: SLACK_TAB_MATCH });
  return tabs.length ? tabs[0] : null;
}

// Ensure a Slack tab exists so the session stays warm and the token is
// readable. Creates one (unfocused, in the background) if none is open. This
// is the "managed tab" the prototype flagged as needed — we don't just hope a
// tab is open. Returns the tab or null if creation failed.
async function ensureSlackTab() {
  let tab = await findSlackTab();
  if (tab) return tab;
  try {
    tab = await chrome.tabs.create({ url: SLACK_TAB_URL, active: false });
    // Give the page a moment to load its localStorage before the first read.
    // Subsequent alarm cycles will find it already warm.
    return tab;
  } catch (e) {
    console.error("Slack Harvester Creds: could not open a Slack tab:", e.message);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Reads — xoxc from page localStorage, d cookie via chrome.cookies (proven)
// ---------------------------------------------------------------------------

async function readTokenFromPage(tabId) {
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        // Slack stores config (incl. the token) in localStorage.localConfig_v2.
        // Defensively scan the whole blob for an xoxc- token.
        try {
          const raw = localStorage.getItem("localConfig_v2");
          if (!raw) return { ok: false, reason: "no localConfig_v2 key" };
          const m = raw.match(/xoxc-[A-Za-z0-9._-]{40,}/);
          return m
            ? { ok: true, token: m[0] }
            : { ok: false, reason: "no xoxc- in localConfig_v2" };
        } catch (e) {
          return { ok: false, reason: "exception: " + e.message };
        }
      },
    });
    return res && res.result ? res.result : { ok: false, reason: "no result" };
  } catch (e) {
    return { ok: false, reason: "executeScript failed: " + e.message };
  }
}

async function readDCookie() {
  try {
    const all = await chrome.cookies.getAll({ domain: ".slack.com" });
    const d = all.find((c) => c.name === "d");
    return d && d.value
      ? { ok: true, cookie: d.value }
      : { ok: false, reason: "no d cookie" };
  } catch (e) {
    return { ok: false, reason: "cookies.getAll failed: " + e.message };
  }
}

// ---------------------------------------------------------------------------
// Push to the harvester's POST /creds (bearer-authed)
// ---------------------------------------------------------------------------

async function pushCreds(token, cookie) {
  const url = credsUrl();
  if (!url) return { ok: false, error: "no config" };
  if (!config.apiToken) return { ok: false, error: "no apiToken in config" };

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.apiToken}`,
      },
      body: JSON.stringify({ token, cookie }),
    });
    if (resp.status === 200) return { ok: true, status: 200 };
    if (resp.status === 401) return { ok: false, status: 401, error: "unauthorized (check apiToken)" };
    return { ok: false, status: resp.status, error: `harvester returned ${resp.status}` };
  } catch (e) {
    // Harvester not running / unreachable — creds are still live in the browser.
    return { ok: false, error: "unreachable: " + e.message };
  }
}

// ---------------------------------------------------------------------------
// Logout detection + alert (O2)
// ---------------------------------------------------------------------------

function fireLogoutAlert(reason) {
  try {
    chrome.notifications.create(LOGOUT_NOTIFICATION_ID, {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title: "Slack Harvester: you're logged out",
      message:
        "Re-sign-in needed — the Slack token couldn't be read (" +
        reason +
        "). Capture is paused until you sign back into Slack web.",
      priority: 2,
    });
  } catch (e) {
    console.error("Slack Harvester Creds: notification failed:", e.message);
  }
}

function clearLogoutAlert() {
  try {
    chrome.notifications.clear(LOGOUT_NOTIFICATION_ID);
  } catch {
    /* no-op */
  }
}

// ---------------------------------------------------------------------------
// State (popup reads this) — NEVER stores token/cookie VALUES
// ---------------------------------------------------------------------------

async function recordStatus(status) {
  await chrome.storage.local.set({ lastStatus: status });
  updateBadge(status);
}

function updateBadge(status) {
  if (status.loggedIn && status.lastPush && status.lastPush.ok) {
    chrome.action.setBadgeText({ text: "OK" });
    chrome.action.setBadgeBackgroundColor({ color: "#22c55e" });
  } else if (!status.loggedIn) {
    chrome.action.setBadgeText({ text: "OUT" });
    chrome.action.setBadgeBackgroundColor({ color: "#ef4444" });
  } else {
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#f59e0b" });
  }
}

// ---------------------------------------------------------------------------
// Refresh cycle — the heart of read + push + alert
// ---------------------------------------------------------------------------

async function refresh(trigger) {
  if (!config) await loadConfig();
  const at = new Date().toISOString();
  const status = { at, trigger, loggedIn: false };

  const tab = await ensureSlackTab();
  if (!tab) {
    status.note = "No Slack tab and could not open one.";
    status.loggedIn = false;
    fireLogoutAlert("no Slack tab");
    await recordStatus(status);
    return status;
  }

  const tok = await readTokenFromPage(tab.id);
  const cok = await readDCookie();
  status.tokenLen = tok.ok ? tok.token.length : 0;
  status.cookieLen = cok.ok ? cok.cookie.length : 0;

  if (!tok.ok || !cok.ok) {
    // Token unreadable or cookie gone => treat as logged out (O2). A freshly
    // opened tab may not have loaded localStorage yet — the next alarm retries;
    // but we still surface + alert so a real logout isn't silent.
    status.loggedIn = false;
    status.note = `token: ${tok.ok ? "ok" : tok.reason}; cookie: ${cok.ok ? "ok" : cok.reason}`;
    fireLogoutAlert(tok.ok ? cok.reason : tok.reason);
    await recordStatus(status);
    return status;
  }

  // Both present => logged in. Clear any stale logout alert and push.
  status.loggedIn = true;
  clearLogoutAlert();
  const push = await pushCreds(tok.token, cok.cookie);
  status.lastPush = { ok: push.ok, status: push.status || null, error: push.error || null, at };
  await recordStatus(status);
  return status;
}

// ---------------------------------------------------------------------------
// Alarms — periodic keep-warm refresh (proven in prototype)
// ---------------------------------------------------------------------------

async function setupAlarm() {
  if (!config) await loadConfig();
  const minutes = (config && config.refreshIntervalMinutes) || DEFAULT_REFRESH_MINUTES;
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: minutes });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) refresh("alarm");
});

// Refresh when the d cookie changes (login/logout/rotation is observable).
chrome.cookies.onChanged.addListener((changeInfo) => {
  if (changeInfo.cookie && changeInfo.cookie.name === "d" &&
      (changeInfo.cookie.domain || "").endsWith("slack.com")) {
    refresh("cookie-change");
  }
});

// ---------------------------------------------------------------------------
// Messages from the popup
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "pushNow") {
    refresh("manual").then(sendResponse);
    return true; // async
  }
  if (msg && msg.type === "getStatus") {
    chrome.storage.local.get("lastStatus", (data) => sendResponse(data.lastStatus || null));
    return true;
  }
  if (msg && msg.type === "getConfig") {
    (async () => {
      if (!config) await loadConfig();
      // Never hand the apiToken to the popup; only report whether it's set.
      sendResponse(
        config
          ? {
              harvesterPort: config.harvesterPort || 7777,
              refreshIntervalMinutes: config.refreshIntervalMinutes || DEFAULT_REFRESH_MINUTES,
              hasApiToken: !!config.apiToken,
            }
          : null
      );
    })();
    return true;
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  await loadConfig();
  if (!config) {
    chrome.action.setBadgeText({ text: "CFG" });
    chrome.action.setBadgeBackgroundColor({ color: "#f59e0b" });
    return;
  }
  await setupAlarm();
  await refresh("init");
}

chrome.runtime.onInstalled.addListener(() => init());
chrome.runtime.onStartup.addListener(() => init());
init();
