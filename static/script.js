const els = {
  q: document.getElementById('q'),
  company: document.getElementById('company'),
  internOnly: document.getElementById('internOnly'),
  nzOnly: document.getElementById('nzOnly'),
  sort: document.getElementById('sort'),
  refreshBtn: document.getElementById('refreshBtn'),
  refreshStatus: document.getElementById('refreshStatus'),
  board: document.getElementById('board'),
  emptyState: document.getElementById('emptyState'),
  lastRefresh: document.getElementById('lastRefresh'),
  clock: document.getElementById('clock'),
};

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  const now = new Date();
  const days = Math.floor((now - d) / (1000 * 60 * 60 * 24));
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString('en-NZ', { year: 'numeric', month: 'short', day: 'numeric' });
}

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => HTML_ESCAPES[c]);
}

function isRecentlySeen(iso) {
  if (!iso) return false;
  const d = new Date(iso);
  if (isNaN(d)) return false;
  return (Date.now() - d.getTime()) < 24 * 60 * 60 * 1000;
}

async function loadCompanies() {
  const previous = els.company.value;
  const res = await fetch('/api/companies');
  const companies = await res.json();
  els.company.innerHTML = '<option value="">All companies</option>' + companies.map(c =>
    `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)} (${c.count})</option>`
  ).join('');
  if (previous && companies.some(c => c.name === previous)) {
    els.company.value = previous;
  }
}

async function loadJobs() {
  const params = new URLSearchParams({
    q: els.q.value,
    company: els.company.value,
    intern_only: els.internOnly.checked,
    nz_only: els.nzOnly.checked,
    sort: els.sort.value,
  });
  const res = await fetch(`/api/jobs?${params}`);
  const jobs = await res.json();
  renderJobs(jobs);
}

function renderJobs(jobs) {
  if (!jobs.length) {
    els.board.innerHTML = '';
    els.emptyState.hidden = false;
    return;
  }
  els.emptyState.hidden = true;
  els.board.innerHTML = jobs.map(j => {
    const isNew = isRecentlySeen(j.first_seen_at);
    const applied = !!j.applied_at;
    return `
    <div class="job-row ${applied ? 'is-applied' : ''}" data-job-id="${escapeHtml(j.id)}">
      <span class="status-badge ${j.is_intern ? 'intern' : 'standard'}">
        ${j.is_intern ? 'Intern/Grad' : 'Standard'}
      </span>
      <span class="job-company">${escapeHtml(j.company)}</span>
      <span class="job-title">
        ${isNew ? '<span class="new-badge">NEW</span>' : ''}${escapeHtml(j.title)}
      </span>
      <span class="job-location">${escapeHtml(j.location || 'Not specified')}</span>
      <span class="job-time">${fmtTime(j.posted_at)}</span>
      <label class="applied-toggle">
        <input type="checkbox" class="applied-checkbox" ${applied ? 'checked' : ''}>
        <span>Applied</span>
      </label>
      <a class="job-apply" href="${escapeHtml(j.url || '#')}" target="_blank" rel="noopener">View →</a>
    </div>
  `;
  }).join('');
}

async function toggleApplied(jobId, applied) {
  const method = applied ? 'POST' : 'DELETE';
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/applied`, { method });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

async function loadStatus() {
  const res = await fetch('/api/status');
  const data = await res.json();
  const parts = [];
  if (data.last_refresh) {
    const d = new Date(data.last_refresh.ran_at);
    parts.push(`Last refreshed: ${d.toLocaleString('en-NZ')} · ${data.last_refresh.total_jobs} jobs total`);
  } else {
    parts.push('No data fetched yet — click the button above to start');
  }
  if (data.next_auto_refresh) {
    const n = new Date(data.next_auto_refresh);
    parts.push(`Auto-refreshes every ${data.auto_refresh_interval_minutes}min · next at ${n.toLocaleTimeString('en-NZ')}`);
  }
  els.lastRefresh.textContent = parts.join(' · ');
}

async function doRefresh() {
  els.refreshBtn.disabled = true;
  els.refreshBtn.querySelector('.refresh-icon').classList.add('spinning');
  els.refreshStatus.textContent = 'Fetching the latest data from each company\'s ATS…';
  els.refreshStatus.classList.remove('error');
  try {
    const res = await fetch('/api/refresh', { method: 'POST' });
    const data = await res.json();
    if (data.errors && data.errors.length) {
      els.refreshStatus.textContent =
        `Done, ${data.total} jobs total. But ${data.errors.length} companies failed to fetch: ` +
        data.errors.map(e => `${e.company}(${e.error})`).join('; ');
      els.refreshStatus.classList.add('error');
    } else {
      els.refreshStatus.textContent = `Refresh complete, ${data.total} jobs fetched.`;
    }
    await loadCompanies();
    await loadJobs();
    await loadStatus();
  } catch (e) {
    els.refreshStatus.textContent = `Refresh failed: ${e}`;
    els.refreshStatus.classList.add('error');
  } finally {
    els.refreshBtn.disabled = false;
    els.refreshBtn.querySelector('.refresh-icon').classList.remove('spinning');
  }
}

function tickClock() {
  const now = new Date().toLocaleTimeString('en-NZ', {
    timeZone: 'Pacific/Auckland', hour12: false,
  });
  els.clock.textContent = now;
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

const debouncedLoadJobs = debounce(loadJobs, 300);

[els.q, els.company, els.internOnly, els.nzOnly, els.sort].forEach(el => {
  const isTextInput = el.tagName === 'INPUT' && el.type === 'text';
  el.addEventListener(isTextInput ? 'input' : 'change', isTextInput ? debouncedLoadJobs : loadJobs);
});
els.refreshBtn.addEventListener('click', doRefresh);

els.board.addEventListener('change', async (e) => {
  if (!e.target.classList.contains('applied-checkbox')) return;
  const checkbox = e.target;
  const row = checkbox.closest('.job-row');
  const jobId = row.dataset.jobId;
  const applied = checkbox.checked;
  row.classList.toggle('is-applied', applied);
  try {
    await toggleApplied(jobId, applied);
  } catch (err) {
    checkbox.checked = !applied;
    row.classList.toggle('is-applied', !applied);
  }
});

tickClock();
setInterval(tickClock, 1000);
loadCompanies().then(loadJobs);
loadStatus();

// The backend auto-refreshes on a schedule; poll so the board picks up
// new/closed jobs without the user needing to click "Refresh" manually.
setInterval(() => {
  loadCompanies();
  loadJobs();
  loadStatus();
}, 2 * 60 * 1000);
