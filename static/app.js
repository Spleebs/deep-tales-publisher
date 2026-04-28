/* ─── Deep Tales Control Room — app.js ─── */

const API_SECRET = "ShoboloBoboloAlpha232323";

// State
let chapters = [];
let activeChapterId = null;
let notesModalAction = null; // "reject" | "revise"
let marketRefreshTimer = null;

// ─── Utility ──────────────────────────────────────────────────────
function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: {
      "X-API-Secret": API_SECRET,
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  }).then(async r => {
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  });
}

function toast(msg, type = "ok") {
  const el = document.createElement("div");
  el.className = `toast${type === "error" ? " error" : ""}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function statusClass(status) {
  const map = {
    draft: "led-draft",
    pending_review: "led-pending",
    revision_requested: "led-revision",
    approved: "led-approved",
    published: "led-published",
  };
  return map[status] || "led-draft";
}

function badgeClass(status) {
  const map = {
    draft: "badge-draft",
    pending_review: "badge-pending",
    revision_requested: "badge-revision",
    approved: "badge-approved",
    published: "badge-published",
  };
  return map[status] || "badge-draft";
}

function statusLabel(status) {
  const map = {
    draft: "DRAFT",
    pending_review: "IN REVIEW",
    revision_requested: "REVISION",
    approved: "APPROVED",
    published: "PUBLISHED",
  };
  return map[status] || status.toUpperCase();
}

function fmtPrice(n) {
  if (!n && n !== 0) return "—";
  if (n >= 1) return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return "$" + n.toFixed(5);
}

function fmtChange(n) {
  if (n === null || n === undefined) return "—";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

// ─── Clock ────────────────────────────────────────────────────────
function tickClock() {
  const now = new Date();
  const hh = String(now.getUTCHours()).padStart(2, "0");
  const mm = String(now.getUTCMinutes()).padStart(2, "0");
  const ss = String(now.getUTCSeconds()).padStart(2, "0");
  document.getElementById("clock").textContent = `${hh}:${mm}:${ss}`;
  const yyyy = now.getUTCFullYear();
  const mo = String(now.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(now.getUTCDate()).padStart(2, "0");
  document.getElementById("date-display").textContent = `${yyyy}.${mo}.${dd}`;
}
setInterval(tickClock, 1000);
tickClock();

// ─── Chapter List ─────────────────────────────────────────────────
async function loadChapters() {
  try {
    chapters = await api("/chapters");
    renderChapterList();
    if (activeChapterId) {
      const still = chapters.find(c => c.id === activeChapterId);
      if (still) renderChapterDetail(still);
    }
  } catch (e) {
    document.getElementById("chapter-list").innerHTML =
      `<div class="loading">Error: ${e.message}</div>`;
  }
}

function renderChapterList() {
  const el = document.getElementById("chapter-list");
  if (!chapters.length) {
    el.innerHTML = `<div class="loading">No chapters yet.</div>`;
    return;
  }
  el.innerHTML = chapters.map(c => `
    <div class="chapter-item${c.id === activeChapterId ? " active" : ""}"
         data-id="${c.id}" onclick="selectChapter('${c.id}')">
      <span class="led ${statusClass(c.status)}"></span>
      <span class="chapter-item-title">${escHtml(c.title)}</span>
    </div>
  `).join("");
}

function selectChapter(id) {
  activeChapterId = id;
  renderChapterList();
  const ch = chapters.find(c => c.id === id);
  if (ch) renderChapterDetail(ch);
}

// ─── Chapter Detail ───────────────────────────────────────────────
function renderChapterDetail(ch) {
  const badge = document.getElementById("chapter-status-badge");
  badge.textContent = statusLabel(ch.status);
  badge.className = `status-badge ${badgeClass(ch.status)}`;

  const bodyDisplay = ch.body_formatted
    ? ch.body_formatted
    : (ch.body || "").replace(/ \|\|\| /g, "\n\n");

  const imageSection = ch.image_url
    ? `<div class="chapter-image-wrap">
         <img src="${escHtml(ch.image_url)}" alt="Chapter illustration" />
       </div>`
    : `<div class="chapter-image-wrap">
         <div class="chapter-image-placeholder">No image yet — click GENERATE</div>
       </div>`;

  const predCta = ch.prediction_cta
    ? `<div class="field-label">PREDICTION CTA</div>
       <div class="prediction-display">${escHtml(ch.prediction_cta)}</div>`
    : "";

  const revNotes = ch.revision_notes
    ? `<div class="field-label">REVISION NOTES</div>
       <div class="revision-notes-display">${escHtml(ch.revision_notes)}</div>`
    : "";

  const shaLine = ch.sha256
    ? `<div class="field-label">SHA-256</div>
       <div class="sha-display">${ch.sha256}</div>`
    : "";

  const actionBar = buildActionBar(ch);

  document.getElementById("chapter-detail").innerHTML = `
    <div class="chapter-title-display">${escHtml(ch.title)}</div>
    ${imageSection}
    <div>
      <div class="field-label">STORY BODY</div>
      <div class="body-preview">${escHtml(bodyDisplay)}</div>
    </div>
    ${shaLine}
    ${predCta}
    ${revNotes}
    ${actionBar}
  `;

  wireActionBar(ch);
}

function buildActionBar(ch) {
  const s = ch.status;
  const btns = [];

  if (s === "draft" || s === "revision_requested") {
    btns.push(`<button class="btn btn-generate" id="action-generate">
      GENERATE IMAGE + FORMAT
    </button>`);
  }

  if (s === "draft" && ch.body_formatted) {
    btns.push(`<button class="btn btn-review" id="action-review">SEND TO REVIEW</button>`);
  }

  if (s === "pending_review") {
    btns.push(`<button class="btn btn-approve" id="action-approve">APPROVE</button>`);
    btns.push(`<button class="btn btn-reject" id="action-reject">REJECT</button>`);
    btns.push(`<button class="btn btn-revise" id="action-revise">REVISE WITH AI</button>`);
  }

  if (s === "revision_requested") {
    btns.push(`<button class="btn btn-revise" id="action-revise">REVISE WITH AI</button>`);
  }

  if (s === "approved") {
    btns.push(`<button class="btn btn-post" id="action-post">⚡ POST TO SUBSTACK</button>`);
  }

  if (!btns.length) return "";
  return `<div class="action-bar">${btns.join("")}</div>`;
}

function wireActionBar(ch) {
  const id = ch.id;

  const genBtn = document.getElementById("action-generate");
  if (genBtn) genBtn.addEventListener("click", () => generateChapter(id, genBtn));

  const reviewBtn = document.getElementById("action-review");
  if (reviewBtn) reviewBtn.addEventListener("click", () => reviewChapter(id, reviewBtn));

  const approveBtn = document.getElementById("action-approve");
  if (approveBtn) approveBtn.addEventListener("click", () => approveChapter(id, approveBtn));

  const rejectBtn = document.getElementById("action-reject");
  if (rejectBtn) rejectBtn.addEventListener("click", () => openNotesModal("reject", id));

  const reviseBtn = document.getElementById("action-revise");
  if (reviseBtn) reviseBtn.addEventListener("click", () => openNotesModal("revise", id));

  const postBtn = document.getElementById("action-post");
  if (postBtn) postBtn.addEventListener("click", () => openPostModal(id));
}

// ─── Actions ──────────────────────────────────────────────────────
async function generateChapter(id, btn) {
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>GENERATING...`;
  try {
    const ch = await api(`/chapters/${id}/generate`, { method: "POST" });
    toast("Image generated and story formatted.");
    updateLocalChapter(ch);
    renderChapterDetail(ch);
    renderChapterList();
  } catch (e) {
    toast(`Generate failed: ${e.message}`, "error");
    btn.disabled = false;
    btn.innerHTML = "GENERATE IMAGE + FORMAT";
  }
}

