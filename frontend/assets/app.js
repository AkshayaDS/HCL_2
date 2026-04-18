/* ═══════════════════════════════════════════════════════════════════════════
 * HCL AI Force — Shared Frontend Utilities
 * ═══════════════════════════════════════════════════════════════════════════
 */

const API_BASE = window.API_BASE || '';

function getJSON(url) {
  return fetch(`${API_BASE}${url}`, { headers: authHeaders() }).then(handle);
}

const API = {
  createCase:   (formData) => {
    // Attach the authenticated operator identity so the backend can stamp
    // the case with the operator's username and full name. Send the auth
    // token plus legacy X-User-* headers for back-compat.
    try {
      const me = (typeof currentIdentity === 'function') ? currentIdentity() : { username: '', name: '', role: '' };
      if (formData && formData.append) {
        if (me.username) formData.append('operator_username', me.username);
        if (me.name)     formData.append('operator_name', me.name);
        if (me.role)     formData.append('operator_role', me.role);
      }
      return fetch(`${API_BASE}/api/cases`, { method: 'POST', body: formData, headers: authHeaders() }).then(handle);
    } catch {
      return fetch(`${API_BASE}/api/cases`, { method: 'POST', body: formData }).then(handle);
    }
  },
  submitCase:   (id) => post(`/api/cases/${id}/submit`, {}),
  listCases:    (params={}) => getJSON(`/api/cases?${new URLSearchParams(params)}`),
  getCase:      (id) => getJSON(`/api/cases/${id}`),
  reject:       (id, note='') => post(`/api/cases/${id}/reject`, { note }),
  resubmit:     (id, note='') => post(`/api/cases/${id}/resubmit`, { note }),
  repair:       (id, override=null) => post(`/api/cases/${id}/repair`, override ? { override } : {}),
  replace:      (id, required_qty=1, override=null) => post(`/api/cases/${id}/replace`, override ? { required_qty, override } : { required_qty }),
  approvePR:    (id) => post(`/api/cases/${id}/approve-pr`, {}),
  rejectPR:     (id, note='') => post(`/api/cases/${id}/reject-pr`, { note }),
  stats:        () => getJSON(`/api/dashboard/stats`),
  inventory:    () => getJSON(`/api/inventory`),
  ewmCheck:     (component, qty=1) => getJSON(`/api/inventory/check?component=${encodeURIComponent(component)}&qty=${qty}`),
  suppliers:    () => getJSON(`/api/suppliers`),
  suppliersRank:(component) => getJSON(`/api/suppliers/rank?component=${encodeURIComponent(component)}`),
  replacePreview: (component, qty=1, opts={}) => getJSON(
    `/api/replace-preview?component=${encodeURIComponent(component)}&qty=${qty}`
    + (opts.caseId ? `&case_id=${encodeURIComponent(opts.caseId)}` : '')
    + (opts.urgency ? `&urgency=${encodeURIComponent(opts.urgency)}` : '')
  ),
  maintenanceOrders: () => getJSON(`/api/maintenance-orders`),
  reservations: () => getJSON(`/api/reservations`),
  purchaseOrders: () => getJSON(`/api/purchase-orders`),
  purchaseRequisitions: (status) => getJSON(`/api/purchase-requisitions${status ? '?status='+status : ''}`),
  // ─── Auth ───
  authLogin:    (username, password, role) => post('/api/auth/login', { username, password, role }),
  authRegister: (username, password, name, role, email) => post('/api/auth/register', { username, password, name, role, email }),
  authMe:       () => getJSON('/api/auth/me'),
  authLogout:   () => post('/api/auth/logout', {}),
};

function authToken() {
  try {
    return localStorage.getItem('hcl_auth_token') || sessionStorage.getItem('hcl_auth_token') || '';
  } catch { return ''; }
}

function authHeaders() {
  const headers = {};
  const tok = authToken();
  if (tok) headers['Authorization'] = 'Bearer ' + tok;
  try {
    const me = (typeof currentIdentity === 'function') ? currentIdentity() : { username: '', name: '', role: '' };
    if (me.username) headers['X-User-Id']   = me.username;
    if (me.name)     headers['X-User-Name'] = me.name;
    if (me.role)     headers['X-User-Role'] = me.role;
  } catch {}
  return headers;
}

function post(url, body) {
  const headers = { 'Content-Type': 'application/json', ...authHeaders() };
  return fetch(`${API_BASE}${url}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body || {}),
  }).then(handle);
}

async function handle(res) {
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const msg = (data && (data.message || data.error)) || `Request failed (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    err.payload = data || {};
    err.code = (data && data.error) || '';
    throw err;
  }
  return data;
}

/* ───── Toast ───── */
function toast(message, kind='info', timeout=3800) {
  let host = document.getElementById('toast-container');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-container';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 250);
  }, timeout);
}

