/* Hiring Desk cockpit. Vanilla JS, no build step. Icons: Lucide (app/static/vendor). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, cls = "") => `<i data-lucide="${name}" class="${cls}" aria-hidden="true"></i>`;
const paintIcons = () => window.lucide && window.lucide.createIcons();
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch { /* private mode */ } },
  local(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; } },
};

const state = { token: store.get("token"), user: null, view: "today", jobId: null, selected: new Set(), reviewId: null, counts: {} };

/* ---------- vocabulary: one name per thing, used everywhere ---------- */
const STAGES = [
  ["new", "New", "Resumes that arrived and have not been screened."],
  ["screened", "Screened", "The AI Screener scored these. You decide who to shortlist."],
  ["shortlisted", "Shortlisted", "You want to talk to these people. Schedule interviews."],
  ["interviewing", "Interviewing", "An interview is proposed or done. Collect feedback."],
  ["offered", "Offer sent", "You sent or are preparing an offer."],
  ["rejected", "Not moving forward", "Closed. Send a kind rejection email."],
];
const STAGE_LABEL = Object.fromEntries(STAGES.map(([k, l]) => [k, l]));
const STAGE_SHORT = { ...STAGE_LABEL, rejected: "Closed" };  // for the narrow stage track on Today
const ASSISTANTS = {
  writing: { label: "AI Writer", icon: "pencil", does: "Drafts job descriptions, rejection emails and offer letters." },
  screening: { label: "AI Screener", icon: "list-checks", does: "Reads every resume and scores it against the job's must-haves, with reasons." },
  scheduling: { label: "AI Scheduler", icon: "calendar-plus", does: "Sets up an interview with the times you offer and drafts the invite." },
  feedback: { label: "AI Follow-up", icon: "bell-ring", does: "Spots interviews with missing feedback, drafts reminders, and summarises feedback." },
  comparison: { label: "AI Comparer", icon: "scale", does: "Puts 2 to 4 candidates side by side. It can only read, never change anything." },
  interview_kit: { label: "AI Interview Kit", icon: "clipboard-check", does: "Prepares tailored questions and an evaluation rubric for an interview." },
  recruiter: { label: "AI Recruiter", icon: "user-check", does: "Moves a candidate to another stage, only after you approve it in Copilot." },
  copilot: { label: "Copilot", icon: "message-square-text", does: "Answers your questions in plain language by handing each step to the right assistant." },
};
const assistant = name => ASSISTANTS[name] || { label: name, icon: "sparkles", does: "" };
const DRAFT_KIND = {
  job_description: "Job description", invite: "Interview invite", rejection: "Rejection email",
  offer: "Offer letter", reminder: "Feedback reminder", interview_kit: "Interview kit",
  decision_memo: "Hiring decision memo",
};
const SCOPE_PLAIN = {
  "job:read": "Read jobs", "job:salary:read": "Read salary bands", "candidate:read": "Read candidates and resumes",
  "candidate:write": "Save scores and summaries", "interview:read": "Read interviews", "interview:write": "Create interviews",
  "feedback:read": "Read feedback", "draft:write": "Write drafts for you to review",
  "draft:read": "Read the review queue", "candidate:stage:write": "Change a candidate's stage",
};

/* ---------- plumbing ---------- */
function toast(msg, { error = false, ms = 4200 } = {}) {
  const t = $("#toast");
  t.textContent = msg; t.className = error ? "error" : ""; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms);
}

let refreshing = null;

/* The access token lives 15 minutes; the httpOnly refresh cookie keeps people signed in. */
async function refreshSession() {
  refreshing = refreshing || (async () => {
    try {
      const res = await fetch("/auth/refresh", { method: "POST", credentials: "same-origin" });
      if (!res.ok) return null;
      const data = await res.json();
      state.token = data.access_token;
      state.user = data.user;
      store.set("token", data.access_token);
      return data.access_token;
    } catch { return null; }
    finally { refreshing = null; }
  })();
  return refreshing;
}

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const send = () => {
    const headers = isForm ? {} : { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    return fetch(path, { ...opts, headers, body: isForm ? opts.body : opts.body && JSON.stringify(opts.body) });
  };
  let res = await send();
  const noRenew = ["/auth/login", "/auth/refresh", "/auth/mfa/verify"];
  if (res.status === 401 && !noRenew.includes(path) && await refreshSession()) res = await send();
  if (res.status === 401 && !noRenew.includes(path)) { signOut(); throw new Error("Your session ended. Sign in again."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg).join("; ") : "Something went wrong (" + res.status + ")");
  }
  return data;
}

