"use strict";

// Local demo accounts. These users only exist in the seeded mock database.
const DEMO_PASSWORD = "MatchCheck-Demo-2026";
const DEMO_USERS = [
  ["priya@datecrew.test", "Priya, Matchmaker A"],
  ["rahul@datecrew.test", "Rahul, Matchmaker B"],
  ["meera@datecrew.test", "Meera, Lead"],
  ["sam@northstar.test", "Sam, other agency"],
];

const LABELS = {
  seeking_gender: "Looking for", age_range: "Age", religions: "Religion", diets: "Diet",
  smoking: "Smoking", wants_children: "Children", cities: "Location",
};

const state = { access: null, user: null, clientId: null, clients: [] };

// ------------------------------------------------------------ DOM helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);
const pretty = (s) => String(s).replace(/_/g, " ");
const fmt = (n) => Number(n).toLocaleString();

function badge(status) {
  return h("span", { class: `badge ${status.toLowerCase()}` }, status);
}

// ------------------------------------------------------------ session storage (best effort)

const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
};

// ------------------------------------------------------------ API client

class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : (detail && detail.message) || "Request failed");
    this.status = status;
    this.detail = detail;
  }
}

async function refreshTokens() {
  const token = store.get("refresh");
  if (!token) return false;
  const res = await fetch("/api/auth/refresh", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: token }),
  });
  if (!res.ok) return false;
  setSession(await res.json());
  return true;
}

async function api(path, { method = "GET", body, retry = true } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...(state.access ? { Authorization: `Bearer ${state.access}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && retry && (await refreshTokens())) return api(path, { method, body, retry: false });
  if (res.status === 401) { logout(); throw new ApiError(401, "Session expired"); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.detail);
  return data;
}

function setSession(tok) {
  state.access = tok.access_token;
  state.user = tok.user;
  store.set("refresh", tok.refresh_token);
}

// ------------------------------------------------------------ auth views

function showLogin() {
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  const box = $("demo-buttons");
  box.replaceChildren(...DEMO_USERS.map(([email, label]) =>
    h("button", { type: "button", class: "ghost", onclick: () => {
      $("login-email").value = email; $("login-password").value = DEMO_PASSWORD;
    } }, label)));
}

async function onLogin(e) {
  e.preventDefault();
  $("login-error").hidden = true;
  try {
    const tok = await api("/api/auth/login", {
      method: "POST", retry: false,
      body: { email: $("login-email").value.trim(), password: $("login-password").value },
    });
    setSession(tok);
    $("login-password").value = "";
    startApp();
  } catch (err) {
    $("login-error").textContent = err.status === 429 ? "Too many attempts. Wait a minute and try again." : err.message;
    $("login-error").hidden = false;
  }
}

async function logout() {
  const token = store.get("refresh");
  if (token && state.access) {
    fetch("/api/auth/logout", {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.access}` },
      body: JSON.stringify({ refresh_token: token }),
    }).catch(() => {});
  }
  store.del("refresh");
  Object.assign(state, { access: null, user: null, clientId: null, clients: [] });
  showLogin();
}

function startApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  $("who").textContent = `${state.user.name} · ${pretty(state.user.role)} · ${state.user.tenant}`;
  $("audit-tab").hidden = state.user.role === "matchmaker";
  $("scope-note").textContent = state.user.role === "matchmaker"
    ? "You only see clients assigned to you."
    : "You see every client in your agency.";
  switchTab("match");
  loadClients();
}

// ------------------------------------------------------------ tabs

function switchTab(name) {
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".tab-panel")) p.hidden = p.id !== `tab-${name}`;
  if (name === "insights") loadInsights();
  if (name === "audit") loadAudit();
}

// ------------------------------------------------------------ clients

async function loadClients() {
  const { data } = await api("/api/clients");
  state.clients = data;
  const list = $("client-list");
  list.replaceChildren(...data.map((c) => h("li", {},
    h("button", { class: `client-btn ${c.id === state.clientId ? "active" : ""}`, onclick: () => openClient(c.id) },
      h("strong", {}, c.name),
      h("span", { class: "muted small" }, `${c.age} · ${c.city}${state.user.role !== "matchmaker" ? " · " + c.matchmaker_name : ""}`)))));
  if (!data.length) list.append(h("li", { class: "muted" }, "No clients."));
  if (data.length && !state.clientId) openClient(data[0].id);
}