/* ───── Formatting helpers ───── */
const fmt = {
  dateTime: (iso) => {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch { return iso; }
  },
  pct: (n) => `${Math.round((Number(n)||0) * 100)}%`,
  money: (n) => `$${(Number(n)||0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
  severity: (s) => {
    const cls = { HIGH: 'badge-high', MEDIUM: 'badge-medium', LOW: 'badge-low' }[s] || 'badge-neutral';
    return `<span class="badge ${cls}">${s || '—'}</span>`;
  },
  status: (s) => `<span class="badge status-${s}">${(s||'').replace(/_/g,' ')}</span>`,
  ewmPill: (s) => `<span class="badge pill-ewm-${s}">EWM · ${s || '—'}</span>`,
};

/* ───── Theme toggle ───── */
function getTheme() {
  try { return localStorage.getItem('hcl-theme') || 'dark'; } catch { return 'dark'; }
}
function setTheme(mode) {
  const m = mode === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', m);
  try { localStorage.setItem('hcl-theme', m); } catch {}
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.innerHTML = m === 'dark' ? '🌙' : '☀️';
}
function applyStoredTheme() {
  setTheme(getTheme());
}
applyStoredTheme();

/* ───── Header / footer / role chip ───── */
function renderHeader(activePage) {
  const header = document.querySelector('header.app-header');
  if (!header) return;
  const role = activePage === 'operator' ? 'Operator · Inspector'
             : activePage === 'supervisor' ? 'Supervisor'
             : '';
  const currentTheme = getTheme();
  const me = (typeof currentIdentity === 'function') ? currentIdentity() : { username: '', name: '', role: '' };
  const signedIn = !!me.username;
  const initials = signedIn
    ? (me.name || me.username || '?').trim().split(/\s+/).map(p => p[0]).join('').slice(0, 2).toUpperCase()
    : '';
  const userBadge = signedIn
    ? `<div class="user-menu" id="user-menu">
         <button class="user-pill" id="user-pill" type="button" title="Signed in as ${esc(me.name || me.username)}">
           <span class="avatar">${esc(initials)}</span>
           <span class="name">${esc(me.name || me.username)}</span>
           <span class="chev">▾</span>
         </button>
         <div class="user-dropdown" id="user-dropdown" hidden>
           <div class="user-dropdown-head">
             <div class="avatar lg">${esc(initials)}</div>
             <div>
               <div class="name">${esc(me.name || me.username)}</div>
               <div class="muted small">${esc(me.username || '')} · ${esc((me.role || '').toUpperCase())}</div>
             </div>
           </div>
           <button type="button" class="user-dropdown-item" id="btn-signout">↩ Sign out</button>
         </div>
       </div>`
    : '';

  // Header nav intentionally omitted — users should not be able to hop
  // between role pages from the chrome once they're signed in. The role
  // chip + user pill stay so identity is always visible.
  header.innerHTML = `
    <div class="logo-area">
      <img class="hcl-logo-img" src="/assets/img/android-chrome-192x192.png" alt="HCLTech" />
      <div class="app-title">AI <span class="accent">Force</span><span class="sub"> · Intelligent Maintenance</span></div>
    </div>
    <div class="header-actions">
      ${role ? `<span class="role-chip">${role}</span>` : ''}
      ${userBadge}
      <button class="theme-toggle" id="theme-toggle" title="Toggle light / dark">${currentTheme === 'dark' ? '🌙' : '☀️'}</button>
    </div>
  `;
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const next = getTheme() === 'dark' ? 'light' : 'dark';
      setTheme(next);
    });
  }
  const pill = document.getElementById('user-pill');
  const dropdown = document.getElementById('user-dropdown');
  if (pill && dropdown) {
    pill.addEventListener('click', (e) => {
      e.stopPropagation();
      dropdown.hidden = !dropdown.hidden;
    });
    document.addEventListener('click', (e) => {
      if (!document.getElementById('user-menu')?.contains(e.target)) dropdown.hidden = true;
    });
    const so = document.getElementById('btn-signout');
    if (so) so.addEventListener('click', () => { signOut(); });
  }

  // Inject flying background jet if it doesn't exist
  if (!document.getElementById('flying-jet')) {
    const jet = document.createElement('div');
    jet.id = 'flying-jet';
    jet.className = 'bg-jet';
    jet.textContent = '✈️';
    document.body.appendChild(jet);
  }
}

function renderFooter() {
  const f = document.querySelector('footer.app-footer');
  if (!f) return;
  f.innerHTML = `© ${new Date().getFullYear()} <strong>HCL Technologies</strong> · AI Force · Intelligent Maintenance & Procurement Platform`;
  // Mount the floating AI assistant on any page that renders the footer.
  // Pages that call renderHeader() also call renderFooter(); pages that
  // don't (login) mount the assistant manually.
  try {
    const page = (document.body.getAttribute('data-page')
      || (location.pathname.includes('operator')   ? 'operator'
        : location.pathname.includes('supervisor') ? 'supervisor'
        : location.pathname.includes('login')      ? 'login'
        : 'home'));
    renderAssistant(page);
  } catch {}
}

/* ───── Workflow progress bar ───── */
/* steps: array of {label, state} where state ∈ 'pending'|'active'|'done'|'end'|'fail' */
function renderWorkflow(container, steps) {
  if (!container) return;
  container.innerHTML = steps.map(s => `<div class="step ${s.state || 'pending'}">${s.label}</div>`).join('');
}

function workflowForCase(c) {
  // Returns 6 steps for the case lifecycle
  const s = (state) => state;
  const status = c.status;
  const stage = c.stage;

  const out = [
    { label: 'Image · Agent', state: 'done' },
    { label: 'SAP MO', state: c.initial_mo_id || c.repair_mo_id ? 'done' : 'pending' },
    { label: 'Approval 1', state: 'pending' },
    { label: 'Decision', state: 'pending' },
    { label: 'Approval 2', state: 'pending' },
    { label: 'Completed', state: 'pending' },
  ];

  if (status === 'NEW') {
    out[2].state = 'active';
  } else if (status === 'REJECTED') {
    out[2].state = 'fail'; out[3].state='fail'; out[4].state='fail'; out[5].state='fail';
  } else if (status === 'RESUBMIT') {
    out[2].state = 'fail';
  } else if (status === 'UNDER_REPAIR') {
    out[2].state = 'done'; out[3].state = 'done'; out[4].state = 'done'; out[5].state = 'end';
  } else if (status === 'RESERVED') {
    out[2].state = 'done'; out[3].state = 'done'; out[4].state = 'done'; out[5].state = 'end';
  } else if (status === 'PR_PENDING_APPROVAL') {
    out[2].state = 'done'; out[3].state = 'done'; out[4].state = 'active';
  } else if (status === 'PR_REJECTED') {
    out[2].state = 'done'; out[3].state = 'done'; out[4].state = 'fail';
  } else if (status === 'PROCUREMENT_COMPLETED') {
    out[2].state='done'; out[3].state='done'; out[4].state='done'; out[5].state='end';
  }
  return out;
}

/* ───── Fonts (inject once) ───── */
(function injectFonts() {
  if (document.getElementById('hcl-fonts')) return;
  const link = document.createElement('link');
  link.id = 'hcl-fonts';
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2?family=Barlow:wght@300;400;500;600;700&family=Barlow+Condensed:wght@500;600;700&display=swap';
  document.head.appendChild(link);
})();

/* ───── Escape HTML helper ───── */
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ═══════════════════════════════════════════════════════════════════════════
 * MODERN PROMPT / CONFIRM DIALOGS
 * Drop-in replacements for window.prompt() and window.confirm() that render
 * as branded, glass-morphism cards with gradient headers. All dialogs return
 * a Promise — `await ui.prompt(...)` resolves to the trimmed string (or null
 * on cancel) and `await ui.confirm(...)` resolves to a boolean.
 * ═══════════════════════════════════════════════════════════════════════════ */
const ui = (function () {
  function ensureHost() {
    let host = document.getElementById('ui-dialog-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'ui-dialog-host';
      document.body.appendChild(host);
    }
    return host;
  }

  function close(host) {
    if (!host) return;
    const bd = host.querySelector('.ui-dialog-backdrop');
    if (!bd) { host.innerHTML = ''; return; }
    bd.classList.add('ui-leaving');
    setTimeout(() => { host.innerHTML = ''; }, 180);
  }

  /**
   * Prompt-style dialog.
   * @param {Object} opts
   *   title, message, placeholder, defaultValue, inputType,
   *   confirmLabel, cancelLabel, tone ('primary'|'success'|'danger'|'warn'),
   *   icon, required (bool), multiline (bool), minRows
   * @returns {Promise<string|null>}
   */
  function prompt(opts = {}) {
    return new Promise((resolve) => {
      const host = ensureHost();
      const {
        title = 'Input required',
        message = '',
        placeholder = '',
        defaultValue = '',
        inputType = 'text',
        confirmLabel = 'Confirm',
        cancelLabel = 'Cancel',
        tone = 'primary',
        icon = '✎',
        required = false,
        multiline = false,
        minRows = 3,
        details = [],
      } = opts;

      const detailBlock = (Array.isArray(details) && details.length)
        ? `<div class="ui-dialog-details">
             ${details.map(d => `
               <div class="ui-dialog-detail-row">
                 <span class="ui-dialog-detail-k">${esc(d.label)}</span>
                 <span class="ui-dialog-detail-v">${d.html || esc(d.value || '—')}</span>
               </div>`).join('')}
           </div>`
        : '';

      const field = multiline
        ? `<textarea id="ui-dlg-input" class="ui-dialog-input" rows="${minRows}" placeholder="${esc(placeholder)}">${esc(defaultValue)}</textarea>`
        : `<input id="ui-dlg-input" type="${esc(inputType)}" class="ui-dialog-input" placeholder="${esc(placeholder)}" value="${esc(defaultValue)}">`;

      host.innerHTML = `
        <div class="ui-dialog-backdrop" data-ui-bd>
          <div class="ui-dialog ui-dialog-${tone}" role="dialog" aria-modal="true">
            <div class="ui-dialog-head">
              <div class="ui-dialog-icon">${esc(icon)}</div>
              <div class="ui-dialog-titles">
                <h3>${esc(title)}</h3>
                ${message ? `<p>${esc(message)}</p>` : ''}
              </div>
              <button type="button" class="ui-dialog-x" data-ui-cancel aria-label="Close">✕</button>
            </div>
            <div class="ui-dialog-body">
              ${detailBlock}
              ${field}
              <div class="ui-dialog-error" id="ui-dlg-error" hidden></div>
            </div>
            <div class="ui-dialog-foot">
              <button type="button" class="btn btn-ghost" data-ui-cancel>${esc(cancelLabel)}</button>
              <button type="button" class="btn btn-${tone}" data-ui-ok>${esc(confirmLabel)}</button>
            </div>
          </div>
        </div>`;

      const bd = host.querySelector('[data-ui-bd]');
      const input = host.querySelector('#ui-dlg-input');
      const errEl = host.querySelector('#ui-dlg-error');

      setTimeout(() => { try { input && input.focus(); input && input.select && input.select(); } catch {} }, 60);

      function cleanupAndResolve(val) {
        document.removeEventListener('keydown', onKey);
        close(host);
        resolve(val);
      }

      function submit() {
        const raw = (input && 'value' in input) ? String(input.value || '') : '';
        const val = raw.trim();
        if (required && !val) {
          if (errEl) { errEl.textContent = 'This field is required.'; errEl.hidden = false; }
          input && input.classList.add('ui-invalid');
          return;
        }
        cleanupAndResolve(val);
      }

      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); cleanupAndResolve(null); }
        if (e.key === 'Enter' && !multiline) { e.preventDefault(); submit(); }
        if (e.key === 'Enter' && multiline && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
      }

      host.querySelectorAll('[data-ui-cancel]').forEach(el => el.addEventListener('click', () => cleanupAndResolve(null)));
      host.querySelector('[data-ui-ok]').addEventListener('click', submit);
      bd.addEventListener('click', (e) => { if (e.target === bd) cleanupAndResolve(null); });
      document.addEventListener('keydown', onKey);
    });
  }

  /**
   * Confirm-style dialog.
   * @param {Object} opts — title, message, confirmLabel, cancelLabel, tone, icon, details[], requireType
   *   If requireType is a non-empty string, the user has to type that exact
   *   word to enable the confirm button (extra-safe double-verification).
   * @returns {Promise<boolean>}
   */
  function confirm(opts = {}) {
    return new Promise((resolve) => {
      const host = ensureHost();
      const {
        title = 'Are you sure?',
        message = '',
        confirmLabel = 'Confirm',
        cancelLabel = 'Cancel',
        tone = 'warn',
        icon = '⚠',
        details = [],
        requireType = '',
      } = opts;

      const detailBlock = (Array.isArray(details) && details.length)
        ? `<div class="ui-dialog-details">
             ${details.map(d => `
               <div class="ui-dialog-detail-row">
                 <span class="ui-dialog-detail-k">${esc(d.label)}</span>
                 <span class="ui-dialog-detail-v">${d.html || esc(d.value || '—')}</span>
               </div>`).join('')}
           </div>`
        : '';

      const typeBlock = requireType
        ? `<div class="ui-dialog-type-guard">
             <label>To proceed, type <code>${esc(requireType)}</code> below</label>
             <input type="text" id="ui-dlg-type" class="ui-dialog-input" placeholder="${esc(requireType)}" autocomplete="off" spellcheck="false">
           </div>`
        : '';

      host.innerHTML = `
        <div class="ui-dialog-backdrop" data-ui-bd>
          <div class="ui-dialog ui-dialog-${tone}" role="alertdialog" aria-modal="true">
            <div class="ui-dialog-head">
              <div class="ui-dialog-icon">${esc(icon)}</div>
              <div class="ui-dialog-titles">
                <h3>${esc(title)}</h3>
                ${message ? `<p>${esc(message)}</p>` : ''}
              </div>
              <button type="button" class="ui-dialog-x" data-ui-cancel aria-label="Close">✕</button>
            </div>
            ${detailBlock || typeBlock ? `<div class="ui-dialog-body">${detailBlock}${typeBlock}</div>` : ''}
            <div class="ui-dialog-foot">
              <button type="button" class="btn btn-ghost" data-ui-cancel>${esc(cancelLabel)}</button>
              <button type="button" class="btn btn-${tone}" data-ui-ok ${requireType ? 'disabled' : ''}>${esc(confirmLabel)}</button>
            </div>
          </div>
        </div>`;

      const bd = host.querySelector('[data-ui-bd]');
      const okBtn = host.querySelector('[data-ui-ok]');
      const typeInput = host.querySelector('#ui-dlg-type');

      if (typeInput) {
        typeInput.addEventListener('input', () => {
          const ok = typeInput.value.trim().toUpperCase() === String(requireType).trim().toUpperCase();
          okBtn.disabled = !ok;
          typeInput.classList.toggle('ui-valid', ok);
        });
        setTimeout(() => { try { typeInput.focus(); } catch {} }, 60);
      }

      function cleanupAndResolve(v) {
        document.removeEventListener('keydown', onKey);
        close(host);
        resolve(v);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); cleanupAndResolve(false); }
        if (e.key === 'Enter' && !okBtn.disabled) { e.preventDefault(); cleanupAndResolve(true); }
      }
      host.querySelectorAll('[data-ui-cancel]').forEach(el => el.addEventListener('click', () => cleanupAndResolve(false)));
      okBtn.addEventListener('click', () => { if (!okBtn.disabled) cleanupAndResolve(true); });
      bd.addEventListener('click', (e) => { if (e.target === bd) cleanupAndResolve(false); });
      document.addEventListener('keydown', onKey);
    });
  }

  /** Pure info/alert dialog. Returns a promise that resolves on dismissal.
   *  Supports an optional `details[]` block — same shape as confirm():
   *  [{ label, value?, html? }, …] — rendered as a compact two-column summary. */
  function alert(opts = {}) {
    return new Promise((resolve) => {
      const host = ensureHost();
      const {
        title = 'Notice',
        message = '',
        confirmLabel = 'OK',
        tone = 'primary',
        icon = 'ℹ',
        details = [],
      } = opts;

      const detailBlock = (Array.isArray(details) && details.length)
        ? `<div class="ui-dialog-details">
             ${details.map(d => `
               <div class="ui-dialog-detail-row">
                 <span class="ui-dialog-detail-k">${esc(d.label)}</span>
                 <span class="ui-dialog-detail-v">${d.html || esc(d.value || '—')}</span>
               </div>`).join('')}
           </div>`
        : '';

      host.innerHTML = `
        <div class="ui-dialog-backdrop" data-ui-bd>
          <div class="ui-dialog ui-dialog-${tone}" role="dialog" aria-modal="true">
            <div class="ui-dialog-head">
              <div class="ui-dialog-icon">${esc(icon)}</div>
              <div class="ui-dialog-titles">
                <h3>${esc(title)}</h3>
                ${message ? `<p>${esc(message)}</p>` : ''}
              </div>
              <button type="button" class="ui-dialog-x" data-ui-ok aria-label="Close">✕</button>
            </div>
            ${detailBlock ? `<div class="ui-dialog-body">${detailBlock}</div>` : ''}
            <div class="ui-dialog-foot">
              <button type="button" class="btn btn-${tone}" data-ui-ok>${esc(confirmLabel)}</button>
            </div>
          </div>
        </div>`;

      function done() { document.removeEventListener('keydown', onKey); close(host); resolve(); }
      function onKey(e) { if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); done(); } }
      host.querySelectorAll('[data-ui-ok]').forEach(el => el.addEventListener('click', done));
      host.querySelector('[data-ui-bd]').addEventListener('click', (e) => { if (e.target.matches('[data-ui-bd]')) done(); });
      document.addEventListener('keydown', onKey);
    });
  }

  /** Centered result popup — thin wrapper over alert() used after supervisor
   *  actions complete (Reject, Resubmit, Repair, Replace, Approve PR, Reject PR).
   *  Always renders as a success-toned dialog unless overridden via opts.tone.
   *  Accepts the same shape as alert(): title, message, icon, details[]. */
  function result(opts = {}) {
    return alert({
      tone: 'success',
      icon: '✓',
      confirmLabel: 'Close',
      ...opts,
    });
  }

  return { prompt, confirm, alert, result };
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Identity helpers — read the authenticated operator / supervisor from
 * localStorage (written by login.html) and expose a normalised object.
 * ═══════════════════════════════════════════════════════════════════════════ */
function currentIdentity() {
  try {
    // Prefer the backend-authoritative session user payload (written by the
    // new /login flow). Fall back to legacy localStorage keys so older
    // clients keep working.
    let raw = localStorage.getItem('hcl_auth_user') || sessionStorage.getItem('hcl_auth_user') || '';
    if (raw) {
      const u = JSON.parse(raw);
      if (u && u.username) {
        return {
          username: u.username,
          name: u.name || u.username,
          role: u.role || '',
          email: u.email || '',
        };
      }
    }
    const username = localStorage.getItem('currentUser') || '';
    const role = localStorage.getItem('currentRole') || '';
    const users = JSON.parse(localStorage.getItem('ai_force_users') || '{}');
    const rec = users[username] || {};
    return {
      username: username,
      name: rec.name || username || '',
      role: rec.role || role || '',
    };
  } catch {
    return { username: '', name: '', role: '' };
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Auth guard — call at the top of operator.html / supervisor.html inline
 * scripts. Verifies the stored session token with /api/auth/me and if the
 * user's role doesn't match `expectedRole`, kicks them back to /login.
 *
 * Returns a Promise that resolves to the authenticated user record once
 * verification succeeds. If no token is present at all the user is sent
 * straight to /login without calling the backend.
 * ═══════════════════════════════════════════════════════════════════════════ */
function authGuard(expectedRole) {
  return new Promise(async (resolve) => {
    const tok = authToken();
    if (!tok) {
      window.location.replace(`/login?role=${encodeURIComponent(expectedRole || '')}`);
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/api/auth/me`, { headers: authHeaders() });
      if (!res.ok) throw new Error('unauth');
      const data = await res.json();
      const u = data.user || {};
      if (expectedRole && (u.role || '').toLowerCase() !== expectedRole.toLowerCase()) {
        window.location.replace(`/login?role=${encodeURIComponent(expectedRole)}`);
        return;
      }
      // Keep the in-browser copy in sync with what the backend says.
      try {
        const store = localStorage.getItem('hcl_auth_token') ? localStorage : sessionStorage;
        store.setItem('hcl_auth_user', JSON.stringify(u));
        localStorage.setItem('currentUser', u.username || '');
        localStorage.setItem('currentRole', u.role || '');
      } catch {}
      resolve(u);
    } catch {
      // Token rejected or server unreachable — bounce to login.
      try {
        localStorage.removeItem('hcl_auth_token');
        sessionStorage.removeItem('hcl_auth_token');
      } catch {}
      window.location.replace(`/login?role=${encodeURIComponent(expectedRole || '')}`);
    }
  });
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Floating AI Assistant widget — shared across every page.
 *
 * Maintains a per-page conversation history in memory so the backend gets
 * full context on each turn. Passes { role, page, current_case } context
 * through so the AI can answer page-specific questions.
 * ═══════════════════════════════════════════════════════════════════════════ */