/* Runs an action from a button, shows progress, and reports errors in plain words. */
async function act(btn, fn, { working = "Working", slow = false } = {}) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${icon("refresh-cw", "spin")} ${esc(working)}`;
  paintIcons();
  if (slow) toast("The AI is working. Free models can take up to a minute per resume.", { ms: 8000 });
  try { await fn(); } catch (e) { toast(e.message, { error: true, ms: 7000 }); }
  finally { if (btn.isConnected) { btn.disabled = false; btn.innerHTML = original; paintIcons(); } }
}

function go(view, extra = {}) { Object.assign(state, extra, { view }); closeDrawer(); render(); window.scrollTo(0, 0); }
function signOut() {
  fetch("/auth/logout", { method: "POST", credentials: "same-origin", keepalive: true }).catch(() => {});
  state.token = null; state.user = null; store.set("token", null); render();
}
async function signOutEverywhere() {
  try { await api("/auth/logout-all", { method: "POST" }); } catch { /* already signed out */ }
  state.token = null; state.user = null; store.set("token", null); render();
}

const fmtWhen = iso => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};
const initials = name => (name || "?").split(/\s+/).map(p => p[0]).slice(0, 2).join("").toUpperCase();
const isPast = iso => iso && new Date(iso) <= new Date();

/* ---------- sign in ---------- */
let authConfig = { oidc_enabled: false, oidc_button_label: "Sign in with SSO" };

function finishLogin(r) {
  state.token = r.access_token; state.user = r.user; store.set("token", r.access_token);
  state.view = r.user.role === "hr_admin" ? "today" : "mine";
  render();
}

function loginShell(slot) {
  $("#app").innerHTML = `
  <div class="login-wrap">
    <section class="login-side">
      <div class="brand" style="padding:0">
        <div class="brand-mark" style="background:var(--primary-ink);color:var(--primary)">${icon("briefcase")}</div>
        <div class="brand-name">Hiring Desk</div>
      </div>
      <div class="stack" style="gap:22px">
        <h1>Run your whole hiring process from one desk.</h1>
        <ul class="login-points">
          <li>${icon("list-checks")}<span>AI assistants read resumes, draft emails and chase feedback for you.</span></li>
          <li>${icon("shield-check")}<span>Nothing reaches a candidate until you approve it.</span></li>
          <li>${icon("eye")}<span>Every AI action is logged, so you can always see why.</span></li>
        </ul>
      </div>
      <p class="small" style="opacity:.8">Prototype. Use sample resumes only.</p>
    </section>
    <section class="login-form">${slot}</section>
  </div>`;
  paintIcons();
}

function renderSignIn() {
  loginShell(`
    <form id="login" novalidate style="display:grid;gap:14px">
      <div><h2>Sign in</h2><p class="muted small" style="margin-top:4px">Use the account your HR admin gave you.</p></div>
      <label class="field">Email<input name="email" type="email" autocomplete="username" required autofocus></label>
      <label class="field">Password<input name="password" type="password" autocomplete="current-password" required></label>
      <p id="login-error" class="small" style="color:var(--bad)" role="alert"></p>
      <button class="btn" type="submit">${icon("log-in")} Sign in</button>
      ${authConfig.oidc_enabled ? `<a class="btn secondary" href="/auth/oidc/login">${icon("shield-check")} ${esc(authConfig.oidc_button_label)}</a>` : ""}
      <button type="button" class="linkish" id="forgot">Forgot your password?</button>
    </form>`);
  $("#login").onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    try {
      const r = await api("/auth/login", { method: "POST", body: { email: f.get("email"), password: f.get("password") } });
      if (r.mfa_required) return renderMfa(r.mfa_token);
      finishLogin(r);
    } catch (err) {
      $("#login-error").textContent = err.message;
      btn.disabled = false;
    }
  };
  $("#forgot").onclick = renderForgot;
}

function renderMfa(mfaToken) {
  loginShell(`
    <form id="mfa" novalidate style="display:grid;gap:14px">
      <div><h2>Enter your code</h2><p class="muted small" style="margin-top:4px">Open your authenticator app and type the 6-digit code. A recovery code also works.</p></div>
      <label class="field">Code<input name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus maxlength="9" placeholder="123456"></label>
      <p id="mfa-error" class="small" style="color:var(--bad)" role="alert"></p>
      <button class="btn" type="submit">${icon("shield-check")} Verify</button>
      <button type="button" class="linkish" id="back">Back to sign in</button>
    </form>`);
  $("#back").onclick = renderSignIn;
  $("#mfa").onsubmit = async e => {
    e.preventDefault();
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    try {
      finishLogin(await api("/auth/mfa/verify", { method: "POST",
        body: { mfa_token: mfaToken, code: new FormData(e.target).get("code").trim() } }));
    } catch (err) {
      $("#mfa-error").textContent = err.message;
      btn.disabled = false;
    }
  };
}

function renderForgot() {
  loginShell(`
    <form id="forgot-form" novalidate style="display:grid;gap:14px">
      <div><h2>Reset your password</h2><p class="muted small" style="margin-top:4px">We will create a one-time link. Ask your HR admin to send it to you if you do not receive one.</p></div>
      <label class="field">Work email<input name="email" type="email" autocomplete="username" required autofocus></label>
      <p id="forgot-msg" class="small" role="status"></p>
      <button class="btn" type="submit">${icon("mail")} Create reset link</button>
      <button type="button" class="linkish" id="back">Back to sign in</button>
    </form>`);
  $("#back").onclick = renderSignIn;
  $("#forgot-form").onsubmit = async e => {
    e.preventDefault();
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    try {
      const r = await api("/auth/forgot-password", { method: "POST",
        body: { email: new FormData(e.target).get("email") } });
      $("#forgot-msg").innerHTML = esc(r.message);
      if (r.reset_token) {
        $("#forgot-msg").innerHTML += `<br><a href="/?reset=${encodeURIComponent(r.reset_token)}">Open the reset link (shown in development only)</a>`;
      }
    } catch (err) {
      $("#forgot-msg").textContent = err.message;
      btn.disabled = false;
    }
  };
}

function renderReset(token) {
  loginShell(`
    <form id="reset-form" novalidate style="display:grid;gap:14px">
      <div><h2>Choose a new password</h2><p class="muted small" style="margin-top:4px">At least 8 characters. Every other session will be signed out.</p></div>
      <label class="field">New password<input name="password" type="password" autocomplete="new-password" minlength="8" required autofocus></label>
      <label class="field">Repeat password<input name="confirm" type="password" autocomplete="new-password" minlength="8" required></label>
      <p id="reset-error" class="small" style="color:var(--bad)" role="alert"></p>
      <button class="btn" type="submit">${icon("key-round")} Save password</button>
    </form>`);
  $("#reset-form").onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (f.get("password") !== f.get("confirm")) {
      $("#reset-error").textContent = "The two passwords are different.";
      return;
    }
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    try {
      await api("/auth/reset-password", { method: "POST",
        body: { token, new_password: f.get("password") } });
      history.replaceState({}, "", "/");
      toast("Password changed. Sign in with the new one.");
      renderSignIn();
    } catch (err) {
      $("#reset-error").textContent = err.message;
      btn.disabled = false;
    }
  };
}

async function viewLogin() {
  loginShell(`<p class="muted small">Loading&hellip;</p>`);
  try { authConfig = await api("/auth/config"); } catch { /* defaults are fine */ }
  const resetToken = new URLSearchParams(location.search).get("reset");
  if (resetToken) return renderReset(resetToken);
  const sso = new URLSearchParams(location.search).get("sso");
  if (sso === "denied") toast("That sign-in was cancelled.", { error: true });
  if (sso === "error") toast("Single sign-on could not verify you. Try again or use your password.", { error: true });
  renderSignIn();
}

/* ---------- shell ---------- */
const HR_NAV = [
  ["today", "Today", "gauge"],
  ["candidates", "Candidates", "users"],
  ["jobs", "Jobs", "briefcase"],
  ["interviews", "Interviews", "calendar-days"],
  ["review", "Review queue", "inbox"],
  null,
  ["activity", "AI activity", "activity"],
  ["team", "Team", "user-cog"],
  ["help", "How it works", "circle-help"],
];
const IV_NAV = [["mine", "My interviews", "calendar-days"], ["help", "How it works", "circle-help"]];

async function render() {
  if (!state.token) return viewLogin();
  if (!state.user) { try { state.user = await api("/auth/me"); } catch { return; } }
  const hr = state.user.role === "hr_admin";
  if (hr) { try { const d = await api("/dashboard"); state.counts = d.needs_you; state.dash = d; } catch { /* shown in view */ } }
  const nav = hr ? HR_NAV : IV_NAV;
  const count = v => v === "review" ? state.counts.drafts_to_review : v === "interviews" ? (state.counts.interviews_to_confirm || 0) + (state.counts.feedback_missing || 0) : 0;
  $("#app").innerHTML = `
  <div class="shell">
    <aside class="side" aria-label="Main">
      <div class="brand"><div class="brand-mark">${icon("briefcase")}</div>
        <div><div class="brand-name">Hiring Desk</div><div class="brand-sub">${hr ? "HR cockpit" : "Interviewer view"}</div></div></div>
      ${hr ? `<button class="copilot-open" id="copilot-open">${icon("message-square-text")}<span>Ask Copilot</span></button>` : ""}
      <nav class="nav">${nav.map(item => item === null ? `<div class="nav-group">Oversight</div>` : `
        <button data-view="${item[0]}" ${state.view === item[0] ? 'aria-current="page"' : ""}>${icon(item[2])}<span>${item[1]}</span>
        ${count(item[0]) ? `<span class="count" title="Waiting on you">${count(item[0])}</span>` : ""}</button>`).join("")}</nav>
      <div class="side-foot">
        <div class="who"><div class="avatar">${esc(initials(state.user.name))}</div>
          <div><div class="who-name">${esc(state.user.name)}</div><div class="who-role">${hr ? "HR admin" : "Interviewer"}</div></div></div>
        <button class="linkish" id="signout">${icon("log-out")} Sign out</button>
      </div>
    </aside>
    <main class="main" id="main" tabindex="-1"></main>
  </div>
  <div id="layer"></div>`;
  $$(".nav button").forEach(b => (b.onclick = () => go(b.dataset.view)));
  $("#signout").onclick = signOut;
  if (hr) $("#copilot-open").onclick = () => copilotOpen();
  const views = { today: viewToday, candidates: viewCandidates, jobs: viewJobs, interviews: viewInterviews, review: viewReview,
    activity: viewActivity, team: viewTeam, help: viewHelp, mine: viewMine };
  try { await (views[state.view] || viewToday)(); }
  catch (e) { $("#main").innerHTML = `<div class="panel empty">${icon("circle-alert")}<h3>This page could not load</h3><p>${esc(e.message)}</p></div>`; }
  paintIcons();
}

const head = (title, lede, actions = "") => `
  <header class="page-head"><div><h1>${title}</h1>${lede ? `<p class="lede">${lede}</p>` : ""}</div>
  ${actions ? `<div class="actions">${actions}</div>` : ""}</header>`;

/* ---------- Today ---------- */
async function viewToday() {
  const d = state.dash || await api("/dashboard");
  const n = d.needs_you;
  const hour = new Date().getHours();
  const greet = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const needs = [
    { k: "drafts_to_review", ic: "inbox", one: "draft waiting for your approval", many: "drafts waiting for your approval",
      clear: "No drafts waiting for approval", btn: "Review drafts", run: () => go("review") },
    { k: "resumes_to_screen", ic: "file-text", one: "resume not screened yet", many: "resumes not screened yet",
      clear: "Every resume has been screened", btn: "Go to candidates", run: () => go("candidates") },
    { k: "interviews_to_confirm", ic: "hourglass", one: "interview waiting for a confirmed time", many: "interviews waiting for a confirmed time",
      clear: "No interview times to confirm", btn: "Confirm times", run: () => go("interviews") },
    { k: "feedback_missing", ic: "bell-ring", one: "interview with no feedback yet", many: "interviews with no feedback yet",
      clear: "No feedback missing", btn: "Draft reminders", run: null },
  ];
  const s = d.setup;
  const steps = [
    ["Create a job", "Write down the role and its must-have skills.", s.has_job, "Create a job", () => openNewJob()],
    ["Add resumes", "Upload .txt or .pdf resumes for that job.", s.has_candidates, "Add resumes", () => go("candidates")],
    ["Screen them with AI", "The AI Screener scores each resume and explains why.", s.has_screened, "Open candidates", () => go("candidates")],
    ["Add an interviewer", "Give a colleague their own sign-in. They only see their interviews.", s.has_interviewer, "Add a person", () => go("team")],
    ["Schedule an interview", "Open a shortlisted candidate and offer some times.", s.has_interview, "Open candidates", () => go("candidates")],
  ];
  const nextIdx = steps.findIndex(x => !x[2]);
  const showGuide = nextIdx !== -1 && store.local("hideGuide") !== "1";

  $("#main").innerHTML = `
    ${head(`${greet}, ${esc(state.user.name.split(" ")[0])}`, "Here is what needs you today. Anything the AI wrote is waiting for your approval in the review queue.",
      `<button class="btn secondary" id="newjob">${icon("plus")} New job</button>`)}
    <div class="grid-today">
      <div class="stack">
        <section class="panel" aria-labelledby="needs-h">
          <div class="panel-head"><h2 id="needs-h">Needs you</h2></div>
          <div class="needs">${needs.map((x, i) => {
            const c = n[x.k] || 0;
            return `<div class="need ${c ? "" : "clear"}"><div class="need-icon">${icon(c ? x.ic : "circle-check")}</div>
              <div class="need-title">${c ? `<span class="need-count">${c}</span>${esc(c === 1 ? x.one : x.many)}` : esc(x.clear)}</div>
              ${c ? `<button class="btn ${x.k === "feedback_missing" ? "ai" : "secondary"} sm" data-need="${i}">${x.k === "feedback_missing" ? icon("sparkles") : ""}${esc(x.btn)}</button>` : ""}</div>`;
          }).join("")}</div>
        </section>
        ${showGuide ? `
        <section class="panel" aria-labelledby="guide-h">
          <div class="panel-head"><h2 id="guide-h">Getting started</h2><span class="muted small">${steps.filter(x => x[2]).length} of ${steps.length} done</span>
            <div class="actions"><button class="btn ghost sm" id="hideguide">Hide</button></div></div>
          <ol class="steps">${steps.map((x, i) => `
            <li class="step ${x[2] ? "done" : ""} ${i === nextIdx ? "next" : ""}">
              <span class="step-num">${x[2] ? icon("check") : i + 1}</span>
              <div><div class="step-title">${x[0]}</div><div class="small muted">${x[1]}</div></div>
              ${!x[2] && i === nextIdx ? `<button class="btn sm" data-step="${i}">${x[3]}</button>` : ""}
            </li>`).join("")}</ol>
        </section>` : ""}
        <section class="panel" aria-labelledby="jobs-h">
          <div class="panel-head"><h2 id="jobs-h">Open jobs</h2><span class="muted small">Candidates at each stage</span></div>
          ${d.jobs.length ? d.jobs.map(j => `
            <div class="job-row">
              <div class="job-row-head"><h3>${esc(j.title)}</h3>${j.department ? `<span class="tag">${esc(j.department)}</span>` : ""}
                ${j.has_description ? "" : `<span class="tag wait">${icon("pencil")}No job description yet</span>`}
                <button class="btn ghost sm" style="margin-left:auto" data-openjob="${j.id}">Open candidates ${icon("chevron-right")}</button></div>
              <div class="track" role="list" aria-label="Candidates by stage">${STAGES.map(([k, l]) => {
                const c = j.stages[k] || 0;
                return `<div class="track-seg s-${k} ${c ? "has" : "zero"}" role="listitem" title="${esc(l)}"><div class="n">${c}</div><div class="l">${esc(STAGE_SHORT[k])}</div></div>`;
              }).join("")}</div>
            </div>`).join("") : `<div class="empty">${icon("briefcase")}<h3>No jobs yet</h3><p>Create your first job to start receiving and screening resumes.</p>
            <button class="btn" id="newjob2">${icon("plus")} Create a job</button></div>`}
        </section>
      </div>
      <aside class="stack">
        <section class="panel" aria-labelledby="up-h">
          <div class="panel-head"><h2 id="up-h">Coming up this week</h2></div>
          ${d.upcoming.length ? `<ul class="upcoming">${d.upcoming.map(u => {
            const t = new Date(u.when);
            return `<li><div class="date-chip"><div class="d">${t.getDate()}</div><div class="m">${t.toLocaleString(undefined, { month: "short" })}</div></div>
              <div><div style="font-weight:600">${esc(u.candidate.name)}</div>
              <div class="small muted">${t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} with ${esc(u.interviewer)}</div></div></li>`;
          }).join("")}</ul>` : `<div class="empty small">${icon("calendar-days")}<p>No interviews in the next 7 days.</p></div>`}
        </section>
        <section class="panel panel-pad stack" style="gap:10px">
          <h3>You stay in charge</h3>
          <p class="small muted">AI assistants only prepare drafts and suggestions, marked in violet. You approve every email and make every hiring decision. Nothing is sent automatically.</p>
          <button class="linkish" id="howto">${icon("circle-help")} How Hiring Desk works</button>
        </section>
      </aside>
    </div>`;

  $("#newjob").onclick = openNewJob;
  $("#newjob2") && ($("#newjob2").onclick = openNewJob);
  $("#howto").onclick = () => go("help");
  $("#hideguide") && ($("#hideguide").onclick = () => { store.local("hideGuide", "1"); render(); });
  $$("[data-step]").forEach(b => (b.onclick = () => steps[+b.dataset.step][4]()));
  $$("[data-openjob]").forEach(b => (b.onclick = () => go("candidates", { jobId: +b.dataset.openjob })));
  $$("[data-need]").forEach(b => (b.onclick = () => {
    const x = needs[+b.dataset.need];
    if (x.run) return x.run();
    act(b, async () => {
      const r = await api("/agents/feedback/chase", { method: "POST", body: {} });
      toast(`${r.summary}. Reminders are in the review queue.`);
      go("review");
    }, { working: "Drafting", slow: true });
  }));
}

/* ---------- Jobs ---------- */
function openNewJob() {
  openModal(`
    <form id="jobform">
      <div class="modal-head">${icon("briefcase")}<h2>Create a job</h2></div>
      <div class="modal-body">
        <div class="form-grid">
          <label class="field">Job title<input name="title" required placeholder="Backend engineer"></label>
          <label class="field">Team or department<input name="department" placeholder="Engineering"></label>
          <label class="field">Location <span class="hint">Shown on your public careers page.</span>
            <input name="location" placeholder="Remote or Bengaluru"></label>
          <label class="field full">Must-have skills and experience
            <span class="hint">The AI Screener scores every resume against exactly this list. Be specific.</span>
            <textarea name="requirements" required placeholder="Python, FastAPI, Docker, PostgreSQL, 3+ years building APIs"></textarea></label>
          <label class="field full">Salary band <span class="hint">Optional. Kept private: only the AI Writer sees it, and only to draft offer letters.</span>
            <input name="salary_band" placeholder="INR 12 to 16 LPA"></label>
        </div>
      </div>
      <div class="modal-foot"><button type="button" class="btn secondary" data-close>Cancel</button><button class="btn">${icon("plus")} Create job</button></div>
    </form>`);
  $("#jobform").onsubmit = async e => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target));
    try {
      const j = await api("/jobs", { method: "POST", body });
      closeModal(); toast(`Job "${j.title}" created. Next, add resumes.`);
      go("candidates", { jobId: j.id });
    } catch (err) { toast(err.message, { error: true }); }
  };
}

async function viewJobs() {
  const jobs = await api("/jobs");
  $("#main").innerHTML = `
    ${head("Jobs", "Each job has must-have skills that the AI Screener scores resumes against.", `<button class="btn" id="newjob">${icon("plus")} New job</button>`)}
    ${jobs.length ? `<div class="stack">${jobs.map(j => `
      <section class="panel">
        <div class="panel-head"><h2>${esc(j.title)}</h2>${j.department ? `<span class="tag">${esc(j.department)}</span>` : ""}
          ${j.published ? `<span class="tag done">${icon("globe")}Live on careers page</span>` : `<span class="tag">Only visible to your team</span>`}
          <div class="actions"><a class="btn ghost sm" href="/careers" target="_blank" rel="noopener">${icon("external-link")}Careers page</a>
            <button class="btn secondary sm" data-cands="${j.id}">${icon("users")} Candidates</button></div></div>
        <div class="panel-pad form-grid">
          <label class="field full">Must-have skills and experience<textarea data-req="${j.id}">${esc(j.requirements)}</textarea></label>
          <label class="field">Location <span class="hint">Shown on the careers page.</span><input data-loc="${j.id}" value="${esc(j.location || "")}"></label>
          <label class="field">Salary band <span class="hint">Private. Used only for offer letters.</span><input data-sal="${j.id}" value="${esc(j.salary_band)}"></label>
          <label class="field">Public hiring page
            <span class="hint">Free ATS: anyone with the link can see this role and apply.</span>
            <span style="display:flex;align-items:center;gap:8px"><input type="checkbox" data-pub="${j.id}" ${j.published ? "checked" : ""} style="width:auto"> Publish this job</span></label>
          <div style="display:flex;align-items:end"><button class="btn secondary sm" data-save="${j.id}">${icon("check")} Save changes</button></div>
          <div class="full">
            <div class="section-title"><h3>Job description</h3>${j.description ? `<span class="tag done">${icon("badge-check")}Approved by you</span>` : `<span class="tag wait">Not written yet</span>`}</div>
            ${j.description ? `<pre class="resume">${esc(j.description)}</pre>` : `<p class="small muted">Let the AI Writer draft one from the title and must-haves. You will review it before it is saved.</p>`}
            <div style="margin-top:10px"><button class="btn ai sm" data-jd="${j.id}">${icon("sparkles")} ${j.description ? "Draft a new version" : "Draft job description"}</button></div>
          </div>
        </div>
      </section>`).join("")}</div>`
    : `<div class="panel empty">${icon("briefcase")}<h3>No jobs yet</h3><p>Create a job to start adding and screening resumes.</p><button class="btn" id="newjob2">${icon("plus")} Create a job</button></div>`}`;
  $("#newjob").onclick = openNewJob;
  $("#newjob2") && ($("#newjob2").onclick = openNewJob);
  $$("[data-cands]").forEach(b => (b.onclick = () => go("candidates", { jobId: +b.dataset.cands })));
  $$("[data-pub]").forEach(cb => (cb.onchange = async () => {
    try {
      await api(`/jobs/${cb.dataset.pub}`, { method: "PATCH", body: { published: cb.checked } });
      toast(cb.checked ? "The job is live on your careers page." : "Removed from the careers page.");
      render();
    } catch (err) { cb.checked = !cb.checked; toast(err.message, { error: true }); }
  }));
  $$("[data-save]").forEach(b => (b.onclick = () => act(b, async () => {
    const id = b.dataset.save;
    await api(`/jobs/${id}`, { method: "PATCH", body: {
      requirements: $(`[data-req="${id}"]`).value,
      location: $(`[data-loc="${id}"]`).value,
      salary_band: $(`[data-sal="${id}"]`).value } });
    toast("Changes saved");
  }, { working: "Saving" })));
  $$("[data-jd]").forEach(b => (b.onclick = () => act(b, async () => {
    await api("/agents/writing/job_description", { method: "POST", body: { job_id: +b.dataset.jd } });
    toast("Draft ready. Review it in the review queue.");
    go("review");
  }, { working: "Drafting", slow: true })));
}

/* ---------- Candidates (pipeline board) ---------- */
async function viewCandidates() {
  const jobs = await api("/jobs");
  if (!jobs.length) {
    $("#main").innerHTML = `${head("Candidates", "")}<div class="panel empty">${icon("users")}<h3>Create a job first</h3>
      <p>Candidates always apply for a job. Create one, then add resumes to it.</p><button class="btn" id="newjob">${icon("plus")} Create a job</button></div>`;
    $("#newjob").onclick = openNewJob;
    return;
  }
  if (!jobs.some(j => j.id === state.jobId)) {
    // Open the job with the most candidates, so the first visit isn't an empty board.
    const total = j => Object.values(j.stages || {}).reduce((a, b) => a + b, 0);
    const busiest = (state.dash?.jobs || []).slice().sort((a, b) => total(b) - total(a))[0];
    state.jobId = busiest && total(busiest) ? busiest.id : jobs[0].id;
  }
  const job = jobs.find(j => j.id === state.jobId);
  const cands = await api(`/jobs/${job.id}/candidates`);
  const newCount = cands.filter(c => c.status === "new").length;
  state.selected = new Set([...state.selected].filter(id => cands.some(c => c.id === id)));

  $("#main").innerHTML = `
    ${head("Candidates", "Each column is a hiring stage. Open a candidate to see the AI's reasons and take the next step.")}
    <div class="toolbar">
      <label class="sr-only" for="jobsel">Job</label>
      <select id="jobsel">${jobs.map(j => `<option value="${j.id}" ${j.id === job.id ? "selected" : ""}>${esc(j.title)}</option>`).join("")}</select>
      <label class="btn secondary" for="files" tabindex="0">${icon("upload")} Add resumes</label>
      <input id="files" type="file" multiple accept=".txt,.md,.pdf" class="sr-only">
      <button class="btn ai" id="screen" ${newCount ? "" : "disabled"} title="${newCount ? "" : "No new resumes to screen"}">${icon("sparkles")} Screen ${newCount ? plural(newCount, "new resume", "new resumes") : "new resumes"}</button>
      <span class="muted small">Tick 2 to 4 candidates to compare them.</span>
    </div>
    ${cands.length ? `<div class="board">${STAGES.map(([k, label, help]) => {
      const list = cands.filter(c => c.status === k);
      return `<section class="col" aria-label="${esc(label)}">
        <div class="col-head"><h3>${esc(label)}</h3><span class="n">${list.length}</span></div>
        <p class="col-help">${esc(help)}</p>
        <div class="col-body">${list.map(card).join("") || ""}</div></section>`;
    }).join("")}</div>`
    : `<div class="panel empty">${icon("upload")}<h3>No resumes for ${esc(job.title)} yet</h3>
       <p>Upload .txt or .pdf resumes. For a demo, use the files in the sample_data folder.</p>
       <label class="btn" for="files">${icon("upload")} Add resumes</label></div>`}
    <div id="selbar"></div>`;

  $("#jobsel").onchange = e => { state.jobId = +e.target.value; state.selected.clear(); render(); };
  $("#files").onchange = async e => {
    const fd = new FormData();
    [...e.target.files].forEach(f => fd.append("files", f));
    try {
      const r = await api(`/jobs/${job.id}/candidates/upload`, { method: "POST", body: fd });
      const skipped = r.skipped.length ? ` Skipped ${r.skipped.length}: ${r.skipped.map(s => `${s.file} (${s.reason})`).join("; ")}.` : "";
      toast(`Added ${plural(r.created.length, "resume", "resumes")}.${skipped} Next, screen them.`, { ms: 7000 });
      render();
    } catch (err) { toast(err.message, { error: true }); }
  };
  $("#screen").onclick = e => screenJob(job.id, e.currentTarget);
  $$(".card").forEach(el => (el.onclick = e => {
    if (e.target.closest("input")) return;
    openCandidate(+el.dataset.id);
  }));
  $$(".card input[type=checkbox]").forEach(cb => (cb.onchange = () => {
    const id = +cb.dataset.pick;
    cb.checked ? state.selected.add(id) : state.selected.delete(id);
    cb.closest(".card").classList.toggle("selected", cb.checked);
    renderSelectionBar();
  }));
  renderSelectionBar();
}

/* Screening runs as a background job (a batch of 40 free-model calls would hold a
   request open for many minutes), so the button polls the job's progress instead. */
async function screenJob(jobId, btn) {
  await act(btn, async () => {
    let job = await api("/agent-jobs", { method: "POST",
      body: { agent: "screening", action: "screen", params: { job_id: jobId } } });
    const deadline = Date.now() + 20 * 60 * 1000;
    while (!["done", "error", "cancelled"].includes(job.status)) {
      if (Date.now() > deadline) throw new Error("Screening is taking unusually long. Check AI activity, or try again.");
      await new Promise(r => setTimeout(r, 1500));
      job = await api(`/agent-jobs/${job.id}`);
      if (btn.isConnected && job.progress_total) {
        btn.innerHTML = `${icon("refresh-cw", "spin")} Screened ${job.progress_current} of ${job.progress_total}`;
        paintIcons();
      }
    }
    if (job.status === "error") throw new Error(job.error || "The screening job failed");
    if (job.status === "cancelled") { toast("Screening stopped. Resumes that were already scored are kept."); return; }
    const r = job.result || {};
    const failed = (r.failed || []).length;
    const ok = (r.processed || 0) - failed;
    toast(failed
      ? `Screened ${ok} of ${r.processed}. ${plural(failed, "resume", "resumes")} could not be read; try again later.`
      : `Screened ${plural(ok, "resume", "resumes")}. Open a candidate to see why.`, { ms: 7000 });
  }, { working: "Starting", slow: true });
  render();
}

function card(c) {
  const sel = state.selected.has(c.id);
  return `<div class="card ${sel ? "selected" : ""}" data-id="${c.id}" role="button" tabindex="0" aria-label="Open ${esc(c.name)}">
    <div class="card-top">
      <input type="checkbox" data-pick="${c.id}" ${sel ? "checked" : ""} aria-label="Select ${esc(c.name)} to compare">
      <span class="card-name" title="${esc(c.name)}">${esc(c.name)}</span>
      <span class="score ${c.score == null ? "none" : ""}" title="${c.score == null ? "Not screened yet" : "AI Screener score out of 100"}">${c.score ?? "–"}</span>
    </div>
    ${c.source === "careers" ? `<div class="small muted">${icon("globe")} Applied on the careers page</div>` : ""}
    ${c.screening?.summary ? `<div class="card-sum">${esc(c.screening.summary)}</div>` : ""}
  </div>`;
}

function renderSelectionBar() {
  const n = state.selected.size;
  const bar = $("#selbar");
  if (!bar) return;
  bar.innerHTML = n ? `<div class="selection-bar"><span>${plural(n, "candidate", "candidates")} selected</span>
    <button class="btn ai sm" id="cmp" ${n >= 2 && n <= 4 ? "" : "disabled"}>${icon("scale")} Compare</button>
    <button class="btn ghost sm" id="clr" style="color:inherit">Clear</button>
    ${n > 4 ? `<span class="small">Pick at most 4</span>` : n < 2 ? `<span class="small">Pick at least 2</span>` : ""}</div>` : "";
  paintIcons();
  if (!n) return;
  $("#clr").onclick = () => { state.selected.clear(); render(); };
  $("#cmp").onclick = e => act(e.currentTarget, async () => showComparison(
    await api("/agents/comparison/compare", { method: "POST", body: { candidate_ids: [...state.selected] } })), { working: "Comparing", slow: true });
}

function showComparison(r) {
  const cs = r.candidates;
  const cell = items => items.length ? `<ul class="list">${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>` : `<span class="muted small">Nothing noted</span>`;
  openModal(`
    <div class="modal-head">${icon("scale")}<h2>Comparison for ${esc(r.job.title)}</h2></div>
    <div class="modal-body">
      <div class="ai-note">${icon("sparkles")}<div><b>Suggested by the AI Comparer.</b> Ordered by screening score. Use it to prepare, not to decide.<div style="margin-top:6px">${esc(r.overview)}</div></div></div>
      <div style="overflow-x:auto"><table class="compare-table">
        <tr><th></th>${cs.map(c => `<th>${esc(c.name)}</th>`).join("")}</tr>
        <tr><td>Score</td>${cs.map(c => `<td><span class="score ${c.score == null ? "none" : ""}">${c.score ?? "–"}</span></td>`).join("")}</tr>
        <tr><td>Stage</td>${cs.map(c => `<td>${esc(STAGE_LABEL[c.status] || c.status)}</td>`).join("")}</tr>
        <tr><td>Strengths</td>${cs.map(c => `<td>${cell(c.strengths)}</td>`).join("")}</tr>
        <tr><td>Risks</td>${cs.map(c => `<td>${cell(c.risks)}</td>`).join("")}</tr>
      </table></div>
      ${r.open_questions.length ? `<div><h3>Questions to settle in interviews</h3>${cell(r.open_questions)}</div>` : ""}
    </div>
    <div class="modal-foot"><button class="btn" data-close>Done</button></div>`);
}

/* ---------- candidate drawer ---------- */
async function openCandidate(id) {
  const [c, interviews, users] = await Promise.all([api(`/candidates/${id}`), api("/interviews"), api("/users")]);
  const mine = interviews.filter(i => i.candidate.id === id);
  const people = users.filter(u => u.is_active);
  const s = c.status;
  const next = {
    new: [["screen", "ai", "sparkles", "Screen new resumes for this job"]],
    screened: [["shortlisted", "", "user-check", "Shortlist"], ["rejected", "danger", "user-x", "Not moving forward"]],
    shortlisted: [["schedule", "", "calendar-plus", "Schedule an interview"], ["rejected", "danger", "user-x", "Not moving forward"]],
    interviewing: [
      ["synthesize", "ai", "scale", "Synthesize decision"],
      ["summarize", "ai", "sparkles", "Summarise feedback"],
      ["offer", "ai", "file-signature", "Draft offer letter"],
      ["schedule", "secondary", "calendar-plus", "Add another interview"],
      ["rejected", "danger", "user-x", "Not moving forward"],
    ],
    offered: [["offer", "ai", "file-signature", "Draft offer letter again"], ["rejected", "danger", "user-x", "Not moving forward"]],
    rejected: [["rejection", "ai", "mail", "Draft rejection email"], ["screened", "secondary", "refresh-cw", "Move back to Screened"]],
  }[s] || [];

  $("#layer").innerHTML = `
    <div class="drawer-backdrop" data-closedrawer></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="dr-h">
      <div class="drawer-head">
        <div class="avatar" style="width:42px;height:42px;font-size:15px">${esc(initials(c.name))}</div>
        <div style="flex:1;min-width:0"><h2 id="dr-h">${esc(c.name)}</h2>
          <div class="small muted">${esc(c.email || "No email found in resume")}</div>
          <div style="margin-top:6px"><span class="tag">${esc(STAGE_LABEL[s] || s)}</span>
            ${c.source === "careers" ? `<span class="tag">${icon("globe")}Careers page</span>` : ""}</div></div>
        <button class="btn ghost sm" data-closedrawer aria-label="Close">${icon("x")}</button>
      </div>
      <div class="drawer-body">
        <section><div class="section-title"><h3>Next step</h3></div>
          <div class="next-steps">${next.map(([a, cls, ic, label]) => `<button class="btn ${cls} sm" data-do="${a}">${icon(ic)} ${label}</button>`).join("")}</div>
          <form id="schedform" class="panel panel-pad hidden" style="margin-top:12px;display:grid;gap:12px">
            <label class="field">Interviewer<select name="iv">${people.map(u => `<option value="${u.id}">${esc(u.name)}${u.role === "hr_admin" ? " (HR)" : ""}</option>`).join("")}</select></label>
            <div class="field">Times to offer the candidate <span class="hint">Offer 1 to 3. The candidate picks one; you confirm it on the Interviews page.</span>
              <input type="datetime-local" name="s1" required><input type="datetime-local" name="s2"><input type="datetime-local" name="s3"></div>
            <div><button class="btn ai sm">${icon("sparkles")} Propose times and draft invite</button></div>
          </form>
        </section>

        <section><div class="section-title"><h3>AI screening</h3>${c.score != null ? `<span class="score">${c.score}</span>` : ""}</div>
          ${c.screening ? `<div class="ai-note">${icon("sparkles")}<div><b>Suggested by the AI Screener.</b> Check it against the resume before you decide.
            <div style="margin-top:6px">${esc(c.screening.summary)}</div></div></div>
            <div class="form-grid" style="margin-top:12px">
              <div><h4 class="small" style="color:var(--done)">Matches</h4>${c.screening.strengths?.length ? `<ul class="list">${c.screening.strengths.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="small muted">None noted</p>`}</div>
              <div><h4 class="small" style="color:var(--wait)">Gaps</h4>${c.screening.gaps?.length ? `<ul class="list">${c.screening.gaps.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="small muted">None noted</p>`}</div>
            </div>` : `<p class="small muted">Not screened yet. Use "Screen new resumes" on the Candidates page.</p>`}
        </section>

        ${c.feedback_summary ? `<section><div class="section-title"><h3>Interview feedback summary</h3></div>
          <div class="ai-note">${icon("sparkles")}<div><b>Summarised by AI Follow-up.</b><div style="margin-top:6px;white-space:pre-wrap">${esc(c.feedback_summary)}</div></div></div></section>` : ""}

        <section><div class="section-title"><h3>Interviews</h3></div>
          ${mine.length ? `<div class="stack" style="gap:8px">${mine.map(i => `<div class="info-note" style="display:grid;gap:6px">
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
              <div><b>${esc(i.interviewer.name)}</b>, ${i.scheduled_at ? esc(fmtWhen(i.scheduled_at)) : "time not confirmed yet"}</div>
              <button class="btn ai sm" data-drkit="${i.id}">${icon("clipboard-check")} Interview kit</button>
            </div>
            <div>${i.feedback ? `Rated ${i.feedback.rating} out of 5: ${esc(i.feedback.notes)}` : i.scheduled_at && isPast(i.scheduled_at) ? `<span style="color:var(--wait)">Feedback missing</span>` : "No feedback yet"}</div></div>`).join("")}</div>`
          : `<p class="small muted">No interviews yet.</p>`}
        </section>

        <section><details><summary>${icon("chevron-right")} Resume</summary><pre class="resume" style="margin-top:10px">${esc(c.resume_text)}</pre></details></section>

        <section><label class="field">Change stage manually <span class="hint">Use this to correct a mistake.</span>
          <select id="stagesel">${STAGES.map(([k, l]) => `<option value="${k}" ${k === s ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label></section>
      </div>
    </aside>`;
  paintIcons();
  $$("[data-closedrawer]").forEach(b => (b.onclick = closeDrawer));
  document.addEventListener("keydown", escClose);
  $(".drawer [data-closedrawer].btn").focus();

  const setStage = async (status, msg) => { await api(`/candidates/${id}/status`, { method: "POST", body: { status } }); toast(msg); await refreshAfterDrawer(id); };
  $("#stagesel").onchange = e => setStage(e.target.value, `Moved to ${STAGE_LABEL[e.target.value]}`).catch(err => toast(err.message, { error: true }));
  $$("[data-do]").forEach(b => (b.onclick = () => {
    const a = b.dataset.do;
    if (a === "schedule") { $("#schedform").classList.toggle("hidden"); return; }
    if (a === "shortlisted" || a === "screened") return act(b, () => setStage(a, `${c.name} moved to ${STAGE_LABEL[a]}`));
    if (a === "rejected") return act(b, () => setStage("rejected", `${c.name} marked as not moving forward. You can now draft a kind rejection email.`));
    if (a === "screen") return screenJob(c.job_id, b).then(() => refreshAfterDrawer(id));
    if (a === "summarize") return act(b, async () => { await api("/agents/feedback/summarize", { method: "POST", body: { candidate_id: id } }); toast("Feedback summarised"); await refreshAfterDrawer(id); }, { working: "Summarising", slow: true });
    if (a === "synthesize") return act(b, async () => {
      const r = await api("/agents/feedback/synthesize", { method: "POST", body: { candidate_id: id } });
      toast(`${r.summary}. The decision memo is in the review queue.`);
      closeDrawer();
      go("review");
    }, { working: "Synthesizing", slow: true });
    const writer = { offer: "offer", rejection: "rejection" }[a];
    if (writer) return act(b, async () => {
      await api(`/agents/writing/${writer}`, { method: "POST", body: { candidate_id: id } });
      if (writer === "offer" && s !== "offered") await api(`/candidates/${id}/status`, { method: "POST", body: { status: "offered" } });
      toast(`${writer === "offer" ? "Offer letter" : "Rejection email"} drafted. Review it in the review queue before sending.`, { ms: 6000 });
      await refreshAfterDrawer(id);
    }, { working: "Drafting", slow: true });
  }));
  $$("[data-drkit]").forEach(b => (b.onclick = () => act(b, async () => {
    const r = await api("/agents/interview_kit/prepare", { method: "POST", body: { interview_id: +b.dataset.drkit } });
    toast(`${r.summary}. The kit is ready in the review queue.`);
    closeDrawer();
    go("review");
  }, { working: "Preparing", slow: true })));
  $("#schedform").onsubmit = e => {
    e.preventDefault();
    const f = new FormData(e.target);
    const slots = ["s1", "s2", "s3"].map(k => f.get(k)).filter(Boolean);
    act($("button", e.target), async () => {
      await api("/agents/scheduling/propose", { method: "POST", body: { candidate_id: id, interviewer_id: +f.get("iv"), slots } });
      toast("Interview proposed. The invite is in the review queue; confirm the time on the Interviews page once the candidate replies.", { ms: 8000 });
      await refreshAfterDrawer(id);
    }, { working: "Drafting invite", slow: true });
  };
}
async function refreshAfterDrawer(id) { await render(); await openCandidate(id); }
function escClose(e) { if (e.key === "Escape") closeDrawer(); }
function closeDrawer() { const l = $("#layer"); if (l) l.innerHTML = ""; document.removeEventListener("keydown", escClose); }

/* ---------- Interviews ---------- */
async function viewInterviews() {
  const rows = await api("/interviews");
  const statusOf = i => i.feedback ? ["done", "Feedback in", "circle-check"]
    : i.status === "proposed" ? ["wait", "Needs a confirmed time", "hourglass"]
    : isPast(i.scheduled_at) ? ["wait", "Feedback missing", "bell-ring"] : ["", "Scheduled", "calendar-check"];
  const missing = rows.filter(i => !i.feedback && i.status === "scheduled" && isPast(i.scheduled_at)).length;
  $("#main").innerHTML = `
    ${head("Interviews", "Confirm the time a candidate picked, and make sure every interviewer sends feedback.",
      `<button class="btn ai" id="chase" ${missing ? "" : "disabled"}>${icon("sparkles")} Draft feedback reminders${missing ? ` (${missing})` : ""}</button>`)}
    <section class="panel">${rows.length ? `<div style="overflow-x:auto"><table>
      <tr><th>Candidate</th><th>Job</th><th>Interviewer</th><th>When</th><th>Status</th><th></th></tr>
      ${rows.map(i => { const [cls, label, ic] = statusOf(i); return `<tr>
        <td><button class="linkish" style="font-weight:600;color:var(--ink);font-size:14px" data-cand="${i.candidate.id}">${esc(i.candidate.name)}</button></td>
        <td>${esc(i.job.title)}</td><td>${esc(i.interviewer.name)}</td>
        <td>${i.scheduled_at ? esc(fmtWhen(i.scheduled_at)) : `<form class="toolbar" style="margin:0" data-confirm="${i.id}">
          <label class="sr-only" for="slot-${i.id}">Time the candidate picked</label>
          <select id="slot-${i.id}" name="slot" style="min-width:0">${i.proposed_slots.map(sl => `<option value="${esc(sl)}">${esc(fmtWhen(sl))}</option>`).join("")}</select>
          <button class="btn sm">${icon("check")} Confirm</button></form>`}</td>
        <td><span class="tag ${cls}">${icon(ic)}${label}</span>${i.feedback ? `<div class="small muted" style="margin-top:4px">Rated ${i.feedback.rating} out of 5</div>` : ""}</td>
        <td><button class="btn ai sm" data-kit="${i.id}" title="Prepare tailored interview questions and rubric">${icon("clipboard-check")} Interview kit</button></td></tr>`; }).join("")}
      </table></div>` : `<div class="empty">${icon("calendar-days")}<h3>No interviews yet</h3><p>Open a shortlisted candidate and choose "Schedule an interview".</p>
        <button class="btn secondary" id="tocands">${icon("users")} Go to candidates</button></div>`}</section>`;
  $("#tocands") && ($("#tocands").onclick = () => go("candidates"));
  $$("[data-cand]").forEach(b => (b.onclick = () => openCandidate(+b.dataset.cand)));
  $$("[data-kit]").forEach(b => (b.onclick = () => act(b, async () => {
    const r = await api("/agents/interview_kit/prepare", { method: "POST", body: { interview_id: +b.dataset.kit } });
    toast(`${r.summary}. The kit is ready in the review queue.`);
    go("review");
  }, { working: "Preparing", slow: true })));
  $("#chase").onclick = e => act(e.currentTarget, async () => {
    const r = await api("/agents/feedback/chase", { method: "POST", body: {} });
    toast(`${r.summary}. Reminders are in the review queue.`); go("review");
  }, { working: "Drafting", slow: true });
  $$("[data-confirm]").forEach(f => (f.onsubmit = e => {
    e.preventDefault();
    act($("button", f), async () => {
      await api(`/interviews/${f.dataset.confirm}/confirm`, { method: "POST", body: { slot: new FormData(f).get("slot") } });
      toast("Time confirmed. The interviewer now sees it in their interviews."); render();
    }, { working: "Confirming" });
  }));
}

/* ---------- Review queue ---------- */
async function viewReview() {
  const drafts = await api("/drafts?status=pending");
  if (!drafts.some(d => d.id === state.reviewId)) state.reviewId = drafts[0]?.id ?? null;
  const d = drafts.find(x => x.id === state.reviewId);
  $("#main").innerHTML = `
    ${head("Review queue", "Everything the AI assistants wrote waits here. Nothing is sent automatically: edit it, approve it, then copy it into your email.")}
    ${drafts.length ? `<section class="panel review">
      <div class="review-list" role="list">${drafts.map(x => `
        <button class="review-item" role="listitem" data-pick="${x.id}" ${x.id === state.reviewId ? 'aria-current="true"' : ""}>
          <span class="tag ai" style="justify-self:start">${icon(assistant(x.agent).icon)}${esc(DRAFT_KIND[x.kind] || x.kind)}</span>
          <span class="t">${esc(x.subject)}</span><span class="small muted">${x.recipient ? "To " + esc(x.recipient) : "Internal"}</span></button>`).join("")}</div>
      <form class="review-edit" id="draftform">
        <div class="ai-note">${icon("sparkles")}<div><b>Written by the ${esc(assistant(d.agent).label)}.</b> Read it carefully and change anything that is wrong.
          ${d.kind === "job_description" ? " Approving saves it as the job description." : " Approving marks it ready; you send it yourself."}</div></div>
        ${d.recipient ? `<div class="small muted">To: <b style="color:var(--ink)">${esc(d.recipient)}</b></div>` : ""}
        <label class="field">Subject<input name="subject" value="${esc(d.subject)}"></label>
        <label class="field">Message<textarea name="content">${esc(d.content)}</textarea></label>
        <div class="toolbar" style="margin:0">
          <button class="btn" data-a="approve">${icon("check")} Approve</button>
          <button class="btn secondary" type="button" data-a="copy">${icon("copy")} Copy text</button>
          <button class="btn danger" type="button" data-a="discard" style="margin-left:auto">${icon("trash-2")} Discard</button>
        </div>
      </form></section>`
    : `<section class="panel empty">${icon("inbox")}<h3>Nothing to review</h3><p>When an AI assistant drafts an email or a job description, it waits here for your approval.</p></section>`}`;
  if (!d) return;
  $$("[data-pick]").forEach(b => (b.onclick = () => { state.reviewId = +b.dataset.pick; viewReview().then(paintIcons); }));
  const form = $("#draftform");
  const vals = () => ({ subject: form.subject.value, content: form.content.value });
  form.onsubmit = e => {
    e.preventDefault();
    act($('[data-a="approve"]', form), async () => {
      await api(`/drafts/${d.id}/approve`, { method: "POST", body: vals() });
      toast(d.kind === "job_description" ? "Approved and saved as the job description." : "Approved. Copy it into your email to send it.");
      state.reviewId = null; render();
    }, { working: "Approving" });
  };
  $('[data-a="copy"]', form).onclick = () => navigator.clipboard.writeText(`${vals().subject}\n\n${vals().content}`)
    .then(() => toast("Copied. Paste it into your email."), () => toast("Copy failed. Select the text and copy it manually.", { error: true }));
  $('[data-a="discard"]', form).onclick = e => act(e.currentTarget, async () => {
    await api(`/drafts/${d.id}/discard`, { method: "POST", body: {} });
    toast("Draft discarded"); state.reviewId = null; render();
  }, { working: "Discarding" });
}

/* ---------- AI activity ---------- */
async function viewActivity() {
  const [runs, agents] = await Promise.all([api("/agent-runs"), api("/agents")]);
  const result = r => r.status === "success" ? `<span class="tag done">${icon("circle-check")}Done</span>`
    : r.status === "denied" ? `<span class="tag bad">${icon("lock")}Blocked</span>` : `<span class="tag bad">${icon("circle-alert")}Failed</span>`;
  $("#main").innerHTML = `
    ${head("AI activity", "What each assistant is allowed to touch, and a log of everything they did. Blocked means an assistant tried something it is not allowed to do.")}
    <div class="stack">
      <section class="panel"><div class="panel-head"><h2>Assistants and their permissions</h2></div>
        <div style="overflow-x:auto"><table><tr><th>Assistant</th><th>What it does</th><th>Allowed to</th></tr>
        ${agents.map(a => `<tr><td style="white-space:nowrap"><span class="tag ai">${icon(assistant(a.name).icon)}${esc(assistant(a.name).label)}</span></td>
          <td>${esc(assistant(a.name).does || a.description)}</td>
          <td><div style="display:flex;flex-wrap:wrap;gap:4px">${a.scopes.map(s => `<span class="tag" title="${esc(s.scope)}">${esc(SCOPE_PLAIN[s.scope] || s.scope)}</span>`).join("")}</div></td></tr>`).join("")}
        </table></div></section>
      <section class="panel"><div class="panel-head"><h2>Activity log</h2><span class="muted small">Latest first</span></div>
        ${runs.length ? `<div style="overflow-x:auto"><table><tr><th>When</th><th>Assistant</th><th>Run by</th><th>Result</th><th>Data it used</th></tr>
        ${runs.map(r => `<tr><td class="small muted" style="white-space:nowrap">${esc(fmtWhen(r.created_at + "Z"))}</td>
          <td style="white-space:nowrap">${esc(assistant(r.agent).label)}<div class="small muted">${esc(r.action.replace(/_/g, " "))}</div></td>
          <td class="small">${esc(r.user)}</td>
          <td>${result(r)}<div class="small muted" style="margin-top:4px">${esc(r.summary || r.error)}</div></td>
          <td><div style="display:flex;flex-wrap:wrap;gap:4px">${r.scopes_used.map(s => `<span class="tag">${esc(SCOPE_PLAIN[s] || s)}</span>`).join("")}</div></td></tr>`).join("")}
        </table></div>` : `<div class="empty">${icon("activity")}<p>No AI activity yet.</p></div>`}</section>
    </div>`;
}

/* ---------- Team ---------- */
function openMfaSetup() {
  act($("#mfa-toggle"), async () => {
    const setup = await api("/auth/mfa/setup", { method: "POST" });
    openModal(`
      <form id="mfa-enable">
        <div class="modal-head">${icon("shield-check")}<h2>Turn on two-factor</h2></div>
        <div class="modal-body stack" style="gap:12px">
          <p class="small muted">Add this key to an authenticator app (Google Authenticator, 1Password, Authy), then type the 6-digit code it shows.</p>
          <label class="field">Setup key<input readonly value="${esc(setup.secret)}" style="font-family:monospace"></label>
          <label class="field">Code<input name="code" inputmode="numeric" autocomplete="one-time-code" required maxlength="6" placeholder="123456" autofocus></label>
        </div>
        <div class="modal-foot"><button type="button" class="btn secondary" data-close>Cancel</button><button class="btn">${icon("shield-check")} Turn on</button></div>
      </form>`);
    $("#mfa-enable").onsubmit = e => {
      e.preventDefault();
      act($("button[type=submit]", e.target), async () => {
        const r = await api("/auth/mfa/enable", { method: "POST",
          body: { code: new FormData(e.target).get("code").trim() } });
        closeModal();
        state.user.mfa_enabled = true;
        render();
        openModal(`
          <div class="modal-head">${icon("shield-check")}<h2>Two-factor is on</h2></div>
          <div class="modal-body stack" style="gap:12px">
            <p>Save these recovery codes somewhere safe. Each one works once if you lose your phone.</p>
            <pre class="resume">${esc(r.recovery_codes.join("\n"))}</pre>
          </div>
          <div class="modal-foot"><button class="btn" data-close>I saved them</button></div>`);
      }, { working: "Turning on" });
    };
  }, { working: "Preparing" });
}

function openMfaDisable() {
  openModal(`
    <form id="mfa-disable">
      <div class="modal-head">${icon("shield-off")}<h2>Turn off two-factor</h2></div>
      <div class="modal-body stack" style="gap:12px">
        <label class="field">Your password<input name="password" type="password" autocomplete="current-password" required></label>
        <label class="field">Current code<input name="code" inputmode="numeric" autocomplete="one-time-code" required maxlength="9" placeholder="123456"></label>
      </div>
      <div class="modal-foot"><button type="button" class="btn secondary" data-close>Cancel</button><button class="btn danger">Turn off</button></div>
    </form>`);
  $("#mfa-disable").onsubmit = e => {
    e.preventDefault();
    const f = new FormData(e.target);
    act($("button[type=submit]", e.target), async () => {
      const r = await api("/auth/mfa/disable", { method: "POST",
        body: { password: f.get("password"), code: f.get("code").trim() } });
      state.token = r.access_token; state.user = r.user; store.set("token", r.access_token);
      closeModal(); render(); toast("Two-factor is off.");
    }, { working: "Turning off" });
  };
}

async function viewTeam() {
  const users = await api("/users");
  $("#main").innerHTML = `
    ${head("Team", "Interviewers get their own sign-in. They only see the candidates they interview, never AI scores, so their judgement stays independent.")}
    <div class="stack">
      <section class="panel"><div style="overflow-x:auto"><table><tr><th>Name</th><th>Email</th><th>Role</th><th></th></tr>
        ${users.map(u => `<tr><td><div class="who"><div class="avatar">${esc(initials(u.name))}</div><b>${esc(u.name)}</b></div></td>
          <td>${esc(u.email)}</td><td><span class="tag">${u.role === "hr_admin" ? "HR admin" : "Interviewer"}</span>
            ${u.mfa_enabled ? `<span class="tag done">${icon("shield-check")}2FA</span>` : ""}</td>
          <td style="text-align:right;white-space:nowrap">${!u.is_active ? `<span class="muted small">Deactivated</span>` : `
            <button class="btn secondary sm" data-reset="${u.id}">${icon("key-round")} Reset password</button>
            ${u.id === state.user.id ? `<span class="muted small">You</span>` : `<button class="btn danger sm" data-off="${u.id}">${icon("user-x")} Deactivate</button>`}`}</td></tr>`).join("")}
      </table></div></section>
      <section class="panel"><div class="panel-head"><h2>Add a person</h2></div>
        <form id="userform" class="panel-pad form-grid">
          <label class="field">Full name<input name="name" required></label>
          <label class="field">Work email<input name="email" type="email" required></label>
          <label class="field">Temporary password <span class="hint">At least 8 characters. Share it with them privately.</span><input name="password" minlength="8" required></label>
          <label class="field">Role<select name="role"><option value="interviewer">Interviewer</option><option value="hr_admin">HR admin</option></select></label>
          <div class="full"><button class="btn">${icon("plus")} Add person</button></div>
        </form></section>
      <section class="panel"><div class="panel-head"><h2>Your account security</h2></div>
        <div class="panel-pad stack" style="gap:12px">
          <div class="info-note">${icon("shield-check")}<div><b>Two-factor authentication.</b>
            ${state.user.mfa_enabled ? "On. You are asked for a code each time you sign in." : "Off. Add a second step to protect HR data and candidate details."}</div></div>
          <div class="actions">
            <button class="btn ${state.user.mfa_enabled ? "secondary" : ""}" id="mfa-toggle">${icon("shield-check")} ${state.user.mfa_enabled ? "Turn off two-factor" : "Turn on two-factor"}</button>
            <button class="btn ghost" id="signout-all">${icon("log-out")} Sign out everywhere</button>
          </div>
        </div></section>
    </div>`;
  $("#userform").onsubmit = e => {
    e.preventDefault();
    act($("button", e.target), async () => {
      const u = await api("/users", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
      toast(`${u.name} added. Share the temporary password with them privately.`); render();
    }, { working: "Adding" });
  };
  $$("[data-off]").forEach(b => (b.onclick = () => act(b, async () => {
    await api(`/users/${b.dataset.off}/deactivate`, { method: "POST" }); toast("Deactivated. They can no longer sign in."); render();
  }, { working: "Deactivating" })));
  $$("[data-reset]").forEach(b => (b.onclick = () => act(b, async () => {
    const r = await api(`/users/${b.dataset.reset}/password-reset`, { method: "POST" });
    openModal(`
      <div class="modal-head">${icon("key-round")}<h2>One-time reset link</h2></div>
      <div class="modal-body stack" style="gap:12px">
        <p class="small muted">Send this link to <b>${esc(r.user.name)}</b> through a channel you trust. It expires ${esc(fmtWhen(r.expires_at))} and works once.</p>
        <label class="field">Link<input readonly value="${esc(r.reset_url)}" autofocus></label>
      </div>
      <div class="modal-foot"><button class="btn" data-close>Done</button></div>`);
  }, { working: "Creating" })));
  $("#mfa-toggle").onclick = () => state.user.mfa_enabled ? openMfaDisable() : openMfaSetup();
  $("#signout-all").onclick = () => signOutEverywhere();
}

/* ---------- How it works ---------- */
async function viewHelp() {
  const hr = state.user.role === "hr_admin";
  $("#main").innerHTML = `
    ${head("How Hiring Desk works", hr ? "One place to run hiring from job post to offer, with AI assistants doing the busywork." : "What you can do here as an interviewer.")}
    <div class="stack">
      ${hr ? `<section class="panel"><div class="panel-head"><h2>A candidate's journey</h2></div>
        <ol class="steps">${STAGES.map(([k, l, help], i) => `<li class="step"><span class="step-num">${i + 1}</span><div><div class="step-title">${esc(l)}</div><div class="small muted">${esc(help)}</div></div></li>`).join("")}</ol></section>
      <section class="panel"><div class="panel-head"><h2>Your AI assistants</h2></div>
        <table>${Object.values(ASSISTANTS).map(a => `<tr><td style="width:170px"><span class="tag ai">${icon(a.icon)}${a.label}</span></td><td>${a.does}</td></tr>`).join("")}</table></section>` : ""}
      <section class="panel panel-pad stack" style="gap:12px"><h2>The rules</h2>
        <div class="info-note">${icon("shield-check")}<div><b>You decide.</b> Assistants only suggest and draft. Shortlisting, rejecting and offers are always your call.</div></div>
        <div class="info-note">${icon("inbox")}<div><b>Nothing is sent automatically.</b> Every email waits in the review queue until you approve it, and you send it yourself.</div></div>
        <div class="info-note">${icon("lock")}<div><b>Each assistant sees only what it needs.</b> For example, the AI Screener can never see salary bands.</div></div>
        <div class="info-note">${icon("eye")}<div><b>Everything is logged.</b> AI activity shows every assistant action, including anything that was blocked.</div></div>
        <div class="info-note">${icon("sparkles")}<div><b>Violet means AI.</b> Anything marked in violet was written or suggested by an assistant. Check it before you rely on it.</div></div>
      </section>
    </div>`;
}

/* ---------- interviewer: my interviews ---------- */
async function viewMine() {
  const rows = await api("/my/interviews");
  $("#main").innerHTML = `
    ${head("My interviews", "Read the resume before the interview, then send your feedback. You won't see the AI's score, so your judgement stays your own.")}
    ${rows.length ? `<div class="stack">${rows.map(i => `
      <section class="panel">
        <div class="panel-head"><div class="avatar">${esc(initials(i.candidate.name))}</div><div><h2>${esc(i.candidate.name)}</h2><div class="small muted">${esc(i.job.title)}</div></div>
          <div class="actions">${i.scheduled_at ? `<span class="tag">${icon("calendar-check")}${esc(fmtWhen(i.scheduled_at))}</span>` : `<span class="tag wait">${icon("hourglass")}Time not confirmed yet</span>`}</div></div>
        <div class="panel-pad stack">
          <details data-res="${i.candidate.id}"><summary>${icon("chevron-right")} Resume</summary><pre class="resume" style="margin-top:10px">Loading...</pre></details>
          ${i.feedback ? `<div class="info-note">${icon("circle-check")}<div><b>Your feedback is in.</b> Rated ${i.feedback.rating} out of 5. ${esc(i.feedback.notes)}</div></div>`
          : i.scheduled_at ? `<form data-fb="${i.id}" class="form-grid">
              <label class="field">Overall rating<select name="rating">${[[5, "5, strong yes"], [4, "4, yes"], [3, "3, unsure"], [2, "2, no"], [1, "1, strong no"]].map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>
              <label class="field full">What you saw <span class="hint">Specific examples help HR decide.</span><textarea name="notes" required></textarea></label>
              <div class="full"><button class="btn">${icon("send")} Send feedback</button></div></form>`
          : `<p class="small muted">You can send feedback once HR confirms the time.</p>`}
        </div></section>`).join("")}</div>`
    : `<section class="panel empty">${icon("calendar-days")}<h3>No interviews yet</h3><p>When HR schedules you for an interview, it appears here.</p></section>`}`;
  $$("[data-res]").forEach(d => d.addEventListener("toggle", async () => {
    if (!d.open || d._loaded) return;
    d._loaded = true;
    try { $("pre", d).textContent = (await api(`/candidates/${d.dataset.res}`)).resume_text; } catch (e) { $("pre", d).textContent = e.message; }
  }));
  $$("[data-fb]").forEach(f => (f.onsubmit = e => {
    e.preventDefault();
    const v = new FormData(f);
    act($("button", f), async () => {
      await api(`/interviews/${f.dataset.fb}/feedback`, { method: "POST", body: { rating: +v.get("rating"), notes: v.get("notes") } });
      toast("Feedback sent. Thank you."); render();
    }, { working: "Sending" });
  }));
}

/* ---------- modal ---------- */
function openModal(html) {
  const m = $("#modal");
  $("#modal-body").innerHTML = html;
  paintIcons();
  $$("[data-close]", m).forEach(b => (b.onclick = closeModal));
  if (!m.open) m.showModal();
}
function closeModal() { const m = $("#modal"); if (m.open) m.close(); }
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });

document.addEventListener("keydown", e => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList?.contains("card")) { e.preventDefault(); openCandidate(+e.target.dataset.id); }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.("label.btn[for]")) { e.preventDefault(); $("#" + e.target.htmlFor).click(); }
});

render();

/* ---------- Copilot chat ----------
   A right-side panel on every HR page. A message starts a background job; we poll it and
   show each step as it finishes. Changes come back as an action card that waits for
   Approve. Lives outside #app so moving between pages keeps the chat open. */
const cp = { convId: null, polling: null, convs: [] };
const CP_EXAMPLES = [
  "Analyse the top 5 candidates for each open job",
  "Screen the new resumes and tell me who stands out",
  "Which interviews are missing feedback?",
  "What is waiting for me in the review queue?",
];

async function copilotOpen() {
  const panel = $("#copilot");
  panel.hidden = false;
  if (!panel.innerHTML) copilotShell();
  try {
    await copilotLoadList();
    if (!cp.convId && cp.convs.length) cp.convId = cp.convs[0].id;
    await copilotShow();
  } catch (e) { toast(e.message, { error: true }); }
  $("#cp-text").focus();
}

function copilotClose() {
  $("#copilot").hidden = true;
  clearTimeout(cp.polling);
  cp.polling = null;
}

function copilotShell() {
  $("#copilot").innerHTML = `
    <div class="cp-head">${icon("message-square-text")}<h2>Copilot</h2>
      <label class="sr-only" for="cp-convs">Past chats</label><select id="cp-convs"></select>
      <button class="btn ghost sm" id="cp-new" title="New chat" aria-label="New chat">${icon("plus")}</button>
      <button class="btn ghost sm" id="cp-close" aria-label="Close Copilot">${icon("x")}</button></div>
    <div class="cp-body" id="cp-body" aria-live="polite"></div>
    <div class="cp-foot">
      <form id="cp-form"><label class="sr-only" for="cp-text">Ask Copilot</label>
        <textarea id="cp-text" maxlength="2000" placeholder="Ask about candidates, jobs or interviews"></textarea>
        <button class="btn ai" id="cp-send">${icon("send")} Ask</button></form>
      <p class="hint">Copilot hands each step to the right assistant. Any change waits for your approval.</p>
    </div>`;
  paintIcons();
  $("#cp-close").onclick = copilotClose;
  $("#cp-new").onclick = () => { cp.convId = null; copilotShow(); $("#cp-text").focus(); };
  $("#cp-convs").onchange = e => { cp.convId = +e.target.value || null; copilotShow(); };
  $("#cp-form").onsubmit = e => { e.preventDefault(); copilotSend($("#cp-text").value); };
  $("#cp-text").onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); copilotSend(e.target.value); } };
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#copilot").hidden && !($("#layer") && $("#layer").innerHTML)) copilotClose();
  });
}

