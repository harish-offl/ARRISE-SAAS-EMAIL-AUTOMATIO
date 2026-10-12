(() => {
  const STORE = "ea_auth_v2";
  const nav = [
    ["home", "Home", "grid", "Workspace overview"],
    ["leads", "Leads", "users", "Lead operations"],
    ["campaigns", "Campaigns", "send", "Send control"],
    ["tools", "Email Tools", "search", "Finder and verifier"],
    ["writer", "AI Writer", "edit", "Outreach copy"],
    ["settings", "Settings", "settings", "Sender setup"]
  ];
  const S = {
    view: "home",
    authMode: "login",
    token: null,
    refreshToken: null,
    user: null,
    local: false,
    stats: null,
    leads: [],
    campaigns: [],
    campaign: null,
    report: null,
    followups: null,
    sender: null,
    deliverability: null,
    dailyActivity: [],
    activityDays: 7,
    filter: "all",
    search: "",
    poll: null,
    replyPoll: null,
    replySyncBusy: false
  };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
  const val = (id) => ($(id) && $(id).value || "").trim();
  const checked = (id) => !!($(id) && $(id).checked);
  const fmtPct = (value) => `${Number(value || 0).toFixed(Number(value || 0) % 1 ? 1 : 0)}%`;
  const fmtDate = (value) => {
    if (!value) return "Not contacted";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  };
  const iconPath = {
    grid: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
    users: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M23 21v-2a4 4 0 0 0-3-3.87",
    send: "M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z",
    search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35",
    edit: "M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z",
    settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm0-13v3m0 14v3M4.22 4.22l2.12 2.12m11.32 11.32 2.12 2.12M2 12h3m14 0h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12",
    refresh: "M23 4v6h-6M20.49 15a9 9 0 1 1-2.12-9.36L23 10",
    menu: "M3 6h18M3 12h18M3 18h18"
  };
  const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${iconPath[name] || iconPath.grid}"/></svg>`;

  async function api(url, options = {}, retry = true) {
    const headers = { ...(options.headers || {}) };
    if (S.token) headers.Authorization = `Bearer ${S.token}`;
    let body = options.body;
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const response = await fetch(url, { ...options, headers, body, cache: "no-store" });
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (response.status === 401 && retry && S.refreshToken) {
      const ok = await refreshToken();
      if (ok) return api(url, options, false);
    }
    if (!response.ok) throw new Error((data && (data.error || data.detail)) || `Request failed (${response.status})`);
    return data;
  }
  async function refreshToken() {
    try {
      const data = await api("/api/auth/refresh", { method: "POST", body: { refresh_token: S.refreshToken } }, false);
      setAuth(data);
      return true;
    } catch (err) {
      clearAuth();
      return false;
    }
  }
  function setAuth(data) {
    S.token = data.access_token;
    S.refreshToken = data.refresh_token;
    S.user = data.user || S.user || null;
    localStorage.setItem(STORE, JSON.stringify({ token: S.token, refreshToken: S.refreshToken, user: S.user }));
  }
  function clearAuth() {
    localStorage.removeItem(STORE);
    S.token = null;
    S.refreshToken = null;
    S.user = null;
    S.local = false;
    stopPoll();
  }
  function notice(text, type = "info") {
    const node = $("notice");
    node.hidden = !text;
    node.className = `notice ${type}`;
    node.textContent = text || "";
  }
  function campaignStatus() {
    return (S.campaign && S.campaign.status) || "idle";
  }
  function getStatus(lead) {
    if (lead.archived_at) return "archived";
    const status = String(lead.last_status || "").toLowerCase();
    if (status === "failed" || status === "skipped") return status;
    if (status === "bounced" || status === "auto_replied" || status === "not_interested" || status === "unsubscribed") return status;
    if (status === "replied" || lead.reply_received_at) return lead.reply_type || "replied";
    if (status === "sent" || lead.sent_at) return "sent";
    return "pending";
  }
  function syncStatsPoll() {
    stopPoll();
    S.poll = setInterval(() => refreshLiveMonitor(), 2000);
    S.replyPoll = setInterval(() => silentReplySync(), 120000);
  }
  async function refreshLiveMonitor() {
    try {
      await Promise.all([loadDashboardStats(false), loadCampaign(false), loadCampaigns(false)]);
      if (S.view === "home" || S.view === "campaigns") renderPage();
    } catch (err) {
      console.warn("Live monitor refresh failed", err);
    }
  }
  function stopPoll() {
    if (S.poll) clearInterval(S.poll);
    if (S.replyPoll) clearInterval(S.replyPoll);
    S.poll = null;
    S.replyPoll = null;
  }

  function showAuth() {
    stopPoll();
    $("marketingSite").hidden = true;
    $("authScreen").hidden = false;
    $("appShell").hidden = true;
    renderAuthMode();
  }
  function showMarketing() {
    stopPoll();
    $("marketingSite").hidden = false;
    $("authScreen").hidden = true;
    $("appShell").hidden = true;
    $("copyrightYear").textContent = new Date().getFullYear();
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  function showApp() {
    $("marketingSite").hidden = true;
    $("authScreen").hidden = true;
    $("appShell").hidden = false;
    renderShell();
    loadCore(true);
    syncStatsPoll();
  }
  function renderAuthMode() {
    document.querySelectorAll("[data-auth-mode]").forEach((button) => button.classList.toggle("active", button.dataset.authMode === S.authMode));
    $("nameField").hidden = S.authMode !== "signup";
    $("authTitle").textContent = S.authMode === "signup" ? "Create your account" : "Welcome back";
    $("authSub").textContent = S.authMode === "signup" ? "Start your Arrise workspace" : "Sign in to continue";
    $("authSubmit").textContent = S.authMode === "signup" ? "Create account" : "Login";
    $("authError").textContent = "";
  }
  function renderShell() {
    $("nav").innerHTML = nav.map(([id, label, ic]) => `<button class="nav-item ${S.view === id ? "active" : ""}" data-nav="${id}" type="button">${icon(ic)}<span>${label}</span></button>`).join("");
    $("refreshBtn").innerHTML = `${icon("refresh")} Refresh`;
    $("menuBtn").innerHTML = icon("menu");
    $("themeToggle").innerHTML = `${icon("settings")} ${document.documentElement.dataset.theme === "dark" ? "Light" : "Dark"}`;
    const user = S.user || { email: "local workspace", full_name: "Local user", workspaces: [] };
    const name = user.full_name || user.email || "Local user";
    $("userName").textContent = name;
    $("userEmail").textContent = user.email || "local workspace";
    $("userAvatar").textContent = (name[0] || "U").toUpperCase();
    $("workspaceName").textContent = (user.workspaces && user.workspaces[0] && user.workspaces[0].name) || "Arrise Digital";
    renderPage();
  }
  function renderShellState() {
    document.querySelectorAll("[data-nav]").forEach((button) => button.classList.toggle("active", button.dataset.nav === S.view));
    const active = nav.find((item) => item[0] === S.view) || nav[0];
    $("pageTitle").textContent = active[1] || "Home";
    if ($("pageSubtitle")) $("pageSubtitle").textContent = active[3] || "Workspace overview";
    $("topStatus").textContent = campaignStatus();
    $("topStatus").className = `status ${campaignStatus()}`;
  }
  function renderPage() {
    const page = { home: renderHome, leads: renderLeads, campaigns: renderCampaigns, tools: renderTools, writer: renderWriter, settings: renderSettings }[S.view] || renderHome;
    page($("content"));
    bindCurrentPage();
    renderShellState();
  }

  async function loadCore(showNotice) {
    if (showNotice) notice("Refreshing workspace data...");
    try {
      await Promise.all([loadDashboardStats(false), loadLeads(false), loadCampaign(false), loadCampaigns(false), loadReport(false), loadDailyActivity(false), loadFollowups(false), loadSender(false), loadDeliverability(false)]);
      if (showNotice) notice("Workspace data is current.", "success");
    } catch (err) {
      notice(err.message, "error");
    } finally {
      renderPage();
    }
  }
  async function loadDashboardStats(render = true) {
    try {
      S.stats = await api("/api/dashboard/stats/");
      if (render && S.view === "home") renderPage();
    } catch (err) {
      if (render) notice(err.message, "error");
    }
  }
  async function loadLeads(render = true) {
    const data = await api(`/api/leads/${S.filter === "archived" ? "?include_archived=true" : ""}`);
    S.leads = Array.isArray(data) ? data : [];
    if (render) renderPage();
  }
  async function loadCampaigns(render = true) {
    const data = await api("/api/campaigns/");
    S.campaigns = Array.isArray(data) ? data : [];
    if (render && S.view === "campaigns") renderPage();
  }
  async function loadCampaign(render = true) {
    S.campaign = await api("/api/campaign/status/");
    if (render) renderPage();
  }
  async function loadReport(render = true) {
    S.report = await api(`/api/reports/overview/?days=${S.activityDays}`);
    if (render) renderPage();
  }
  async function loadDailyActivity(render = true) {
    S.dailyActivity = await api(`/api/dashboard/daily/?days=${S.activityDays}`);
    if (render) renderPage();
  }
  async function loadFollowups(render = true) {
    S.followups = await api("/api/followups/settings/");
    if (render) renderPage();
  }
  async function loadSender(render = true) {
    S.sender = await api("/api/sender/profile/");
    if (render) renderPage();
  }
  async function loadDeliverability(render = true) {
    S.deliverability = await api("/api/dashboard/deliverability/");
    if (render && S.view === "settings") renderPage();
  }
  function stat(key, fallback = 0) {
    return S.stats && S.stats[key] !== undefined ? S.stats[key] : fallback;
  }
  function metric(label, value, tone = "") {
    return `<div class="metric ${tone}"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
  }
  function warmupTone(status) {
    if (status === "stable") return "sent";
    if (status === "ready") return "good";
    if (status === "warming") return "warn";
    return "idle";
  }
  function warmupStatusLabel(status) {
    if (status === "stable") return "Warm-up complete, volume stabilized";
    if (status === "ready") return "Warm-up reaching full volume";
    if (status === "warming") return "Warm-up in progress, ramping volume";
    return "Warm-up not started";
  }
  function check(label, ok, detail) {
    return `<div class="check ${ok ? "" : "warn"}"><b>${ok ? "OK" : "!"}</b><div><strong>${esc(label)}</strong><div class="muted small">${esc(detail)}</div></div></div>`;
  }
  function progressBlock(campaign = {}) {
    const total = Number(campaign.total || 0);
    const processed = Number(campaign.processed || 0);
    const sent = Number(campaign.sent || 0);
    const skipped = Number(campaign.skipped || 0);
    const failed = Number(campaign.failed || 0);
    const remaining = Number(campaign.remaining ?? Math.max(0, total - processed));
    const pct = total ? Math.min(100, Math.round(processed / total * 100)) : 0;
    const statusLine = total
      ? `${pct}% complete. ${sent} message${sent === 1 ? "" : "s"} accepted by the sender, ${remaining} remaining.`
      : (campaign.message || "No campaign has been started yet.");
    const elapsed = campaign.elapsed_seconds
      ? `${campaign.elapsed_seconds}s`
      : (campaign.status === "running" ? "Running" : "Not started");
    return `<div class="progress"><i style="width:${pct}%"></i></div><p class="muted small">${esc(statusLine)}</p><div class="grid-4">${metric("Accepted by sender", sent, "good")}${metric("Processed", `${processed}/${total}`, "primary")}${metric("Failed", failed, "bad")}${metric("Remaining", remaining, "warn")}</div><div class="grid-4" style="margin-top:10px">${metric("Skipped", skipped)}${metric("Started", campaign.started_at ? fmtDate(campaign.started_at) : "Not started")}${metric("Finished", campaign.finished_at ? fmtDate(campaign.finished_at) : "Not yet")}${metric("Elapsed", elapsed)}</div>`;
  }
  function renderActivityChart() {
    const rows = Array.isArray(S.dailyActivity) ? S.dailyActivity : [];
    if (!rows.length) return `<div class="empty activity-empty">Sending activity will appear here after your first campaign.</div>`;
    const maximum = Math.max(1, ...rows.map((row) => Number(row.sent || 0) + Number(row.failed || 0)));
    const labelInterval = Math.max(1, Math.ceil(rows.length / 7));
    return rows.map((row, index) => {
      const sent = Number(row.sent || 0);
      const failed = Number(row.failed || 0);
      const date = new Date(`${row.day}T12:00:00`);
      const label = Number.isNaN(date.getTime()) ? row.day : new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
      const bar = (count, type) => count ? `<i class="activity-bar ${type}" style="height:${Math.max(4, count / maximum * 88)}px" title="${count} ${type === "failed" ? "failed" : "accepted"}"></i>` : "";
      return `<div class="activity-day" title="${esc(label)}: ${sent} accepted, ${failed} failed"><div class="activity-bars">${bar(sent, "sent")}${bar(failed, "failed")}</div><time>${index % labelInterval === 0 || index === rows.length - 1 ? esc(label) : ""}</time></div>`;
    }).join("");
  }
  function renderRecentActivity() {
    const activity = S.report && Array.isArray(S.report.recent_activity) ? S.report.recent_activity : [];
    if (!activity.length) return `<div class="empty">Campaign and reply activity will appear here when events are recorded.</div>`;
    return `<div class="result-list">${activity.slice(0, 6).map((item) => `<div class="result"><div><strong>${esc(item.lead_name || item.type || "Activity")}</strong><span>${esc(item.summary || item.type || "")} · ${esc(item.email || "")}</span></div><span>${esc(fmtDate(item.occurred_at))}</span></div>`).join("")}</div>`;
  }
  function renderHome(node) {
    const stats = S.stats || {};
    const report = S.report || {};
    const totals = report.totals || {};
    const recent = report.recent || {};
    const replyRate = fmtPct(stats.reply_rate || 0);
    const replies = Number(stats.replied_leads ?? totals.replied_leads ?? 0);
    const health = (report.sender_health && report.sender_health.health_score) ?? stats.sender_health_score ?? "—";
    const periodLabel = S.activityDays === 1 ? "today" : `last ${S.activityDays} days`;
    const activeCampaigns = campaignStatus() === "running" ? 1 : 0;
    $("previewLeads").textContent = stat("total_leads", 0);
    $("previewReply").textContent = replyRate;
    $("previewHealth").textContent = health;
    node.innerHTML = `
      <div class="page-head"><div><h2>Workspace overview</h2><p>Campaign and contact performance from your recorded workspace activity.</p></div><div class="split-actions"><button class="btn btn-primary" data-nav="leads">${icon("users")} Import contacts</button><button class="btn" data-nav="campaigns">${icon("send")} Campaigns</button></div></div>
      <div class="grid-4">${metric("Total contacts", stat("total_leads", 0))}${metric("Eligible contacts", stat("pending_leads", 0))}${metric("Active campaigns", activeCampaigns)}${metric(`Emails accepted · ${periodLabel}`, recent.sent_events || 0, "primary")}</div>
      <div class="grid-4">${metric(`Replies received · ${periodLabel}`, recent.recent_replies || 0)}${metric(`Failed sends · ${periodLabel}`, recent.failed_events || 0, "bad")}${metric("Unsubscribed contacts", stats.opt_outs || 0)}${metric("Sender health", health, "primary")}</div>
      <div class="grid-2">
        <section class="section-card"><div class="section-title"><div><h3>Campaign pulse</h3><p>Current runner state and queue progress</p></div><span class="status ${campaignStatus()}">${esc(campaignStatus())}</span></div>${progressBlock(S.campaign || {})}<div class="check-row" style="margin-top:16px">${check("Contact list", Number(stat("total_leads", 0)) > 0, `${stat("total_leads", 0)} records stored`)}${check("Outreach queue", Number(stat("pending_leads", 0)) > 0 || Number(S.followups && S.followups.due_count || 0) > 0, `${stat("pending_leads", 0)} new, ${Number(S.followups && S.followups.due_count || 0)} due follow-ups`)}${check("Sender setup", !!(S.sender && S.sender.sender_name), S.sender && S.sender.sender_name ? S.sender.sender_name : "Add sender details in Settings")}</div></section>
        <section class="section-card"><div class="section-title"><div><h3>Sending activity</h3><p>Provider-accepted and failed messages · ${periodLabel}</p></div><label class="small">Period <select id="activityRange" aria-label="Activity date range"><option value="1" ${S.activityDays === 1 ? "selected" : ""}>Today</option><option value="7" ${S.activityDays === 7 ? "selected" : ""}>Last 7 days</option><option value="30" ${S.activityDays === 30 ? "selected" : ""}>Last 30 days</option><option value="90" ${S.activityDays === 90 ? "selected" : ""}>Last 90 days</option></select></label></div><div class="activity-chart" style="--bar-count:${Math.max(1, S.dailyActivity.length)}" role="img" aria-label="Daily provider-accepted and failed sending activity">${renderActivityChart()}</div><div class="section-title" style="margin-top:18px"><div><h3>Recent activity</h3><p>Recorded sends, replies, and opens</p></div><button class="btn btn-sm" data-action="syncReplies">Sync replies</button></div>${renderRecentActivity()}</section>
      </div>`;
  }

  function renderLeads(node) {
    const rows = filteredLeads();
    node.innerHTML = `
      <div class="page-head"><div><h2>Leads</h2><p>Import, review, filter, and inspect lead records.</p></div><button class="btn" data-action="refreshLeads">${icon("refresh")} Refresh leads</button></div>
      <div class="grid-2">
        <section class="section-card"><div class="section-title"><div><h3>Upload list</h3><p>CSV or Excel import with backend normalization</p></div></div><label class="drop"><strong id="fileLabel">Drop or choose lead file</strong><span>CSV, XLSX, or XLS</span><input id="leadFile" type="file" accept=".csv,.xlsx,.xls,text/csv"></label><div class="grid-2" style="margin-top:12px"><div class="switch-row"><div><strong>Replace existing</strong><span>Clear old rows before import</span></div><label class="switch"><input id="replaceExisting" type="checkbox" checked><i></i></label></div><div class="switch-row"><div><strong>Require solution</strong><span>Skip incomplete rows</span></div><label class="switch"><input id="requireSolution" type="checkbox" checked><i></i></label></div></div><div class="split-actions" style="margin-top:12px"><button class="btn btn-primary" data-action="uploadLeads">Upload leads</button><button class="btn" data-action="uploadLaunch">Upload then launch</button></div><p id="uploadResult" class="muted small"></p></section>
        <section class="section-card"><div class="section-title"><div><h3>Add one lead</h3><p>Create a single record quickly</p></div></div><form id="addLeadForm"><div class="inline-fields"><div class="field"><label>Name</label><input id="leadName" placeholder="Prospect name"></div><div class="field"><label>Email</label><input id="leadEmail" type="email" placeholder="name@company.com" required></div><div class="field"><label>Company</label><input id="leadCompany" placeholder="Company"></div><div class="field"><label>Industry</label><input id="leadIndustry" placeholder="Industry"></div></div><div class="field"><label>Solution or niche</label><input id="leadNiche" placeholder="What they may need"></div><button class="btn btn-primary" type="submit">Add lead</button></form></section>
      </div>
      <section class="section-card"><div class="section-title"><div><h3>Lead workspace</h3><p id="leadCount">${rows.length} visible of ${S.leads.length} ${S.filter === "archived" ? "archived" : "active"} contacts</p></div><div class="filters">${["all","pending","sent","replied","not_interested","unsubscribed","failed","skipped","archived"].map((filter) => `<button class="filter ${S.filter === filter ? "active" : ""}" data-filter="${filter}">${filter}</button>`).join("")}</div></div><div class="toolbar"><div class="field"><input id="leadSearch" type="search" placeholder="Search name, email, company, industry" value="${esc(S.search)}"></div><button class="btn btn-danger" data-action="resetFailed">Reset failed</button></div><div id="leadResults">${leadTable(rows)}</div></section>`;
  }
  function filteredLeads() {
    return S.leads.filter((lead) => {
      const status = getStatus(lead);
      if (S.filter !== "all" && status !== S.filter) return false;
      if (!S.search) return true;
      return [lead.name, lead.email, lead.company, lead.industry, lead.niche, lead.phone].join(" ").toLowerCase().includes(S.search.toLowerCase());
    });
  }
  function leadTable(rows) {
    if (!rows.length) return `<div class="empty">No matching leads yet.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Company</th><th>Solution</th><th>Status</th><th>Last contact</th><th></th></tr></thead><tbody>${rows.map((lead) => `<tr><td><strong>${esc(lead.name || "Unnamed")}</strong><span>${esc(lead.industry || "-")}</span></td><td><strong>${esc(lead.email || "-")}</strong><span>${esc(lead.phone || "-")}</span></td><td><strong>${esc(lead.company || "-")}</strong><span>${esc(lead.industry || "-")}</span></td><td><strong>${esc(lead.niche || "-")}</strong><span>${esc(lead.sequence_status || "pending")}</span></td><td><span class="status ${getStatus(lead)}">${getStatus(lead)}</span></td><td><strong>${fmtDate(lead.last_contacted_at || lead.sent_at)}</strong><span>${esc(lead.last_error || lead.reply_summary || "")}</span></td><td><button class="btn btn-sm" data-lead="${lead.id}">Open</button></td></tr>`).join("")}</tbody></table></div>`;
  }

  function renderCampaigns(node) {
    const followups = S.followups || {};
    node.innerHTML = `
      <div class="page-head"><div><h2>Campaigns</h2><p>Build audience-filtered drafts and launch them after sender readiness checks.</p></div><button class="btn" data-action="startCampaign" ${campaignStatus() === "running" ? "disabled" : ""}>${icon("send")} ${campaignStatus() === "running" ? "Campaign running" : "Start all eligible"}</button></div>
      <section class="section-card"><div class="section-title"><div><h3>New campaign draft</h3><p>Optional industry matching uses a case-insensitive partial match against contact records.</p></div></div><form id="campaignForm" class="inline-fields"><div class="field"><label for="campaignName">Campaign name</label><input id="campaignName" maxlength="200" placeholder="e.g. October partner outreach" required></div><div class="field"><label for="campaignIndustry">Industry filter</label><input id="campaignIndustry" maxlength="100" placeholder="Leave blank for all eligible contacts"></div><button class="btn btn-primary" type="submit">Save draft</button></form></section>
      <section class="section-card"><div class="section-title"><div><h3>Saved campaigns</h3><p>${S.campaigns.length} draft${S.campaigns.length === 1 ? "" : "s"} in this workspace</p></div><button class="btn btn-sm" data-action="refreshCampaigns">${icon("refresh")} Refresh</button></div>${campaignTable()}</section>
      <div class="grid-2"><section class="section-card"><div class="section-title"><div><h3>Live monitor</h3><p>Background runner status</p></div><span class="status ${campaignStatus()}">${esc(campaignStatus())}</span></div>${progressBlock(S.campaign || {})}<div class="split-actions" style="margin-top:14px"><button class="btn" data-action="refreshCampaign">${icon("refresh")} Check status</button><button class="btn" data-action="syncReplies">Sync replies</button></div></section><section class="section-card"><div class="section-title"><div><h3>Launch checklist</h3><p>Ready items before sending</p></div></div><div class="check-row">${check("Leads available", Number(stat("total_leads", 0)) > 0, `${stat("total_leads", 0)} active records`)}${check("Pending or due", Number(stat("pending_leads", 0)) > 0 || Number(followups.due_count || 0) > 0, `${stat("pending_leads", 0)} pending, ${followups.due_count || 0} follow-ups due`)}${check("Runner idle", campaignStatus() !== "running", campaignStatus() === "running" ? "Wait for current run" : "Ready to launch")}</div></section></div>
      <section class="section-card"><div class="section-title"><div><h3>Follow-up rules</h3><p>Fixed dates and delays stay server-side</p></div><span class="status ${followups.enabled ? "sent" : "idle"}">${followups.enabled ? "enabled" : "off"}</span></div><div class="grid-4">${metric("Due now", followups.due_count || 0, "warn")}${metric("Scheduled", followups.scheduled_count || 0, "primary")}${metric("Active", followups.active_count || 0, "good")}${metric("Next due", followups.next_due_at ? fmtDate(followups.next_due_at) : "None")}</div><form id="followupForm" class="inline-fields" style="margin-top:14px"><div class="switch-row"><div><strong>Enable follow-ups</strong><span>Include due follow-ups in campaign launch</span></div><label class="switch"><input id="fuEnabled" type="checkbox" ${followups.enabled ? "checked" : ""}><i></i></label></div><div class="field"><label>First delay days</label><input id="fu1" type="number" min="0" max="90" value="${followups.followup_1_delay_days ?? 3}"></div><div class="field"><label>Second delay days</label><input id="fu2" type="number" min="0" max="90" value="${followups.followup_2_delay_days ?? 7}"></div><div class="field"><label>Final bump days</label><input id="fu3" type="number" min="0" max="90" value="${followups.final_bump_delay_days ?? 12}"></div><button class="btn btn-primary" type="submit">Save follow-ups</button></form></section>`;
  }
  function campaignTable() {
    if (!S.campaigns.length) return `<div class="empty">No saved campaigns yet. Create a draft above to keep campaign work organized.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Campaign</th><th>Audience</th><th>Status</th><th>Created</th><th>Results</th><th></th></tr></thead><tbody>${S.campaigns.map((campaign) => {
      let filters = {};
      try { filters = JSON.parse(campaign.audience_filter || "{}"); } catch (err) {}
      const audience = filters.industry ? `Industry contains “${esc(filters.industry)}”` : "All eligible contacts";
      return `<tr><td><strong>${esc(campaign.name)}</strong><span>Saved campaign</span></td><td>${audience}</td><td><span class="status ${esc(campaign.status || "draft")}">${esc(campaign.status || "draft")}</span></td><td>${esc(fmtDate(campaign.created_at))}</td><td>${campaign.total_leads || 0} contacts · ${campaign.sent_count || 0} accepted · ${campaign.failed_count || 0} failed</td><td><button class="btn btn-sm btn-primary" data-launch-campaign="${campaign.id}" ${campaign.status !== "draft" || campaignStatus() === "running" ? "disabled" : ""}>Launch</button></td></tr>`;
    }).join("")}</tbody></table></div>`;
  }
  function renderTools(node) {
    node.innerHTML = `
      <div class="page-head"><div><h2>Email Tools</h2><p>Find addresses, verify risk, and process prospect lists.</p></div></div>
      <div class="grid-3"><section class="section-card"><div class="section-title"><div><h3>Email finder</h3><p>Find a professional email from name + company/domain</p></div></div><form id="finderForm"><div class="field"><label>Person's name</label><input id="finderName" placeholder="John Doe"></div><div class="field"><label>Company or website</label><input id="finderDomain" placeholder="Acme Inc / acme.com (not an email address)"></div><button class="btn btn-primary" type="submit">Find email</button></form><div id="finderResults" class="result-list" style="margin-top:12px"></div></section>
      <section class="section-card"><div class="section-title"><div><h3>Email verifier</h3><p>Format, MX, provider, and risk</p></div></div><form id="verifierForm"><div class="field"><label>Email</label><input id="verifyEmail" type="email" placeholder="name@company.com"></div><button class="btn btn-primary" type="submit">Verify</button></form><div id="verifyResults" class="result-list" style="margin-top:12px"></div></section>
      <section class="section-card"><div class="section-title"><div><h3>Bulk finder</h3><p>One prospect per line: name, domain</p></div></div><form id="bulkFinderForm"><div class="field"><label>Prospects</label><textarea id="bulkProspects" placeholder="Jane Doe, example.com"></textarea></div><button class="btn btn-primary" type="submit">Run bulk find</button></form><div id="bulkResults" class="result-list" style="margin-top:12px"></div></section></div>`;
  }

  function renderWriter(node) {
    node.innerHTML = `
      <div class="page-head"><div><h2>AI Writer</h2><p>Generate outreach copy using the backend email engine.</p></div><button class="btn" data-action="copyEmail">Copy email</button></div>
      <div class="grid-2"><section class="section-card"><div class="section-title"><div><h3>Lead context</h3><p>Keep inputs specific and concise</p></div></div><form id="aiForm"><div class="inline-fields"><div class="field"><label>Name</label><input id="aiName" value="Prospect"></div><div class="field"><label>Company</label><input id="aiCompany" value="Acme Corp"></div><div class="field"><label>Industry</label><input id="aiIndustry" value="technology"></div><div class="field"><label>Solution</label><input id="aiNiche" value="lead generation"></div><div class="field"><label>Goal</label><select id="aiGoal"><option value="cold">Cold outreach</option><option value="followup">Follow-up</option></select></div><div class="field"><label>Tone</label><select id="aiTone"><option value="professional">Professional</option><option value="friendly">Friendly</option><option value="formal">Formal</option></select></div></div><button class="btn btn-primary" type="submit">Generate</button></form></section><section class="section-card"><div class="section-title"><div><h3>Email draft</h3><p>Review before sending</p></div><span class="status idle" id="writerStatus">draft</span></div><textarea id="aiOutput" style="width:100%;min-height:360px;border:1px solid var(--line);border-radius:var(--radius);padding:14px;background:var(--surface);resize:vertical">Your generated email will appear here.</textarea></section></div>`;
  }

  function renderSettings(node) {
    const profile = S.sender || {};
    const deliverability = S.deliverability || {};
    const inboxTone = deliverability.status === "ready" ? "good" : (deliverability.status === "blocked" ? "bad" : "warn");
    node.innerHTML = `
      <div class="page-head"><div><h2>Settings</h2><p>Sender identity, tracking, warmup, spam guard, and account details.</p></div><button class="btn" data-action="loadMe">Refresh account</button></div>
      <div class="grid-2"><section class="section-card"><div class="section-title"><div><h3>Sender profile</h3><p>Used by campaigns and generated copy</p></div></div><form id="senderForm"><div class="inline-fields"><div class="field"><label>Sender name</label><input id="senderName" value="${esc(profile.sender_name || "")}"></div><div class="field"><label>Agency name</label><input id="agencyName" value="${esc(profile.agency_name || "")}"></div><div class="field"><label>Website URL</label><input id="websiteUrl" value="${esc(profile.website_url || "")}"></div><div class="field"><label>Tracking base URL</label><input id="trackingBaseUrl" value="${esc(profile.tracking_base_url || "")}"></div><div class="field"><label>Daily send limit</label><input id="dailySendLimit" type="number" min="1" max="500" value="${profile.daily_send_limit || 25}"></div><div class="field"><label>Deliverability floor</label><input id="deliverabilityFloor" type="number" min="50" max="100" value="${profile.deliverability_floor || 90}"></div></div><button class="btn btn-primary" type="submit">Save sender</button></form></section>
      <section class="section-card"><div class="section-title"><div><h3>Controls</h3><p>Operational safeguards</p></div></div><div class="check-row"><div class="switch-row"><div><strong>Open tracking</strong><span>Requires a public tracking URL</span></div><label class="switch"><input id="openTracking" type="checkbox" ${profile.open_tracking_enabled ? "checked" : ""}><i></i></label></div><div class="switch-row"><div><strong>Reply sync</strong><span>Stop follow-ups after replies</span></div><label class="switch"><input id="replySync" type="checkbox" ${profile.reply_sync_enabled !== false ? "checked" : ""}><i></i></label></div><div class="switch-row"><div><strong>Warmup mode</strong><span>Ramp volume gradually</span></div><label class="switch"><input id="warmupEnabled" type="checkbox" ${profile.warmup_enabled ? "checked" : ""}><i></i></label></div><div class="switch-row"><div><strong>Spam guard</strong><span>Block low quality copy</span></div><label class="switch"><input id="spamGuard" type="checkbox" ${profile.spam_guard_enabled !== false ? "checked" : ""}><i></i></label></div></div><div class="grid-4" style="margin-top:14px">${metric("Health", profile.health_score || 0, "primary")}${metric("Inbox readiness", deliverability.status || "unknown", inboxTone)}${metric("Sent today", profile.today_sent || 0, "warn")}${metric("Warmup target", profile.daily_warmup_target || 40, "good")}</div></section>
      <section class="section-card"><div class="section-title"><div><h3>Warm-up</h3><p>Build sender reputation with gradual volume</p></div><div><button class="btn" id="runWarmupBtn">Run now</button><button class="btn" id="stopWarmupBtn">Reset</button></div></div><div class="grid-2" style="margin-bottom:10px"><div class="field"><label>Recipients (comma separated)</label><input id="warmupRecipients" value="${esc(profile.warmup_recipients || "")}" placeholder="friend@email.com, second@gmail.com"></div><div class="field"><label>Daily warmup target</label><input id="dailyWarmupTarget" type="number" min="1" max="500" value="${profile.daily_warmup_target || 40}"></div></div><div class="check-row"><div class="switch-row"><div><strong>Warmup status</strong><span>${warmupStatusLabel(profile.warmup_status)}</span></div><span class="status ${warmupTone(profile.warmup_status)}">${profile.warmup_status || "not_started"}</span></div></div><p class="muted" style="margin-top:8px;font-size:12px;color:var(--muted)">Day ${profile.warmup_day_count || 0} · Target ${profile.daily_warmup_target || 40}/day</p></section></div>`;
  }

  function bindCurrentPage() {
    const search = $("leadSearch");
    if (search) search.oninput = () => {
      S.search = search.value;
      const visible = filteredLeads();
      $("leadCount").textContent = `${visible.length} visible of ${S.leads.length} ${S.filter === "archived" ? "archived" : "active"} contacts`;
      $("leadResults").innerHTML = leadTable(visible);
    };
    const file = $("leadFile");
    if (file) file.onchange = () => { $("fileLabel").textContent = file.files[0] ? file.files[0].name : "Drop or choose lead file"; };
    const addLeadForm = $("addLeadForm");
    if (addLeadForm) addLeadForm.onsubmit = addLead;
    const campaignForm = $("campaignForm");
    if (campaignForm) campaignForm.onsubmit = createCampaignDraft;
    const followupForm = $("followupForm");
    if (followupForm) followupForm.onsubmit = saveFollowups;
    const finderForm = $("finderForm");
    if (finderForm) finderForm.onsubmit = findEmail;
    const verifierForm = $("verifierForm");
    if (verifierForm) verifierForm.onsubmit = verifyEmail;
    const bulkFinderForm = $("bulkFinderForm");
    if (bulkFinderForm) bulkFinderForm.onsubmit = bulkFind;
    const aiForm = $("aiForm");
    if (aiForm) aiForm.onsubmit = generateEmail;
    const senderForm = $("senderForm");
    if (senderForm) senderForm.onsubmit = saveSender;
    const runWarmup = $("runWarmupBtn");
    if (runWarmup) runWarmup.onclick = runWarmupNow;
    const stopWarmup = $("stopWarmupBtn");
    if (stopWarmup) stopWarmup.onclick = resetWarmup;
  }

  async function createCampaignDraft(event) {
    event.preventDefault();
    try {
      const campaign = await api("/api/campaigns/", {
        method: "POST",
        body: {
          name: val("campaignName"),
          audience_filter: val("campaignIndustry") ? { industry: val("campaignIndustry") } : {}
        }
      });
      S.campaigns = [campaign, ...S.campaigns];
      notice("Campaign draft saved.", "success");
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }

  async function saveLead(event) {
    event.preventDefault();
    const id = $("editLeadId").value;
    try {
      await api(`/api/leads/${id}`, {
        method: "PUT",
        body: {
          name: val("editLeadName"),
          email: val("editLeadEmail"),
          company: val("editLeadCompany"),
          industry: val("editLeadIndustry"),
          niche: val("editLeadNiche"),
          phone: val("editLeadPhone")
        }
      });
      await Promise.all([loadLeads(false), loadDashboardStats(false)]);
      notice("Lead details saved.", "success");
      closeDrawer();
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }

  async function archiveLead(id) {
    if (!window.confirm("Archive this contact? It will be removed from the outreach queue.")) return;
    try {
      await api(`/api/leads/${id}/archive`, { method: "POST", body: {} });
      await Promise.all([loadLeads(false), loadDashboardStats(false)]);
      closeDrawer();
      notice("Contact archived and removed from sending eligibility.", "success");
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }

  async function addLead(event) {
    event.preventDefault();
    try {
      await api("/api/leads/", { method: "POST", body: { name: val("leadName"), email: val("leadEmail"), company: val("leadCompany"), industry: val("leadIndustry"), niche: val("leadNiche") } });
      notice("Lead added.", "success");
      await Promise.all([loadLeads(false), loadDashboardStats(false)]);
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }
  async function uploadLeads(launch) {
    const input = $("leadFile");
    const file = input && input.files[0];
    if (!file) { notice("Choose a lead file first.", "warn"); return false; }
    const body = new FormData();
    body.append("file", file);
    body.append("replace_existing", checked("replaceExisting") ? "true" : "false");
    body.append("require_solution", checked("requireSolution") ? "true" : "false");
    try {
      notice("Uploading leads...");
      const result = await api("/api/leads/upload/", { method: "POST", body });
      $("uploadResult").textContent = `Created ${result.created || 0}, updated ${result.updated || 0}, skipped ${result.skipped || 0}.`;
      await Promise.all([loadLeads(false), loadDashboardStats(false)]);
      notice("Lead import complete.", "success");
      if (launch) await startCampaign();
      renderPage();
      return true;
    } catch (err) { notice(err.message, "error"); return false; }
  }
  async function startCampaign() {
    closeDrawer();
    try {
      notice("Starting campaign...");
      const data = await api("/api/campaign/start/", { method: "POST" });
      S.campaign = data.campaign || S.campaign;
      S.view = "campaigns";
      await Promise.all([loadCampaign(false), loadDashboardStats(false), loadLeads(false)]);
      notice("Campaign started.", "success");
      renderShell();
    } catch (err) {
      notice(err.message, "error");
      await loadCampaign(false);
      renderPage();
    }
  }
  async function startSavedCampaign(id) {
    try {
      notice("Checking audience, sender setup, and deliverability...");
      const data = await api(`/api/campaigns/${id}/start`, { method: "POST", body: {} });
      S.campaign = data.campaign || S.campaign;
      await Promise.all([loadCampaigns(false), loadDashboardStats(false)]);
      notice(`Campaign launched for ${data.queue.total} eligible contacts.`, "success");
      renderPage();
    } catch (err) {
      notice(err.message, "error");
      await loadCampaigns(false);
      renderPage();
    }
  }
  async function saveFollowups(event) {
    event.preventDefault();
    try {
      await api("/api/followups/settings/", { method: "POST", body: { enabled: checked("fuEnabled"), followup_1_delay_days: Number(val("fu1") || 3), followup_2_delay_days: Number(val("fu2") || 7), final_bump_delay_days: Number(val("fu3") || 12), followup_1_date: null, followup_2_date: null, final_bump_date: null } });
      notice("Follow-up rules saved.", "success");
      await loadFollowups(false);
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }
  async function findEmail(event) {
    event.preventDefault();
    const output = $("finderResults");
    output.innerHTML = "";
    try {
      const data = await api("/api/email-finder/", { method: "POST", body: { name: val("finderName"), domain: val("finderDomain"), company: val("finderDomain") } });
      if (data.note && !(data.results || []).length) {
        output.innerHTML = `<div class="notice error">${esc(data.note)}</div>`;
        return;
      }
      output.innerHTML = (data.results || []).map((result) => `<div class="result"><div><strong>${esc(result.email)}</strong><span class="muted small">${esc(result.status || "Possible")} · ${esc(result.source || "")}</span><div class="muted small">${esc(result.evidence || "")}</div></div><button class="btn btn-sm" data-copy="${esc(result.email)}">Copy</button></div>`).join("") || `<div class="empty">No results.</div>`;
    } catch (err) { output.innerHTML = `<div class="notice error">${esc(err.message)}</div>`; }
  }
  async function verifyEmail(event) {
    event.preventDefault();
    const output = $("verifyResults");
    const verifyBtn = document.querySelector("#verifierForm .btn-primary");
    if (verifyBtn) { verifyBtn.disabled = true; verifyBtn.textContent = "Verifying…"; }
    try {
      const data = await api("/api/email-verifier/", { method: "POST", body: { email: val("verifyEmail") } });
      output.innerHTML = renderVerifyResult(data);
    } catch (err) {
      output.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
    } finally {
      if (verifyBtn) { verifyBtn.disabled = false; verifyBtn.textContent = "Verify"; }
    }
  }
  function renderVerifyResult(data) {
    if (!data || !data.status) return `<div class="empty">No result.</div>`;
    const status = (String(data.status || "UNKNOWN")).toUpperCase();
    const incomplete = status === "VERIFICATION_FAILED";
    const c = data.checks || {};

    // Normalize each check to one of: "ok" | "no" | "na" (not checked).
    const s = (ok) => (ok === true || ok === 1 || ok === "1" || ok === "yes" || ok === "Yes")
      ? "ok" : (ok === false || ok === 0 || ok === "0" || ok === "no" || ok === "No")
      ? "no" : "na";
    const cs = { syntax: s(c.syntax), domain: s(c.domain_exists), mx: s(c.mx_configured) };

    const risk = (String(data.risk_level || "unknown")).toUpperCase();
    const riskTone = risk === "HIGH" ? "failed" : (risk === "MEDIUM" ? "warn" : "idle");
    // Single source of truth for displayed confidence (from the backend).
    let confidence = String(data.confidence || "unknown").toUpperCase();
    const provider = data.provider || "Not determined";

    // Everything below is rendered from the SAME result object (data.checks and
    // data.status), so it can never contradict the details section.

    let body;
    if (incomplete) {
      // DNS/MX genuinely could not complete -> "Verification incomplete".
      body = `<div class="notice warn" style="margin:0 0 10px"><strong>⚠ Verification incomplete</strong><div class="muted small">DNS/mail verification could not be completed. The checks below were not performed.</div></div>
        <div class="check-row">
          <div class="check warn"><b>?</b><div><strong>Valid email format</strong><span>Not checked</span></div></div>
          <div class="check warn"><b>?</b><div><strong>Domain</strong><span>Not checked</span></div></div>
          <div class="check warn"><b>?</b><div><strong>MX / mail server</strong><span>Not checked</span></div></div>
          <div class="check warn"><b>⚠</b><div><strong>Specific inbox</strong><span>Unknown</span></div></div>
        </div>`;
      // Confidence on an inconclusive run must not be presented as verified.
      if (["HIGH", "MEDIUM", "LOW"].includes(confidence)) confidence = "INCONCLUSIVE";
    } else {
      const rows = [];
      rows.push(verCheckState(cs.syntax, "Valid email format", "Email syntax is valid."));
      rows.push(verCheckState(cs.domain, "Domain exists", `${esc(data.domain || "the domain")} exists.`));
      rows.push(verCheckState(cs.mx, "Mail server found", "The domain has valid mail servers."));
      if (c.disposable) rows.push(`<div class="check bad"><b>⚠</b><div><strong>Disposable address</strong><span>Disposable/temporary provider.</span></div></div>`);
      if (c.role_account) rows.push(`<div class="check warn"><b>⚠</b><div><strong>Role/shared address</strong><span>Shared inbox such as info@ / sales@ / support@.</span></div></div>`);
      const mb = String(c.mailbox_exists || "UNKNOWN").toUpperCase();
      if (mb === "CONFIRMED") {
        rows.push(`<div class="check"><b>✓</b><div><strong>Specific inbox: confirmed</strong><span>Mail server accepted the recipient (delivery still not guaranteed).</span></div></div>`);
      } else if (mb === "NOT_CONFIRMED") {
        rows.push(`<div class="check bad"><b>✗</b><div><strong>Specific inbox: rejected</strong><span>The mail server rejected this exact mailbox.</span></div></div>`);
      } else {
        rows.push(`<div class="check warn"><b>⚠</b><div><strong>Specific inbox: not confirmed</strong><span>The exact mailbox could not be independently confirmed.</span></div></div>`);
      }
      body = `<div class="check-row">${rows.join("")}</div>`;
    }

    const smtpStatus = (data.smtp && data.smtp.status) || "NOT_PERFORMED";
    const smtpDetail = (data.smtp && data.smtp.detail) || "";
    const detailItems = [
      `Syntax: ${cs.syntax === "ok" ? "Valid" : (cs.syntax === "no" ? "Invalid" : "Not checked")}`,
      `Domain: ${cs.domain === "ok" ? "Valid" : (cs.domain === "no" ? "Invalid" : "Not checked")}`,
      `MX: ${cs.mx === "ok" ? "Configured" : (cs.mx === "no" ? "Missing" : "Not checked")}`,
      `Mailbox: ${String((c.mailbox_exists || "UNKNOWN")).toUpperCase()}`,
      `SMTP: ${smtpStatus}`,
      `Confidence: ${confidence}`,
      `Provider: ${provider}`,
    ].join("\n");

    return `<div class="metric" style="margin:0 0 10px">
        <strong style="font-size:20px;word-break:break-word">${esc(data.email)}</strong>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap">
          <span class="status ${status === "VERIFICATION_FAILED" ? "warn" : (["INVALID","INVALID_DOMAIN","NO_MAIL_SERVER"].includes(status) ? "failed" : "idle")}">${esc(status)}</span>
          <span class="status ${riskTone}">Risk: ${esc(risk)}</span>
          <span class="muted small">${esc(provider)}</span>
        </div>
      </div>
      ${body}
      <div class="muted small" style="margin:10px 0 0"><strong>Recommendation:</strong> ${esc(data.recommendation || "")}</div>
      <details style="margin-top:10px"><summary class="muted small" style="cursor:pointer">Verification details ▾</summary>
        <pre class="muted small" style="margin:8px 0 0;white-space:pre-wrap;line-height:1.6">${esc(detailItems)}</pre>
        ${smtpDetail ? `<div class="muted small" style="margin-top:6px">${esc(smtpDetail)}</div>` : ""}
        ${(data.evidence && data.evidence.length) ? `<ul class="muted small" style="margin:8px 0 0;padding-left:18px">${data.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
      </details>`;
  }
  function verCheckState(state, label, detail) {
    if (state === "ok") return `<div class="check"><b>✓</b><div><strong>${esc(label)}</strong><span>${esc(detail)}</span></div></div>`;
    if (state === "no") return `<div class="check bad"><b>✗</b><div><strong>${esc(label)}</strong><span>Failed</span></div></div>`;
    return `<div class="check warn"><b>?</b><div><strong>${esc(label)}</strong><span>Not checked</span></div></div>`;
  }
  async function bulkFind(event) {
    event.preventDefault();
    const lines = val("bulkProspects").split(/\n+/).map((line) => line.trim()).filter(Boolean);
    const prospects = lines.map((line) => {
      const [name, domain] = line.split(",").map((part) => (part || "").trim());
      return { name, domain, company: domain };
    });
    const output = $("bulkResults");
    try {
      const data = await api("/api/email-finder/bulk/", { method: "POST", body: { prospects } });
      output.innerHTML = (data.results || []).map((row) => `<div class="result"><div><strong>${esc(row.request.name || "Prospect")}</strong><span>${(row.results || []).map((result) => `${esc(result.email)} (${esc(result.status || "Possible")})`).join(", ") || (row.note ? esc(row.note) : "No result")}</span></div><span class="status idle">${(row.results || []).length}</span></div>`).join("");
    } catch (err) { output.innerHTML = `<div class="notice error">${esc(err.message)}</div>`; }
  }
  async function generateEmail(event) {
    event.preventDefault();
    const output = $("aiOutput");
    const status = $("writerStatus");
    status.textContent = "generating";
    status.className = "status running";
    try {
      const data = await api("/api/ai/generate", { method: "POST", body: { lead: { name: val("aiName"), company: val("aiCompany"), industry: val("aiIndustry"), niche: val("aiNiche") }, goal: val("aiGoal"), tone: val("aiTone") } });
      output.value = data.email || "";
      status.textContent = "ready";
      status.className = "status sent";
    } catch (err) { status.textContent = "failed"; status.className = "status failed"; notice(err.message, "error"); }
  }
  async function saveSender(event) {
    event.preventDefault();
    try {
      await api("/api/sender/profile/", { method: "POST", body: { sender_name: val("senderName"), agency_name: val("agencyName"), website_url: val("websiteUrl"), tracking_base_url: val("trackingBaseUrl"), daily_send_limit: Number(val("dailySendLimit") || 25), deliverability_floor: Number(val("deliverabilityFloor") || 90), open_tracking_enabled: checked("openTracking"), reply_sync_enabled: checked("replySync"), warmup_enabled: checked("warmupEnabled"), warmup_recipients: val("warmupRecipients"), daily_warmup_target: Number(val("dailyWarmupTarget") || 40), spam_guard_enabled: checked("spamGuard") } });
      notice("Sender profile saved.", "success");
      await Promise.all([loadSender(false), loadDeliverability(false)]);
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }
  async function silentReplySync() {
    if (S.replySyncBusy) return;
    S.replySyncBusy = true;
    try {
      const result = await api("/api/replies/sync/", { method: "POST", body: { limit: 100, unread_only: false } });
      if ((result.updated || 0) > 0) {
        await Promise.all([loadLeads(false), loadReport(false), loadDashboardStats(false)]);
        renderPage();
      }
    } catch (err) {
      console.warn("Silent reply sync failed", err);
    } finally {
      S.replySyncBusy = false;
    }
  }
  async function runWarmupNow() {
    try {
      const result = await api("/api/warmup/run/", { method: "POST", body: {} });
      notice(`Warm-up run complete: ${result.sent || 0} sent (target ${result.target || 0}).`, "success");
      await loadSender(false);
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }
  async function resetWarmup() {
    try {
      await api("/api/warmup/stop/", { method: "POST", body: {} });
      notice("Warm-up reset.", "success");
      await loadSender(false);
      renderPage();
    } catch (err) { notice(err.message, "error"); }
  }
  async function runReplySync() {
    if (S.replySyncBusy) return;
    S.replySyncBusy = true;
    try {
      notice("Syncing replies...");
      const result = await api("/api/replies/sync/", { method: "POST", body: { limit: 100, unread_only: false } });
      await Promise.all([loadLeads(false), loadReport(false), loadDashboardStats(false)]);
      notice(`Reply sync complete: ${result.updated || 0} updated, ${result.matched || 0} matched, ${result.checked || 0} checked.`, "success");
      renderPage();
    } catch (err) { notice(err.message, "error"); }
    finally { S.replySyncBusy = false; }
  }
  async function openLead(id) {
    const panel = $("drawerPanel");
    const drawer = $("drawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    panel.innerHTML = `<div class="empty">Loading lead...</div>`;
    try {
      const [lead, timeline, score] = await Promise.all([api(`/api/leads/${id}`), api(`/api/leads/${id}/timeline`), api(`/api/leads/${id}/score`)]);
      const replyTypeBadge = lead.reply_type ? `<span class="status replied" style="margin-left:8px;font-size:11px">${esc(lead.reply_type)}</span>` : "";
      const latestReply = lead.reply_summary || "";
      const latestReplySection = latestReply
        ? `<section class="section-card" style="margin-top:14px"><div class="section-title"><div><h3>Latest Reply</h3><p>${esc(lead.reply_type || "unknown")} reply</p></div></div><p>${esc(latestReply)}</p></section>`
        : "";
      panel.innerHTML = `<div class="drawer-head"><div><h2 style="margin:0">${esc(lead.name || "Unnamed lead")}</h2><p class="muted">${esc(lead.email || "")}</p></div><button class="btn btn-sm" data-close-drawer>Close</button></div><div class="grid-3">${metric("Score", score.score || 0, "primary")}${metric("Opens", lead.open_count || 0, "good")}${metric("Follow-ups", lead.followup_count || 0, "warn")}</div><section class="section-card" style="margin-top:14px"><div class="section-title"><div><h3>Contact details</h3><p>Update this record or archive it from future outreach.</p></div><span class="status ${getStatus(lead)}">${getStatus(lead)}</span></div><form id="editLeadForm"><input id="editLeadId" type="hidden" value="${lead.id}"><div class="field"><label for="editLeadName">Name</label><input id="editLeadName" value="${esc(lead.name || "")}" ${lead.archived_at ? "disabled" : ""}></div><div class="field"><label for="editLeadEmail">Email</label><input id="editLeadEmail" type="email" required value="${esc(lead.email || "")}" ${lead.archived_at ? "disabled" : ""}></div><div class="field"><label for="editLeadCompany">Company</label><input id="editLeadCompany" value="${esc(lead.company || "")}" ${lead.archived_at ? "disabled" : ""}></div><div class="field"><label for="editLeadIndustry">Industry</label><input id="editLeadIndustry" value="${esc(lead.industry || "")}" ${lead.archived_at ? "disabled" : ""}></div><div class="field"><label for="editLeadNiche">Solution / niche</label><input id="editLeadNiche" value="${esc(lead.niche || "")}" ${lead.archived_at ? "disabled" : ""}></div><div class="field"><label for="editLeadPhone">Phone</label><input id="editLeadPhone" value="${esc(lead.phone || "")}" ${lead.archived_at ? "disabled" : ""}></div><div class="split-actions"><button class="btn btn-primary" type="submit" ${lead.archived_at ? "disabled" : ""}>Save details</button>${lead.archived_at ? "" : `<button class="btn btn-danger" type="button" data-archive-lead="${lead.id}">Archive contact</button>`}</div></form><p class="muted small">${esc(lead.last_error || "No recent send error")}</p>${replyTypeBadge}</section>${latestReplySection}<section class="section-card" style="margin-top:14px"><div class="section-title"><div><h3>Timeline</h3><p>Recent lead events</p></div></div><div class="timeline">${(timeline || []).map((item) => {
        const etype = String(item.event_type || item.type || "").toLowerCase();
        const isReply = etype === "reply_received";
        const isOpen = etype === "open";
        const isNote = etype === "note";
        const isFailed = etype === "email_failed";
        const iconChar = isReply ? "&#9993;" : isOpen ? "&#128065;" : isNote ? "&#128221;" : isFailed ? "&#9888;" : "&#9993;";
        const badge = item.reply_type && isReply ? ` <span style="font-size:11px;opacity:0.8">(${esc(item.reply_type)})</span>` : "";
        const eventStatus = isReply ? (item.status || item.reply_type || "") : "";
        const statusLabel = eventStatus && !["sent", "replied", "pending", "unknown"].includes(String(eventStatus).toLowerCase()) ? ` <span class="status ${esc(eventStatus)}" style="font-size:10px;margin-left:4px">${esc(eventStatus)}</span>` : "";
        return `<div class="timeline-item"><strong>${iconChar} ${esc(item.title || "Event")}${badge}${statusLabel}</strong><div>${esc(item.body || item.description || "")}</div><span>${fmtDate(item.occurred_at || item.created_at)}</span></div>`;
      }).join("") || `<div class="empty">No timeline yet.</div>`}</div></section>`;
    } catch (err) { panel.innerHTML = `<div class="notice error">${esc(err.message)}</div>`; }
  }
  function closeDrawer() {
    const drawer = $("drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  }
  async function loadMe(render = true) {
    if (!S.token) return;
    try {
      S.user = await api("/api/auth/me");
      localStorage.setItem(STORE, JSON.stringify({ token: S.token, refreshToken: S.refreshToken, user: S.user }));
      if (render) { notice("Account refreshed.", "success"); renderShell(); }
    } catch (err) { if (render) notice(err.message, "error"); }
  }

  document.addEventListener("click", async (event) => {
    const publicAuth = event.target.closest("[data-public-auth]");
    if (publicAuth) {
      event.preventDefault();
      S.authMode = publicAuth.dataset.publicAuth;
      showAuth();
      return;
    }
    if (event.target.closest("#backToMarketing")) {
      showMarketing();
      return;
    }
    const navButton = event.target.closest("[data-nav]");
    if (navButton) { closeDrawer(); S.view = navButton.dataset.nav; $("sidebar").classList.remove("open"); renderShell(); return; }
    const filterButton = event.target.closest("[data-filter]");
    if (filterButton) {
      S.filter = filterButton.dataset.filter;
      S.search = "";
      await loadLeads(false);
      renderPage();
      return;
    }
    const action = event.target.closest("[data-action]");
    if (action) {
      const name = action.dataset.action;
      if (name === "refreshLeads") await loadLeads(true);
      if (name === "refreshCampaigns") await loadCampaigns(true);
      if (name === "uploadLeads") await uploadLeads(false);
      if (name === "uploadLaunch") await uploadLeads(true);
      if (name === "startCampaign") await startCampaign();
      if (name === "refreshCampaign") await loadCampaign(true);
      if (name === "syncReplies") await runReplySync();
      if (name === "resetFailed") { await api("/api/leads/reset-failed/", { method: "POST" }); await loadCore(false); notice("Failed leads reset.", "success"); }
      if (name === "copyEmail" && navigator.clipboard) { await navigator.clipboard.writeText($("aiOutput")?.value || ""); notice("Copied email.", "success"); }
      if (name === "loadMe") await loadMe();
      return;
    }
    const launchButton = event.target.closest("[data-launch-campaign]");
    if (launchButton) { await startSavedCampaign(launchButton.dataset.launchCampaign); return; }
    const leadButton = event.target.closest("[data-lead]");
    if (leadButton) { openLead(leadButton.dataset.lead); return; }
    const archiveButton = event.target.closest("[data-archive-lead]");
    if (archiveButton) { await archiveLead(archiveButton.dataset.archiveLead); return; }
    const copyButton = event.target.closest("[data-copy]");
    if (copyButton && navigator.clipboard) { await navigator.clipboard.writeText(copyButton.dataset.copy); notice("Copied.", "success"); return; }
    if (event.target.closest("[data-close-drawer]")) closeDrawer();
  });

  document.addEventListener("submit", (event) => {
    if (event.target.id === "editLeadForm") saveLead(event);
  });
  document.addEventListener("change", async (event) => {
    if (event.target.id !== "activityRange") return;
    S.activityDays = Number(event.target.value);
    try {
      await Promise.all([loadReport(false), loadDailyActivity(false)]);
      renderPage();
    } catch (err) {
      notice(err.message, "error");
    }
  });

  $("refreshBtn").onclick = () => loadCore(true);
  $("menuBtn").onclick = () => $("sidebar").classList.toggle("open");
  $("themeToggle").onclick = () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("ea_theme", document.documentElement.dataset.theme);
    renderShell();
  };
  $("logoutBtn").onclick = () => { clearAuth(); showAuth(); };
  document.querySelectorAll("[data-auth-mode]").forEach((button) => { button.onclick = () => { S.authMode = button.dataset.authMode; renderAuthMode(); }; });
  $("localMode").onclick = () => {
    $("authError").textContent = "Login or sign up is required so lead and campaign data stays protected.";
  };
  $("authForm").onsubmit = async (event) => {
    event.preventDefault();
    $("authSubmit").disabled = true;
    $("authError").textContent = "";
    try {
      const body = { email: val("authEmail"), password: val("authPassword") };
      if (S.authMode === "signup") body.full_name = val("fullName");
      const data = await api(`/api/auth/${S.authMode}`, { method: "POST", body }, false);
      setAuth(data);
      await loadMe(false);
      showApp();
    } catch (err) { $("authError").textContent = err.message; }
    finally { $("authSubmit").disabled = false; }
  };
  function boot() {
    document.documentElement.dataset.theme = localStorage.getItem("ea_theme") || "light";
    try {
      const saved = JSON.parse(localStorage.getItem(STORE) || "null");
      if (saved && saved.token) {
        S.token = saved.token;
        S.refreshToken = saved.refreshToken;
        S.user = saved.user;
        loadMe(false).finally(showApp);
        return;
      }
    } catch (err) {}
    showMarketing();
  }
  boot();
})();