async function openClient(id) {
  state.clientId = id;
  for (const b of document.querySelectorAll(".client-btn")) b.classList.remove("active");
  const idx = state.clients.findIndex((c) => c.id === id);
  document.querySelectorAll(".client-btn")[idx]?.classList.add("active");

  const detail = $("client-detail");
  detail.replaceChildren(h("p", { class: "muted" }, "Loading..."));
  const [client, shortlist, shares] = await Promise.all([
    api(`/api/clients/${id}`), api(`/api/clients/${id}/shortlist?limit=25`), api(`/api/clients/${id}/shares`),
  ]);
  if (state.clientId !== id) return;
  detail.replaceChildren(
    renderClientHeader(client),
    renderCard(client.preference_card),
    renderChecker(client),
    renderShortlist(client, shortlist),
    renderShares(client, shares.data),
  );
}

function renderClientHeader(c) {
  return h("div", { class: "client-head" },
    h("div", {}, h("h2", {}, c.name), h("p", { class: "muted" }, `${pretty(c.gender)}, ${c.age}, ${c.city} · matchmaker: ${c.matchmaker_name}`)),
    c.notes ? h("p", { class: "note" }, c.notes) : null);
}

function hardChips(hard) {
  const chips = [];
  if (hard.seeking_gender) chips.push(["Looking for", hard.seeking_gender]);
  if (hard.age_min != null) chips.push(["Age", `${hard.age_min} to ${hard.age_max}`]);
  for (const k of ["religions", "diets", "smoking", "wants_children", "cities"]) {
    if (hard[k]) chips.push([LABELS[k], hard[k].map(pretty).join(", ") + (k === "cities" ? " or will relocate" : "")]);
  }
  return chips;
}

function softText(p) {
  const v = Array.isArray(p.value) ? p.value.map(pretty).join(", ") : pretty(p.value);
  const names = { min_height_cm: "Height at least", max_height_cm: "Height at most", min_education: "Education at least", professions: "Profession", drinking: "Drinking" };
  return `${names[p.key] || p.key}: ${v}${p.key.includes("height") ? " cm" : ""}`;
}

function renderCard(card) {
  return h("div", { class: "card" },
    h("div", { class: "card-title" }, h("h3", {}, "Preference card"), h("span", { class: "muted small" }, `version ${card.version}`)),
    h("p", { class: "label" }, "Deal-breakers (block the share)"),
    h("div", { class: "chips" }, hardChips(card.hard).map(([k, v]) => h("span", { class: "chip hard" }, h("b", {}, `${k}: `), v))),
    h("p", { class: "label" }, "Soft preferences (weighted fit score)"),
    h("div", { class: "chips" }, card.soft.map((p) => h("span", { class: "chip" }, softText(p), h("span", { class: "weight" }, `x${p.weight}`)))));
}

// ------------------------------------------------------------ checker and sharing