async function copilotLoadList() {
  cp.convs = await api("/copilot/conversations");
  $("#cp-convs").innerHTML = `<option value="">New chat</option>` + cp.convs.map(c =>
    `<option value="${c.id}" ${c.id === cp.convId ? "selected" : ""}>${esc(c.title)}</option>`).join("");
}

async function copilotShow() {
  clearTimeout(cp.polling);
  const body = $("#cp-body");
  if (!cp.convId) {
    $("#cp-convs").value = "";
    body.innerHTML = `<div class="ai-note">${icon("sparkles")}<div><b>Ask in plain language.</b> Copilot finds what it needs,
      hands each step to the right assistant, and shows you every step. Changes such as rejecting someone are prepared
      for your approval, never made on their own.</div></div>
      <div class="cp-examples">${CP_EXAMPLES.map(t => `<button data-example="${esc(t)}">${esc(t)}</button>`).join("")}</div>`;
    paintIcons();
    $$("[data-example]", body).forEach(b => (b.onclick = () => copilotSend(b.dataset.example)));
    return;
  }
  const conv = await api(`/copilot/conversations/${cp.convId}`);
  $("#cp-convs").value = String(cp.convId);
  body.innerHTML = conv.messages.map(copilotMessage).join("");
  paintIcons();
  copilotWire(body);
  body.scrollTop = body.scrollHeight;
  const last = conv.messages[conv.messages.length - 1];
  if (last && last.role === "user" && last.job_id) copilotPoll(last.job_id);
}