async function reviewChapter(id, btn) {
  btn.disabled = true;
  try {
    const ch = await api(`/chapters/${id}/review`, { method: "POST" });
    toast("Chapter submitted for review.");
    updateLocalChapter(ch);
    renderChapterDetail(ch);
    renderChapterList();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
    btn.disabled = false;
  }
}

async function approveChapter(id, btn) {
  btn.disabled = true;
  try {
    const ch = await api(`/chapters/${id}/approve`, { method: "POST" });
    toast("Chapter approved. Ready to post.");
    updateLocalChapter(ch);
    renderChapterDetail(ch);
    renderChapterList();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
    btn.disabled = false;
  }
}

// ─── Notes Modal (reject / revise) ───────────────────────────────
function openNotesModal(action, id) {
  notesModalAction = { action, id };
  document.getElementById("notes-modal-title").textContent =
    action === "reject" ? "REJECTION NOTES" : "REVISION NOTES";
  document.getElementById("notes-field").value = "";
  showModal("modal-notes");
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-cancel-notes").addEventListener("click", () => hideModal("modal-notes"));
  document.getElementById("btn-submit-notes").addEventListener("click", submitNotesModal);
});

async function submitNotesModal() {
  const notes = document.getElementById("notes-field").value.trim();
  if (!notes) { toast("Notes cannot be empty.", "error"); return; }

  const { action, id } = notesModalAction;
  hideModal("modal-notes");

  const endpoint = action === "reject" ? "reject" : "revise";
  const btn = document.getElementById(`action-${action}`);
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>...`; }

  try {
    const ch = await api(`/chapters/${id}/${endpoint}`, {
      method: "POST",
      body: JSON.stringify({ notes }),
    });
    toast(action === "reject" ? "Chapter sent back for revision." : "Chapter revised. Please re-generate.");
    updateLocalChapter(ch);
    renderChapterDetail(ch);
    renderChapterList();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
    if (btn) { btn.disabled = false; btn.innerHTML = action === "reject" ? "REJECT" : "REVISE WITH AI"; }
  }
}

// ─── Post Modal ───────────────────────────────────────────────────
let pendingPostId = null;

function openPostModal(id) {
  pendingPostId = id;
  showModal("modal-post");
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-cancel-post").addEventListener("click", () => {
    hideModal("modal-post");
    pendingPostId = null;
  });
  document.getElementById("btn-confirm-post").addEventListener("click", confirmPost);
});

async function confirmPost() {
  if (!pendingPostId) return;
  const id = pendingPostId;
  pendingPostId = null;
  hideModal("modal-post");

  const btn = document.getElementById("action-post");
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>TRANSMITTING...`; }

  try {
    const ch = await api(`/chapters/${id}/post`, { method: "POST" });
    toast("PUBLISHED to Substack.");
    updateLocalChapter(ch);
    renderChapterDetail(ch);
    renderChapterList();
  } catch (e) {
    toast(`Publish failed: ${e.message}`, "error");
    if (btn) { btn.disabled = false; btn.innerHTML = "⚡ POST TO SUBSTACK"; }
  }
}