// Inline SVG AI sparkle logo — Gemini-style 4-point star with a small
// companion, rendered in HCL royal-blue gradient (h=262°). Used across the
// voice assistant chrome (FAB, panel header, AI bubble avatars).
const AI_SPARKLE_SVG = `
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" class="ai-sparkle">
  <defs>
    <linearGradient id="aiSparkleGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#E8F1FF"/>
      <stop offset="45%" stop-color="#96D4FF"/>
      <stop offset="100%" stop-color="#00ABFF"/>
    </linearGradient>
    <linearGradient id="aiSparkleGrad2" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="100%" stop-color="#96D4FF"/>
    </linearGradient>
  </defs>
  <!-- Big 4-point sparkle -->
  <path d="M13 2.5 C13.4 7.9 15.5 10 20.9 10.4 C15.5 10.8 13.4 12.9 13 18.3 C12.6 12.9 10.5 10.8 5.1 10.4 C10.5 10 12.6 7.9 13 2.5 Z"
        fill="url(#aiSparkleGrad)"/>
  <!-- Small companion sparkle -->
  <path d="M22 15 C22.25 18 23.25 19 26.25 19.25 C23.25 19.5 22.25 20.5 22 23.5 C21.75 20.5 20.75 19.5 17.75 19.25 C20.75 19 21.75 18 22 15 Z"
        fill="url(#aiSparkleGrad2)"
        opacity="0.95"/>
</svg>`;