function copilotLinked(text, steps) {
  // Turn candidate names the steps touched into links that open the candidate.
  let html = esc(text);
  const seen = new Map();
  (steps || []).forEach(s => (s.candidates || []).forEach(c => seen.set(c.name, c.id)));
  [...seen.entries()].sort((a, b) => b[0].length - a[0].length).forEach(([name, id]) => {
    const safe = esc(name);
    if (safe.length > 1) html = html.split(safe).join(`<button class="linkish" data-cand="${id}">${safe}</button>`);
  });
  return html;
}

function copilotStep(s) {
  const a = assistant(s.agent);
  const g = s.grant;
  return `<div class="cp-step ${s.ok ? "" : "failed"}">${icon(s.ok ? "circle-check" : "circle-alert")}
    <div><div>${esc(s.summary)}</div>
      <div class="cp-step-meta"><span>${esc(a.label)}</span>${s.duration_ms ? `<span>${(s.duration_ms / 1000).toFixed(1)} s</span>` : ""}
      ${g ? `<span title="Single-use token ${esc(String(g.jti).slice(0, 8))}: ${esc(g.actors.join(" acting for "))}">${icon("key-round")} own token:</span>
        ${g.scopes.map(sc => `<span class="tag">${esc(SCOPE_PLAIN[sc] || sc)}</span>`).join("")}` : ""}</div></div></div>`;
}