// ─── New Chapter Modal (AI draft) ────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-new-chapter").addEventListener("click", async () => {
    // Show modal and populate focal character preview from cached market data
    const preview = document.getElementById("draft-market-preview");
    preview.innerHTML = `<span class="loading">Loading market weights...</span>`;
    showModal("modal-new");
    try {
      const data = await api("/market/weights");
      const focal = (data.focal || []).map(f => `${f.character} (${f.symbol})`).join(" + ");
      const lines = (data.ranked || []).slice(0, 3).map(t => {
        const sign = t.price_change_7d_pct >= 0 ? "+" : "";
        return `<span class="${t.price_change_7d_pct >= 0 ? "pos" : "neg"}">${t.symbol} ${sign}${t.price_change_7d_pct.toFixed(1)}%</span>`;
      }).join("  ");
      preview.innerHTML = `
        <div class="mini-focal">FOCAL: <strong>${escHtml(focal)}</strong></div>
        <div class="mini-market">${lines}</div>
      `;
    } catch (e) {
      preview.innerHTML = `<span class="loading">Market preview unavailable</span>`;
    }
  });
  document.getElementById("btn-cancel-new").addEventListener("click", () => hideModal("modal-new"));
  document.getElementById("btn-save-new").addEventListener("click", generateAiDraft);
});

async function generateAiDraft() {
  const saveBtn = document.getElementById("btn-save-new");
  saveBtn.disabled = true;
  saveBtn.innerHTML = `<span class="spinner"></span>GENERATING...`;

  try {
    const ch = await api("/chapters/ai-draft", { method: "POST" });
    hideModal("modal-new");
    toast(`Chapter drafted: "${ch.title}"`);
    chapters.unshift(ch);
    activeChapterId = ch.id;
    renderChapterList();
    renderChapterDetail(ch);
  } catch (e) {
    toast(`AI draft failed: ${e.message}`, "error");
  } finally {
    saveBtn.disabled = false;
    saveBtn.innerHTML = "GENERATE";
  }
}