function renderChecker(client) {
  const input = h("input", { placeholder: "Candidate ID, for example TDC-00042", maxlength: "64" });
  const out = h("div", { class: "check-out" });
  const run = async () => {
    const id = input.value.trim().toUpperCase();
    if (!id) return;
    out.replaceChildren(h("p", { class: "muted" }, "Checking..."));
    try {
      const res = await api("/api/match/check", { method: "POST", body: { client_id: client.id, candidate_id: id } });
      out.replaceChildren(renderCheckResult(client, res));
    } catch (err) {
      out.replaceChildren(h("p", { class: "error" }, err.status === 404 ? "No candidate with that ID in your agency." : err.message));
    }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  return h("div", { class: "card" },
    h("h3", {}, "Check a specific profile before sharing"),
    h("p", { class: "muted small" }, "Paste any candidate ID. The check runs the client's deal-breakers and soft preferences."),
    h("div", { class: "row" }, input, h("button", { class: "primary", onclick: run }, "Check")),
    out);
}

function candidateLine(k) {
  return `${k.age}, ${k.city}, ${pretty(k.religion)}, ${pretty(k.diet)}, smokes ${k.smoking}, children: ${k.wants_children}, ${k.height_cm} cm, ${pretty(k.education)}, ${pretty(k.profession)}${k.open_to_relocate ? ", will relocate" : ""}`;
}

function renderCheckResult(client, res) {
  const k = res.candidate;
  const box = h("div", { class: `result ${res.status.toLowerCase()}` },
    h("div", { class: "result-head" }, badge(res.status), h("strong", {}, `${k.name} (${k.id})`), h("span", { class: "score" }, `fit ${res.fit_score}`)),
    h("p", { class: "small" }, candidateLine(k)),
    res.violations.length ? h("ul", { class: "reasons" }, res.violations.map((v) => h("li", {}, h("b", {}, `${v.label}: `), v.reason))) : null,
    res.soft_mismatches.length ? h("p", { class: "small muted" }, "Soft mismatches: " + res.soft_mismatches.join("; ")) : null);
  box.append(shareControls(client, k.id, res.status));
  return box;
}

function shareControls(client, candidateId, status) {
  const wrap = h("div", { class: "share-controls" });
  const msg = h("span", { class: "small" });
  const doShare = async (reason) => {
    try {
      await api("/api/shares", { method: "POST", body: { client_id: client.id, candidate_id: candidateId, override_reason: reason || null } });
      msg.textContent = "Shared. Recorded below.";
      setTimeout(() => openClient(client.id), 600);
    } catch (err) {
      msg.textContent = err.status === 409 ? "Blocked: add a reason to override." : err.message;
    }
  };
  if (status === "BLOCK") {
    const reason = h("input", { placeholder: "Override reason (logged), at least 5 characters", maxlength: "500" });
    wrap.append(reason, h("button", { class: "danger", onclick: () => doShare(reason.value.trim()) }, "Share anyway"), msg);
  } else {
    wrap.append(h("button", { class: "primary", onclick: () => doShare(null) }, "Share with client"), msg);
  }
  return wrap;
}

function renderShortlist(client, sl) {
  const m = sl.meta;
  const rows = sl.data.map((r) => {
    const k = r.candidate;
    return h("tr", {},
      h("td", {}, h("strong", {}, k.name), h("div", { class: "muted small" }, k.id)),
      h("td", {}, `${k.age}`), h("td", {}, k.city + (k.open_to_relocate ? " *" : "")),
      h("td", {}, pretty(k.religion)), h("td", {}, pretty(k.diet)), h("td", {}, k.smoking), h("td", {}, k.wants_children),
      h("td", {}, h("div", { class: "bar" }, h("span", { style: null, "data-w": r.fit_score })), h("span", { class: "small" }, r.fit_score)),
      h("td", {}, badge(r.status), r.soft_mismatches.length ? h("div", { class: "muted small" }, r.soft_mismatches.join("; ")) : null),
      h("td", {}, h("button", { class: "ghost", onclick: async () => {
        const res = await api("/api/match/check", { method: "POST", body: { client_id: client.id, candidate_id: k.id } });
        const out = document.querySelector(".check-out");
        out.replaceChildren(renderCheckResult(client, res));
        out.scrollIntoView({ behavior: "smooth", block: "center" });
      } }, "Review")));
  });
  const table = h("table", {},
    h("thead", {}, h("tr", {}, ["Candidate", "Age", "City", "Religion", "Diet", "Smoking", "Children", "Fit", "Status", ""].map((t) => h("th", {}, t)))),
    h("tbody", {}, rows));
  // Bar widths are set through the DOM, not inline style attributes, to keep the CSP strict.
  for (const s of table.querySelectorAll(".bar span")) s.style.width = `${s.dataset.w}%`;
  return h("div", { class: "card" },
    h("div", { class: "card-title" }, h("h3", {}, "Ranked shortlist"),
      h("span", { class: "perf" }, `${fmt(m.pool_size)} profiles → ${fmt(m.passed_hard_filters)} pass deal-breakers → top ${sl.data.length} in ${m.request_ms} ms${m.cache_hit ? " (cached)" : ""}`)),
    sl.data.length ? h("div", { class: "table-wrap" }, table) : h("p", { class: "muted" }, "No candidates pass every deal-breaker. Review the preference card."),
    h("p", { class: "muted small" }, "* open to relocating. Profiles already shared with this client are excluded."));
}

// ------------------------------------------------------------ shares and the feedback loop

function renderShares(client, shares) {
  const live = shares.filter((s) => !s.is_historical);
  const hist = shares.filter((s) => s.is_historical);
  const row = (s) => {
    const actions = h("td", {});
    if (s.outcome === "pending") {
      const fb = h("input", { placeholder: "Client feedback if rejected", maxlength: "2000" });
      actions.append(
        h("button", { class: "ghost", onclick: () => recordOutcome(client, s.id, "accepted") }, "Accepted"),
        fb, h("button", { class: "ghost", onclick: () => fb.value.trim() ? recordOutcome(client, s.id, "rejected", fb.value.trim()) : fb.focus() }, "Rejected"));
    } else if (s.outcome === "accepted") {
      const out = h("span", { class: "small" });
      actions.append(h("button", { class: "ghost", onclick: async () => {
        try { const c = await api(`/api/shares/${s.id}/reveal-contact`, { method: "POST" }); out.textContent = ` ${c.phone} · ${c.email}`; }
        catch (err) { out.textContent = err.message; }
      } }, "Reveal contact"), out);
    } else if (s.rejection_id) {
      actions.append(h("button", { class: "ghost", onclick: () => showRejection(client, s.rejection_id) }, "View reason"));
    }
    return h("tr", {},
      h("td", {}, s.candidate_name, h("div", { class: "muted small" }, s.candidate_id)),
      h("td", {}, badge(s.check_status), s.override_reason ? h("div", { class: "small warn-text" }, `override: ${s.override_reason}`) : null),
      h("td", {}, h("span", { class: `outcome ${s.outcome}` }, s.outcome)),
      h("td", {}, s.reason_code ? pretty(s.reason_code) + (s.reason_detail ? ` (${LABELS[s.reason_detail] || s.reason_detail})` : "") : ""),
      actions);
  };
  const table = (list) => h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Candidate", "Check at share time", "Outcome", "Classified reason", ""].map((t) => h("th", {}, t)))),
    h("tbody", {}, list.map(row))));
  return h("div", { class: "card", id: "shares-card" },
    h("h3", {}, "Shared profiles"),
    h("div", { id: "rejection-panel" }),
    live.length ? table(live) : h("p", { class: "muted small" }, "Nothing shared from this tool yet."),
    hist.length ? h("details", {}, h("summary", {}, `Last 30 days before the tool (${hist.length})`), table(hist)) : null);
}