const CP_STATUS = { pending: "Not applied yet", approved: "Applying", done: "Applied", failed: "Partly applied", dismissed: "Dismissed" };

function copilotMessage(m) {
  if (m.role === "user") return `<div class="cp-msg user">${esc(m.content)}</div>`;
  const steps = m.steps || [];
  const p = m.proposal;
  const tone = p ? ({ pending: "wait", done: "done", failed: "bad" }[p.status] || "") : "";
  const card = p ? `<div class="cp-card"><div class="row"><b>Prepared for your approval</b>
      <span class="tag ${tone}">${esc(CP_STATUS[p.status] || p.status)}</span></div>
      <ul>${p.items.map(i => `<li>${esc(i.label)}</li>`).join("")}</ul>
      ${p.status === "pending" ? `<div class="row"><button class="btn sm" data-approve="${p.id}">${icon("check")} Approve all</button>
        <button class="btn secondary sm" data-dismiss="${p.id}">Dismiss</button></div>` : ""}</div>` : "";
  return `<div class="cp-msg assistant"><div class="cp-answer">${copilotLinked(m.content, steps)}</div>
    ${steps.length ? `<details class="cp-steps"><summary>${icon("chevron-right")} How Copilot did it (${steps.length} step${steps.length === 1 ? "" : "s"})</summary>
      ${steps.map(copilotStep).join("")}</details>` : ""}${card}</div>`;
}