// ─── Modal helpers ────────────────────────────────────────────────
function showModal(id) { document.getElementById(id).classList.remove("hidden"); }
function hideModal(id) { document.getElementById(id).classList.add("hidden"); }

// ─── Market Data ──────────────────────────────────────────────────
async function loadMarket() {
  try {
    const data = await api("/market/weights");
    renderMarketPrices(data.ranked);
    renderWeightModel(data);
  } catch (e) {
    document.getElementById("market-feed").innerHTML =
      `<div class="loading">Market data unavailable</div>`;
  }
}

function renderMarketPrices(ranked) {
  const el = document.getElementById("market-feed");
  if (!ranked || !ranked.length) {
    el.innerHTML = `<div class="loading">No data</div>`;
    return;
  }
  el.innerHTML = ranked.map(t => {
    const change = t.price_change_7d_pct;
    const cls = change >= 0 ? "pos" : "neg";
    return `<div class="market-token">
      <span class="token-symbol">${t.symbol}</span>
      <span class="token-name">${t.character}</span>
      <span class="token-price">${fmtPrice(t.price_usd)}</span>
      <span class="token-change ${cls}">${fmtChange(change)}</span>
    </div>`;
  }).join("");
}

function renderWeightModel(data) {
  const el = document.getElementById("weight-model");
  const focalIds = (data.focal || []).map(f => f.character);
  const maxW = data.ranked[0]?.weight || 1;

  const rows = data.ranked.map((t, i) => {
    const isFocal = focalIds.includes(t.character);
    const barPct = Math.round((t.weight / maxW) * 100);
    return `<div class="weight-row">
      <span class="weight-rank">${i + 1}</span>
      <span class="weight-char">${t.character}</span>
      <div class="weight-bar-wrap"><div class="weight-bar" style="width:${barPct}%"></div></div>
      ${isFocal ? `<span class="weight-focal">FOCAL</span>` : ""}
    </div>`;
  }).join("");

  const events = (data.events || []).map(ev => {
    if (ev.type === "termination_risk")
      return `<div class="weight-event">⚠ TERMINATION RISK: ${ev.character} (${ev.symbol})</div>`;
    if (ev.type === "isolated_move")
      return `<div class="weight-event">⚡ ${ev.character}: ${ev.move_pct > 0 ? "+" : ""}${ev.move_pct?.toFixed(1)}% isolated move</div>`;
    if (ev.type === "market_surge_all")
      return `<div class="weight-event">📈 MARKET-WIDE SURGE — all characters</div>`;
    if (ev.type === "market_crash_all")
      return `<div class="weight-event">📉 MARKET-WIDE CRASH — all characters</div>`;
    return "";
  }).join("");

  el.innerHTML = rows + events;
}

// ─── Analytics ────────────────────────────────────────────────────
async function loadAnalytics() {
  try {
    const data = await api("/analytics");
    const pub = data.publication || {};
    const strip = document.getElementById("analytics-strip");
    const subs = pub.subscriber_count ?? pub.subscriberCount ?? "—";
    const posts = (data.stats?.recent_posts || []).length;

    strip.innerHTML = `
      <div class="analytics-stat">
        <span class="stat-val">${typeof subs === "number" ? subs.toLocaleString() : subs}</span>
        <span class="stat-label">Subscribers</span>
      </div>
      <div class="analytics-stat">
        <span class="stat-val">${pub.name || "deeptalesai"}</span>
        <span class="stat-label">Publication</span>
      </div>
      <div class="analytics-stat">
        <span class="stat-val">${posts}</span>
        <span class="stat-label">Recent Posts</span>
      </div>
    `;
  } catch (e) {
    document.getElementById("analytics-strip").innerHTML =
      `<span class="loading">Analytics unavailable (${e.message})</span>`;
  }
}

// ─── Helpers ──────────────────────────────────────────────────────
function updateLocalChapter(updated) {
  const idx = chapters.findIndex(c => c.id === updated.id);
  if (idx !== -1) chapters[idx] = updated;
  else chapters.unshift(updated);
}

function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ─── Init ─────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  loadChapters();
  loadMarket();
  loadAnalytics();

  // Auto-refresh market every 5 minutes
  marketRefreshTimer = setInterval(() => {
    loadMarket();
    loadChapters(); // also keep chapter list fresh
  }, 5 * 60 * 1000);
});