function renderAssistant(pageKey) {
  // Skip the login screen — the voice panel overlaps the sign-in form.
  if (pageKey === 'login') return;
  // If this page already has the assistant, just re-wire context.
  if (document.getElementById('voice-assistant')) {
    if (typeof window.__assistantSetPage === 'function') window.__assistantSetPage(pageKey);
    return;
  }

  const host = document.createElement('div');
  host.id = 'voice-assistant';
  host.className = 'voice-assistant';
  host.innerHTML = `
    <div class="voice-panel" id="voice-panel" aria-hidden="true">
      <div class="voice-head">
        <div class="voice-head-main">
          <div class="voice-head-avatar">${AI_SPARKLE_SVG}</div>
          <div>
            <div class="voice-head-title">HCLTech · AI Force</div>
            <div class="voice-head-sub">
              <span class="dot"></span>
              <span id="voice-status-text">Voice Agent</span>
            </div>
          </div>
        </div>
        <div class="voice-head-actions">
          <button type="button" class="voice-ico-btn" id="voice-new" title="New conversation">⟲</button>
          <button type="button" class="voice-ico-btn" id="voice-close" title="Minimise">✕</button>
        </div>
      </div>

      <div class="voice-stage">
        <div class="voice-orb" id="voice-orb" data-state="idle" role="button" aria-label="Tap to speak">
          <div class="orb-glow"></div>
          <div class="orb-ring ring-3"></div>
          <div class="orb-core">
            <div class="orb-logo">${AI_SPARKLE_SVG}</div>
          </div>
          <div class="orb-ring ring-1"></div>
          <div class="orb-ring ring-2"></div>
          <div class="orb-bars" id="voice-orb-bars" aria-hidden="true">
            <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
          </div>
        </div>
        <div class="voice-status" id="voice-status">Tap the orb to speak</div>
        <div class="voice-interim" id="voice-interim" aria-live="polite"></div>
      </div>

      <div class="voice-transcript" id="voice-transcript" aria-live="polite"></div>

      <div class="voice-actions">
        <button class="voice-action" id="voice-mic" type="button" aria-pressed="false">
          <span class="ic">🎤</span>
          <span class="lbl">Tap to speak</span>
        </button>
        <button class="voice-action ghost" id="voice-type-toggle" type="button" title="Type instead of speaking">⌨</button>
      </div>

      <form class="voice-textform" id="voice-textform" autocomplete="off" hidden>
        <input type="text" id="voice-textinput" placeholder="Type your message…" aria-label="Type message">
        <button type="submit" aria-label="Send">➤</button>
      </form>
    </div>

    <button type="button" class="voice-fab" id="voice-fab" title="Open HCLTech Voice Agent" aria-expanded="false">
      <span class="fab-glow"></span>
      <span class="fab-ring"></span>
      <span class="fab-orb"></span>
      <span class="fab-ic">${AI_SPARKLE_SVG}</span>
    </button>
  `;
  document.body.appendChild(host);

  // ── DOM refs ────────────────────────────────────────────────────────
  const panel = document.getElementById('voice-panel');
  const fab = document.getElementById('voice-fab');
  const orb = document.getElementById('voice-orb');
  const orbBars = document.getElementById('voice-orb-bars');
  const statusEl = document.getElementById('voice-status');
  const statusText = document.getElementById('voice-status-text');
  const interimEl = document.getElementById('voice-interim');
  const transcriptEl = document.getElementById('voice-transcript');
  const micBtn = document.getElementById('voice-mic');
  const micLabel = micBtn.querySelector('.lbl');
  const typeToggle = document.getElementById('voice-type-toggle');
  const textForm = document.getElementById('voice-textform');
  const textInput = document.getElementById('voice-textinput');
  const closeBtn = document.getElementById('voice-close');
  const newBtn = document.getElementById('voice-new');

  // ── State machine: IDLE → LISTENING → THINKING → SPEAKING → IDLE ────
  const STATES = { IDLE: 'idle', LISTENING: 'listening', THINKING: 'thinking', SPEAKING: 'speaking' };
  let state = STATES.IDLE;
  let currentPage = pageKey || 'home';
  /** @type {{role:'user'|'assistant', content:string}[]} */
  let history = [];
  let recognition = null;
  let audioCtx = null, analyser = null;
  let currentAudio = null, currentAudioURL = null;
  let animationId = null;
  let audioSourceNode = null;

  window.__assistantSetPage = (p) => { currentPage = p || 'home'; };

  // ── Speech Recognition (Web Speech API) ─────────────────────────────
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const listeningSupported = !!SR;
  if (listeningSupported) {
    recognition = new SR();
    recognition.lang = navigator.language || 'en-US';
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onresult = (event) => {
      let interim = '', finalText = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const r = event.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interim += r[0].transcript;
      }
      if (interim) interimEl.textContent = interim;
      if (finalText) {
        interimEl.textContent = '';
        handleUserText(finalText.trim());
      }
    };
    recognition.onerror = (event) => {
      if (event.error === 'not-allowed') {
        addTurn('ai', '⚠ Microphone access is blocked. Click the lock icon in your browser\'s address bar and allow the mic for this site.');
      } else if (event.error === 'no-speech') {
        // Silent — user just didn't speak, return to idle
      } else {
        addTurn('ai', `⚠ Speech recognition error: ${event.error}`);
      }
      setState(STATES.IDLE);
    };
    recognition.onend = () => { if (state === STATES.LISTENING) setState(STATES.IDLE); };
  } else {
    micBtn.disabled = true;
    micLabel.textContent = 'Voice not supported — use ⌨ to type';
    // Automatically show the text fallback
    textForm.hidden = false;
  }

  // ── UI helpers ──────────────────────────────────────────────────────
  function setState(s) {
    state = s;
    orb.setAttribute('data-state', s);
    const labels = {
      idle:      'Tap the orb to speak',
      listening: 'Listening… speak now',
      thinking:  'Thinking…',
      speaking:  'Speaking · tap orb to stop',
    };
    statusEl.textContent = labels[s] || '';
    micBtn.classList.toggle('active', s === 'listening');
    micBtn.setAttribute('aria-pressed', s === 'listening' ? 'true' : 'false');
    if (s === 'listening') micLabel.textContent = 'Listening…';
    else if (s === 'thinking') micLabel.textContent = 'Thinking…';
    else if (s === 'speaking') micLabel.textContent = 'Speaking…';
    else micLabel.textContent = listeningSupported ? 'Tap to speak' : 'Voice not supported';
  }

  function addTurn(who, text) {
    const d = document.createElement('div');
    d.className = 'voice-turn turn-' + who;
    const avatarHTML = who === 'user' ? '🧑' : AI_SPARKLE_SVG;
    d.innerHTML = `<div class="avatar">${avatarHTML}</div>
                   <div class="bubble">${esc(text).replace(/\n/g, '<br>')}</div>`;
    transcriptEl.appendChild(d);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  function pageContext() {
    const me = (typeof currentIdentity === 'function') ? currentIdentity() : { role: 'guest' };
    const ctx = { role: me.role || 'guest', page: currentPage, user: me };

    // ── Current case (if a modal is open on the supervisor console) ──
    const modal = document.getElementById('modal-bd');
    if (modal && window.allCases) {
      const title = modal.querySelector('.modal-head h3');
      if (title) {
        const caseId = (title.textContent || '').split('·')[0].trim();
        const match = (window.allCases || []).find(c => c.case_id === caseId);
        if (match) ctx.current_case = {
          case_id: match.case_id, component: match.component, defect: match.defect,
          damaged_area: match.damaged_area, severity: match.severity, status: match.status,
          stage: match.stage, decision: match.decision, recommendation: match.recommendation,
          ewm_status: match.ewm_status, reservation_id: match.reservation_id,
          pr_id: match.pr_id, pr_status: match.pr_status, po_id: match.po_id,
          operator_label: match.operator_label,
        };
      }
    }

    // ── Page-level snapshot that the user is looking at right now ──
    // This augments the authoritative server-side snapshot with what's
    // currently visible in the browser — useful for questions like
    // "what do I have on screen?", "which case is highlighted?", etc.
    if (window.allCases) {
      ctx.visible_case_ids = (window.allCases || []).slice(0, 12).map(c => c.case_id);
    }
    if (window.lastStats) {
      ctx.visible_stats = {
        total_cases: window.lastStats.total_cases,
        maintenance_orders: window.lastStats.maintenance_orders,
        purchase_orders: window.lastStats.purchase_orders,
        purchase_requisitions: window.lastStats.purchase_requisitions,
        status_counts: window.lastStats.status_counts,
        severity_counts: window.lastStats.severity_counts,
        decision_counts: window.lastStats.decision_counts,
      };
    }
    return ctx;
  }

  // ── Listening control ───────────────────────────────────────────────
  function startListening() {
    if (!recognition || state !== STATES.IDLE) return;
    stopSpeaking();  // cut off AI mid-sentence if user starts talking
    try {
      interimEl.textContent = '';
      setState(STATES.LISTENING);
      recognition.start();
    } catch {
      setState(STATES.IDLE);
    }
  }
  function stopListening() {
    if (!recognition) return;
    try { recognition.stop(); } catch {}
    if (state === STATES.LISTENING) setState(STATES.IDLE);
  }

  // ── ElevenLabs TTS playback with waveform visualisation ─────────────
  async function speakText(text) {
    stopSpeaking();
    setState(STATES.SPEAKING);
    try {
      const res = await fetch(`${API_BASE}/api/assistant/voice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        let detail = '';
        try { const j = await res.json(); detail = j.message || j.error || ''; } catch {}
        statusText.textContent = detail.includes('not_configured')
          ? 'ElevenLabs key missing — using browser voice'
          : 'ElevenLabs unavailable — using browser voice';
        fallbackSpeak(text);
        return;
      }
      const blob = await res.blob();
      currentAudioURL = URL.createObjectURL(blob);
      currentAudio = new Audio(currentAudioURL);
      currentAudio.onended = () => stopSpeaking();
      currentAudio.onerror  = () => stopSpeaking();

      // Waveform visualisation via AnalyserNode
      try {
        audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === 'suspended') await audioCtx.resume();
        audioSourceNode = audioCtx.createMediaElementSource(currentAudio);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 64;
        audioSourceNode.connect(analyser);
        analyser.connect(audioCtx.destination);
        const data = new Uint8Array(analyser.frequencyBinCount);
        const bars = orbBars.querySelectorAll('span');
        const tick = () => {
          if (!currentAudio || currentAudio.ended || currentAudio.paused) return;
          analyser.getByteFrequencyData(data);
          bars.forEach((b, i) => {
            const v = (data[i * 2] || 0) / 255;
            b.style.transform = `scaleY(${0.14 + v * 1.1})`;
          });
          animationId = requestAnimationFrame(tick);
        };
        tick();
      } catch { /* analyser unavailable, audio still plays */ }

      await currentAudio.play().catch(() => fallbackSpeak(text));
      statusText.textContent = 'Powered by ElevenLabs';
    } catch (err) {
      fallbackSpeak(text);
    }
  }
  function fallbackSpeak(text) {
    if ('speechSynthesis' in window) {
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.05; u.pitch = 1.0;
      u.onend = () => { if (state === STATES.SPEAKING) setState(STATES.IDLE); };
      u.onerror = () => { if (state === STATES.SPEAKING) setState(STATES.IDLE); };
      try { window.speechSynthesis.speak(u); } catch { setState(STATES.IDLE); }
    } else {
      setState(STATES.IDLE);
    }
  }
  function stopSpeaking() {
    try { if (animationId) cancelAnimationFrame(animationId); } catch {}
    animationId = null;
    try { if (currentAudio) { currentAudio.pause(); currentAudio.src = ''; } } catch {}
    try { if (currentAudioURL) URL.revokeObjectURL(currentAudioURL); } catch {}
    try { if (audioSourceNode) audioSourceNode.disconnect(); } catch {}
    currentAudio = null; currentAudioURL = null; audioSourceNode = null;
    try { if (window.speechSynthesis && window.speechSynthesis.speaking) window.speechSynthesis.cancel(); } catch {}
    orbBars.querySelectorAll('span').forEach(b => b.style.transform = 'scaleY(0.14)');
    if (state === STATES.SPEAKING) setState(STATES.IDLE);
  }

  // ── Conversation turn: user text → LLM → TTS ────────────────────────
  async function handleUserText(text) {
    if (!text) { setState(STATES.IDLE); return; }
    addTurn('user', text);
    history.push({ role: 'user', content: text });
    setState(STATES.THINKING);

    try {
      const res = await fetch(`${API_BASE}/api/assistant/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ messages: history, context: pageContext() }),
      });
      let data = null; try { data = await res.json(); } catch {}
      if (!res.ok) throw new Error((data && data.message) || `Assistant error (${res.status})`);

      const reply = (data && data.reply) || '(no response)';
      history.push({ role: 'assistant', content: reply });
      addTurn('ai', reply);

      if (data && data.source && data.source !== 'groq') {
        statusText.textContent = 'Offline mode — set GROQ_API_KEY';
      }

      await speakText(reply);
    } catch (err) {
      addTurn('ai', '⚠ ' + (err.message || 'Unable to reach the assistant.'));
      setState(STATES.IDLE);
    }
  }

  // ── UI wiring ───────────────────────────────────────────────────────
  function togglePanel(force) {
    const open = force !== undefined ? force : panel.getAttribute('aria-hidden') === 'true';
    panel.setAttribute('aria-hidden', open ? 'false' : 'true');
    fab.setAttribute('aria-expanded', open ? 'true' : 'false');
    fab.classList.toggle('active', open);
    if (!open) { stopListening(); stopSpeaking(); }
    if (open && !transcriptEl.childElementCount) {
      addTurn('ai', "Hi — I'm your Voice Agent. Tap the orb or the mic button and speak. I can explain the workflow, a case on your screen, EWM, or PR/PO approvals.");
    }
  }
  fab.addEventListener('click', () => togglePanel());
  closeBtn.addEventListener('click', () => togglePanel(false));
  newBtn.addEventListener('click', () => {
    history = []; transcriptEl.innerHTML = ''; stopListening(); stopSpeaking();
    addTurn('ai', "Fresh conversation — tap the orb when you're ready.");
  });

  const orbClick = () => {
    if (state === STATES.SPEAKING) { stopSpeaking(); return; }
    if (state === STATES.LISTENING) { stopListening(); return; }
    if (state === STATES.IDLE && listeningSupported) startListening();
  };
  orb.addEventListener('click', orbClick);
  micBtn.addEventListener('click', orbClick);

  typeToggle.addEventListener('click', () => {
    textForm.hidden = !textForm.hidden;
    if (!textForm.hidden) setTimeout(() => textInput.focus(), 30);
  });
  textForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const t = textInput.value.trim();
    if (!t) return;
    textInput.value = '';
    handleUserText(t);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panel.getAttribute('aria-hidden') === 'false') togglePanel(false);
  });
}

async function signOut() {
  try { await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', headers: authHeaders() }); } catch {}
  try {
    localStorage.removeItem('hcl_auth_token');
    localStorage.removeItem('hcl_auth_user');
    sessionStorage.removeItem('hcl_auth_token');
    sessionStorage.removeItem('hcl_auth_user');
    localStorage.removeItem('currentUser');
    localStorage.removeItem('currentRole');
  } catch {}
  window.location.replace('/login');
}