function copilotWire(root) {
  $$("[data-cand]", root).forEach(b => (b.onclick = () => openCandidate(+b.dataset.cand)));
  $$("[data-approve]", root).forEach(b => (b.onclick = () => act(b, async () => {
    const r = await api(`/copilot/proposals/${b.dataset.approve}/approve`, { method: "POST" });
    toast("Approved. Applying the changes now.");
    await copilotShow();
    copilotPoll(r.job_id, true);
  }, { working: "Approving" })));
  $$("[data-dismiss]", root).forEach(b => (b.onclick = () => act(b, async () => {
    await api(`/copilot/proposals/${b.dataset.dismiss}/dismiss`, { method: "POST" });
    toast("Dismissed. Nothing was changed.");
    await copilotShow();
  }, { working: "Dismissing" })));
}

async function copilotSend(text) {
  text = (text || "").trim();
  if (!text) return;
  const btn = $("#cp-send");
  btn.disabled = true;
  try {
    if (!cp.convId) cp.convId = (await api("/copilot/conversations", { method: "POST", body: {} })).id;
    const r = await api(`/copilot/conversations/${cp.convId}/messages`, { method: "POST", body: { text } });
    $("#cp-text").value = "";
    await copilotLoadList();
    await copilotShow();
    copilotPoll(r.job_id);
  } catch (e) { toast(e.message, { error: true, ms: 7000 }); }
  finally { btn.disabled = false; }
}

function copilotPoll(jobId, refreshAfter = false) {
  clearTimeout(cp.polling);
  const convAtStart = cp.convId;
  const body = $("#cp-body");
  if (!$("#cp-live")) body.insertAdjacentHTML("beforeend", `<div class="cp-msg assistant" id="cp-live"></div>`);
  const tick = async () => {
    if ($("#copilot").hidden || cp.convId !== convAtStart) return;
    let job = null;
    try { job = await api(`/agent-jobs/${jobId}`); } catch { job = null; }
    if (job && (job.status === "queued" || job.status === "running")) {
      const live = $("#cp-live");
      if (live) {
        live.innerHTML = `<div class="cp-working">${icon("refresh-cw", "spin")} ${job.status === "queued" ? "Starting" : "Working on it"}.
          Free AI models can take a minute per step.</div>${(job.steps || []).map(copilotStep).join("")}`;
        paintIcons();
        body.scrollTop = body.scrollHeight;
      }
      cp.polling = setTimeout(tick, 2000);
      return;
    }
    if (job && job.status === "error") toast(`Copilot stopped: ${job.error}`, { error: true, ms: 8000 });
    await copilotShow();
    if (refreshAfter && $("#main")) render();   // approved changes may move candidates on the page behind
  };
  tick();
}