async function recordOutcome(client, shareId, outcome, feedback) {
  const res = await api(`/api/shares/${shareId}/outcome`, { method: "POST", body: { outcome, feedback: feedback || null } });
  if (res.rejection_id) await showRejection(client, res.rejection_id, true);
  else openClient(client.id);
}

async function showRejection(client, rejectionId, poll = false) {
  const panel = $("rejection-panel");
  panel.replaceChildren(h("p", { class: "muted" }, "Classifying feedback..."));
  let r;
  for (let i = 0; i < 20; i++) {
    r = await api(`/api/rejections/${rejectionId}`);
    if (r.status !== "queued" || !poll) break;
    await new Promise((ok) => setTimeout(ok, 400));
  }
  const box = h("div", { class: "result rejection" },
    h("p", {}, h("b", {}, "Client said: "), `"${r.text}"`),
    r.status === "done"
      ? h("p", {}, h("b", {}, "Reason: "), pretty(r.reason_code), r.reason_detail ? ` (${LABELS[r.reason_detail]})` : "",
          h("span", { class: "muted small" }, `  ${r.classifier} classifier, confidence ${r.confidence}`))
      : h("p", { class: "error" }, `Classification ${r.status}.`));
  if (r.reason_code === "preference_violation") {
    box.append(h("p", { class: "small warn-text" }, "Avoidable: this deal-breaker was already on the card. The checker blocks this case now."));
  }
  if (r.suggestion) {
    const s = r.suggestion;
    box.append(h("p", { class: "small" }, s.message));
    if (s.proposed) {
      box.append(h("button", { class: "primary", onclick: () => addDealBreaker(client, s.rule, s.proposed) },
        `Add deal-breaker: ${s.label} = ${s.proposed.map(pretty).join(", ")}`));
    }
  }
  panel.replaceChildren(box);
  if (poll) {
    const shares = await api(`/api/clients/${client.id}/shares`);
    const card = $("shares-card");
    const fresh = renderShares(client, shares.data);
    fresh.querySelector("#rejection-panel").replaceWith(panel);
    card.replaceWith(fresh);
  }
}

async function addDealBreaker(client, rule, value) {
  const current = await api(`/api/clients/${client.id}`);
  const card = current.preference_card;
  const hard = { ...card.hard, [rule]: value };
  await api(`/api/clients/${client.id}/preference-card`, { method: "PUT", body: { hard, soft: card.soft } });
  openClient(client.id);
}

// ------------------------------------------------------------ insights

