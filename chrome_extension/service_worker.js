try {
  importScripts("bridge_config.js");
} catch (_) {
}

const cfg = globalThis.PORTAMCP_BRIDGE || null;
const attached = new Set();
let ws = null;
let reconnectTimer = null;
let heartbeatTimer = null;

function runtimeError() {
  return chrome.runtime.lastError ? new Error(chrome.runtime.lastError.message) : null;
}

function tabsQuery(queryInfo) {
  return new Promise((resolve, reject) => chrome.tabs.query(queryInfo, (tabs) => {
    const err = runtimeError();
    if (err) reject(err); else resolve(tabs);
  }));
}

function tabsUpdate(tabId, updateProperties) {
  return new Promise((resolve, reject) => chrome.tabs.update(tabId, updateProperties, (tab) => {
    const err = runtimeError();
    if (err) reject(err); else resolve(tab);
  }));
}

function windowsUpdate(windowId, updateInfo) {
  return new Promise((resolve, reject) => chrome.windows.update(windowId, updateInfo, (win) => {
    const err = runtimeError();
    if (err) reject(err); else resolve(win);
  }));
}

function debuggerAttach(tabId) {
  return new Promise((resolve, reject) => chrome.debugger.attach({tabId}, "1.3", () => {
    const err = runtimeError();
    if (err) reject(err); else resolve();
  }));
}

function debuggerDetach(tabId) {
  return new Promise((resolve, reject) => chrome.debugger.detach({tabId}, () => {
    const err = runtimeError();
    if (err) reject(err); else resolve();
  }));
}

function debuggerCommand(tabId, method, params = {}) {
  return new Promise((resolve, reject) => chrome.debugger.sendCommand({tabId}, method, params, (result) => {
    const err = runtimeError();
    if (err) reject(err); else resolve(result);
  }));
}

async function ensureAttached(tabId) {
  if (attached.has(tabId)) return;
  await debuggerAttach(tabId);
  attached.add(tabId);
  try {
    await debuggerCommand(tabId, "Runtime.enable");
    await debuggerCommand(tabId, "Page.enable");
  } catch (_) {}
}

async function detachOne(tabId) {
  if (!attached.has(tabId)) return;
  try { await debuggerDetach(tabId); } catch (_) {}
  attached.delete(tabId);
}

async function detachAll() {
  for (const tabId of Array.from(attached)) await detachOne(tabId);
}

async function evaluate(tabId, expression) {
  await ensureAttached(tabId);
  const result = await debuggerCommand(tabId, "Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true
  });
  if (result && result.exceptionDetails) {
    const text = result.exceptionDetails.exception?.description || result.exceptionDetails.text || "JavaScript evaluation failed";
    throw new Error(text);
  }
  const remote = result ? result.result : null;
  if (!remote) return null;
  if (Object.prototype.hasOwnProperty.call(remote, "value")) return remote.value;
  return {type: remote.type, subtype: remote.subtype, description: remote.description};
}

function snapshotExpression(args) {
  const opts = JSON.stringify({
    max_elements: Math.max(1, Math.min(Number(args.max_elements || 300), 1000)),
    include_body_text: args.include_body_text !== false
  });
  return `(() => {
    const opts = ${opts};
    const sel = 'a,button,input,textarea,select,[role],[contenteditable="true"]';
    const elements = Array.from(document.querySelectorAll(sel)).slice(0, opts.max_elements).map((e, i) => {
      const r = e.getBoundingClientRect();
      const style = getComputedStyle(e);
      const visible = !!(r.width && r.height) && style.visibility !== 'hidden' && style.display !== 'none';
      return {
        i,
        tag: e.tagName.toLowerCase(),
        role: e.getAttribute('role'),
        name: e.getAttribute('aria-label') || e.getAttribute('name') || e.innerText || e.value || '',
        type: e.getAttribute('type'),
        id: e.id || '',
        placeholder: e.getAttribute('placeholder') || '',
        visible,
        rect: [r.x, r.y, r.width, r.height]
      };
    });
    return {
      url: location.href,
      title: document.title,
      elements,
      body_text: opts.include_body_text ? ((document.body && document.body.innerText) || '').slice(0, 100000) : ''
    };
  })()`;
}

function clickExpression(args) {
  const opts = JSON.stringify(args || {});
  return `(() => {
    const o = ${opts};
    let el = null;
    if (o.selector) {
      el = document.querySelector(o.selector);
    } else {
      const candidates = Array.from(document.querySelectorAll('a,button,input,[role],[contenteditable="true"]'));
      if (o.role) {
        const wantedName = String(o.name || '').toLowerCase();
        el = candidates.find(e => {
          const role = (e.getAttribute('role') || '').toLowerCase();
          const name = (e.getAttribute('aria-label') || e.innerText || e.value || '').trim().toLowerCase();
          return role === String(o.role).toLowerCase() && (!wantedName || name.includes(wantedName));
        });
      } else if (o.text) {
        const wanted = String(o.text).toLowerCase();
        el = candidates.find(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().toLowerCase().includes(wanted));
      }
    }
    if (!el) throw new Error('Element not found');
    el.scrollIntoView({block:'center', inline:'center'});
    el.focus({preventScroll:true});
    el.click();
    return {tag: el.tagName.toLowerCase(), id: el.id || '', text: (el.innerText || el.value || '').slice(0, 500)};
  })()`;
}