function bars(entries, max) {
  const top = max || Math.max(1, ...entries.map((e) => e[1]));
  const list = h("div", { class: "bars" }, entries.map(([label, n, cls]) =>
    h("div", { class: "bar-row" }, h("span", { class: "bar-label" }, label),
      h("div", { class: `bar wide ${cls || ""}` }, h("span", { "data-w": Math.round((100 * n) / top) })), h("span", { class: "bar-n" }, fmt(n)))));
  for (const s of list.querySelectorAll(".bar span")) s.style.width = `${s.dataset.w}%`;
  return list;
}

async function loadInsights() {
  const box = $("insights");
  box.replaceChildren(h("p", { class: "muted" }, "Loading..."));
  const [bt, dash] = await Promise.all([api("/api/backtest"), api("/api/dashboard")]);
  const reasons = dash.reasons.map((r) => [
    r.reason_detail ? `${pretty(r.reason_code)}: ${LABELS[r.reason_detail] || r.reason_detail}` : pretty(r.reason_code),
    r.n, r.reason_code === "preference_violation" ? "bad" : r.reason_code === "new_preference" ? "warn" : "",
  ]);
  const totalRejected = dash.reasons.reduce((a, r) => a + r.n, 0);
  const avoidable = dash.reasons.filter((r) => r.reason_code === "preference_violation").reduce((a, r) => a + r.n, 0);

  box.replaceChildren(
    h("div", { class: "card" },
      h("h3", {}, "Backtest: last 30 days replayed through the checker"),
      h("p", { class: "muted small" }, "Every profile shared before the tool existed, checked against today's preference cards."),
      bt.shared ? h("div", { class: "kpis" },
        kpi(fmt(bt.shared), "profiles shared"),
        kpi(fmt(bt.blocked), "would have been blocked"),
        kpi(fmt(bt.blocked_accepted), "blocked profiles the client actually accepted"),
        kpi(`${bt.acceptance_before}% → ${bt.acceptance_after}%`, "acceptance rate, same accepted profiles")) : h("p", { class: "muted" }, "No history in your scope."),
      bt.by_matchmaker.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Matchmaker", "Shared", "Accepted", "Would block", "Acceptance now", "With checker"].map((t) => h("th", {}, t)))),
        h("tbody", {}, bt.by_matchmaker.map((m) => h("tr", {},
          h("td", {}, m.matchmaker), h("td", {}, fmt(m.shared)), h("td", {}, fmt(m.accepted)), h("td", {}, fmt(m.blocked)),
          h("td", {}, `${m.acceptance_before}%`), h("td", {}, h("strong", {}, `${m.acceptance_after}%`))))))) : null,
      Object.keys(bt.blocked_by_rule).length ? h("div", {}, h("p", { class: "label" }, "Which deal-breakers were ignored"), bars(Object.entries(bt.blocked_by_rule))) : null),
    h("div", { class: "card" },
      h("h3", {}, "Why clients reject profiles"),
      h("p", { class: "muted small" }, `${fmt(totalRejected)} rejections classified from free text. ${totalRejected ? Math.round((100 * avoidable) / totalRejected) : 0}% broke a deal-breaker the client had already given us.`),
      bars(reasons)));
}

function kpi(value, label) {
  return h("div", { class: "kpi" }, h("div", { class: "kpi-v" }, value), h("div", { class: "kpi-l" }, label));
}

// ------------------------------------------------------------ audit

async function loadAudit() {
  const box = $("audit");
  const { data } = await api("/api/audit");
  box.replaceChildren(h("div", { class: "card" },
    h("h3", {}, "Audit log"),
    h("p", { class: "muted small" }, "Logins, shares, overrides, outcome changes, preference edits and contact reveals."),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["When", "Who", "Action", "Entity", "Detail"].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.map((a) => h("tr", {},
        h("td", { class: "small" }, new Date(a.at).toLocaleString()), h("td", {}, a.actor), h("td", {}, pretty(a.action)),
        h("td", { class: "small" }, `${a.entity} ${a.entity_id ? a.entity_id.slice(0, 8) : ""}`), h("td", { class: "small" }, a.detail || ""))))))));
}

// ------------------------------------------------------------ boot

document.addEventListener("DOMContentLoaded", async () => {
  $("login-form").addEventListener("submit", onLogin);
  $("logout").addEventListener("click", logout);
  for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => switchTab(b.dataset.tab));
  if (await refreshTokens().catch(() => false)) startApp();
  else showLogin();
});