function fillExpression(args) {
  const opts = JSON.stringify(args || {});
  return `(() => {
    const o = ${opts};
    let el = null;
    if (o.selector) el = document.querySelector(o.selector);
    if (!el && o.placeholder) {
      el = Array.from(document.querySelectorAll('input,textarea')).find(e => (e.getAttribute('placeholder') || '') === o.placeholder);
    }
    if (!el && o.label) {
      const wanted = String(o.label).trim().toLowerCase();
      const label = Array.from(document.querySelectorAll('label')).find(l => (l.innerText || '').trim().toLowerCase().includes(wanted));
      if (label) el = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector('input,textarea,select');
    }
    if (!el) throw new Error('Form control not found');
    const value = String(o.value ?? '');
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.focus({preventScroll:true});
    return {tag: el.tagName.toLowerCase(), id: el.id || '', chars: value.length};
  })()`;
}

async function handleRequest(msg) {
  const op = msg.op;
  const args = msg.args || {};
  if (op === "status") {
    return {attached_tabs: Array.from(attached), user_agent: navigator.userAgent};
  }
  if (op === "list_tabs") {
    const tabs = await tabsQuery({});
    return tabs.map(t => ({
      tab_id: t.id,
      window_id: t.windowId,
      index: t.index,
      active: !!t.active,
      pinned: !!t.pinned,
      audible: !!t.audible,
      discarded: !!t.discarded,
      title: t.title || "",
      url: t.url || "",
      status: t.status || ""
    }));
  }
  const tabId = Number(args.tab_id);
  if (!Number.isInteger(tabId)) throw new Error("tab_id must be an integer");

  if (op === "snapshot") return await evaluate(tabId, snapshotExpression(args));
  if (op === "evaluate") {
    const javascript = String(args.javascript || "");
    if (javascript.length > 50000) throw new Error("javascript too long");
    return await evaluate(tabId, javascript);
  }
  if (op === "click") return await evaluate(tabId, clickExpression(args));
  if (op === "fill") return await evaluate(tabId, fillExpression(args));
  if (op === "navigate") {
    const tab = await tabsUpdate(tabId, {url: String(args.url || "about:blank")});
    return {tab_id: tab.id, url: tab.url || "", title: tab.title || ""};
  }
  if (op === "activate") {
    const tab = await tabsUpdate(tabId, {active: true});
    if (tab.windowId !== undefined) await windowsUpdate(tab.windowId, {focused: true});
    return {tab_id: tab.id, window_id: tab.windowId, active: true};
  }
  if (op === "screenshot") {
    await ensureAttached(tabId);
    const params = {format: "png", fromSurface: true, captureBeyondViewport: !!args.full_page};
    if (args.full_page) {
      const metrics = await debuggerCommand(tabId, "Page.getLayoutMetrics");
      const size = metrics.cssContentSize || metrics.contentSize;
      if (size) {
        params.clip = {x: 0, y: 0, width: Math.min(size.width, 16384), height: Math.min(size.height, 16384), scale: 1};
      }
    }
    const result = await debuggerCommand(tabId, "Page.captureScreenshot", params);
    return {base64: result.data};
  }
  if (op === "detach") {
    await detachOne(tabId);
    return {tab_id: tabId, detached: true};
  }
  throw new Error(`Unknown operation: ${op}`);
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 2000);
}

function connect() {
  if (!cfg || !cfg.token) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const url = `ws://${cfg.host}:${cfg.port}/chrome-bridge?token=${encodeURIComponent(cfg.token)}`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    ws.send(JSON.stringify({type: "hello", data: {extension_version: chrome.runtime.getManifest().version, user_agent: navigator.userAgent}}));
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: "heartbeat"}));
    }, 20000);
  };
  ws.onmessage = async (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }
    if (!msg || !msg.id) return;
    try {
      const data = await handleRequest(msg);
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({id: msg.id, ok: true, data}));
    } catch (error) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({id: msg.id, ok: false, error: String(error && error.message ? error.message : error)}));
    }
  };
  ws.onclose = async () => {
    ws = null;
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
    await detachAll();
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

chrome.debugger.onDetach.addListener((source) => {
  if (source && source.tabId !== undefined) attached.delete(source.tabId);
});
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
connect();
