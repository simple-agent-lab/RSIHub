import {lineageChart} from './viewer-ui.js';

const state = { entries: [], rows: [], agent: 'All', method: 'All', query: '', refreshing: false };
const groups = document.querySelector('#experiment-groups');
const summary = document.querySelector('#catalog-summary');
const refreshState = document.querySelector('#refresh-state');
const methodOrder = ['A-Evolve', 'GEPA', 'AHE', 'HyperAgents'];

const escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const formatScore = (value) => value == null ? '—' : `${(Number(value) * 100).toFixed(0)}%`;
const signedScore = (value) => `${value > 0 ? '+' : ''}${(Number(value) * 100).toFixed(0)} pp`;

function champion(generations) {
  return generations.reduce((best, candidate) => {
    if (candidate.score == null || candidate.selection_eligible === false) return best;
    return best == null || Number(candidate.score) > Number(best.score) ? candidate : best;
  }, null);
}

function derive(entry, snapshot, error = null) {
  const generations = snapshot?.generations || [];
  const seed = generations.find((item) => String(item.genid) === '0') || generations[0] || null;
  const final = champion(generations);
  return { entry, snapshot, error, generations, seed, champion: final };
}

function renderSummary(rows) {
  const ready = rows.filter((row) => !row.error);
  const complete = ready.filter((row) => row.snapshot.experiment.health === 'complete').length;
  const generations = ready.reduce((total, row) => total + row.generations.length, 0);
  const improved = ready.filter((row) => row.seed?.score != null && row.champion?.score > row.seed.score).length;
  summary.innerHTML = [
    [ready.length, 'workspaces readable'],
    [complete, 'experiments complete'],
    [generations, 'generations indexed'],
    [improved, 'champions above seed'],
  ].map(([value, label]) => `<div class="summary-item"><strong>${value}</strong><span>${label}</span></div>`).join('');
}

function filterButton(value, selected, count, kind) {
  return `<button class="filter-button ${value === selected ? 'active' : ''}" type="button" data-filter-kind="${kind}" data-filter-value="${escapeHtml(value)}"><span>${escapeHtml(value)}</span><span>${count}</span></button>`;
}

function renderFilters() {
  const agents = ['All', ...new Set(state.rows.map((row) => row.entry.agent).filter(Boolean))];
  const methods = ['All', ...methodOrder.filter((method) => state.rows.some((row) => row.entry.method === method))];
  document.querySelector('#agent-filters').innerHTML = agents.map((value) => filterButton(value, state.agent, value === 'All' ? state.rows.length : state.rows.filter((row) => row.entry.agent === value).length, 'agent')).join('');
  document.querySelector('#method-filters').innerHTML = methods.map((value) => filterButton(value, state.method, value === 'All' ? state.rows.length : state.rows.filter((row) => row.entry.method === value).length, 'method')).join('');
  document.querySelectorAll('[data-filter-kind]').forEach((button) => button.addEventListener('click', () => {
    state[button.dataset.filterKind] = button.dataset.filterValue;
    renderFilters();
    renderGroups();
  }));
}

function experimentCard(row) {
  if (row.error) return `<article class="experiment-card"><div class="card-heading"><div><p class="card-kicker">${escapeHtml(row.entry.agent || 'Agent')}</p><h4>${escapeHtml(row.entry.method || row.entry.label)}</h4></div><span class="status-mark failed">Unavailable</span></div><p class="error-note">${escapeHtml(row.error)}</p><div class="card-footer"><span>${escapeHtml(row.entry.workspace)}</span></div></article>`;
  const health = row.snapshot.experiment.health;
  const delta = row.seed?.score != null && row.champion?.score != null ? row.champion.score - row.seed.score : null;
  const rejected = row.generations.filter((item) => item.score == null || item.status.includes('rejected') || item.status.includes('failed')).length;
  return `<a class="experiment-card" href="${escapeHtml(row.entry.url)}">
    <div class="card-heading">
      <div><p class="card-kicker">${escapeHtml(row.entry.agent || 'Target agent')} · ${escapeHtml(row.entry.selection_metric || 'Selection score')}</p><h4>${escapeHtml(row.entry.method || row.entry.label)}</h4></div>
      <span class="status-mark ${escapeHtml(health)}">${escapeHtml(health.replaceAll('_', ' '))}</span>
    </div>
    <div class="score-row">
      <div class="score-cell"><span>Seed · G${escapeHtml(row.seed?.genid ?? '—')}</span><strong>${formatScore(row.seed?.score)}</strong></div>
      <span class="score-arrow" aria-hidden="true">→</span>
      <div class="score-cell"><span>Champion · G${escapeHtml(row.champion?.genid ?? '—')}</span><strong>${formatScore(row.champion?.score)}</strong></div>
      <span class="score-delta ${delta < 0 ? 'negative' : ''}">${delta == null ? 'No delta' : signedScore(delta)}</span>
    </div>
    <div class="lineage-box">${lineageChart(row.generations, row.champion)}</div>
    <div class="card-footer"><span>${row.generations.length} generations · ${rejected} rejected/failed</span><span class="open-label">Open evolution record →</span></div>
  </a>`;
}

function visibleRows() {
  const query = state.query.toLowerCase();
  return state.rows.filter((row) => {
    if (state.agent !== 'All' && row.entry.agent !== state.agent) return false;
    if (state.method !== 'All' && row.entry.method !== state.method) return false;
    return !query || [row.entry.label, row.entry.method, row.entry.agent, row.entry.workspace].some((value) => String(value || '').toLowerCase().includes(query));
  });
}

function renderGroups() {
  const rows = visibleRows();
  if (!rows.length) {
    groups.innerHTML = '<section class="empty-panel"><div><strong>No experiments match</strong><p>Change a filter or clear the search field.</p></div></section>';
    return;
  }
  const agents = [...new Set(rows.map((row) => row.entry.agent || 'Experiments'))];
  groups.innerHTML = agents.map((agent) => {
    const agentRows = rows.filter((row) => (row.entry.agent || 'Experiments') === agent).sort((a, b) => methodOrder.indexOf(a.entry.method) - methodOrder.indexOf(b.entry.method));
    return `<section class="agent-group"><div class="group-heading"><h3>${escapeHtml(agent)} target</h3><span>${agentRows.length} experiments</span></div><div class="experiment-grid">${agentRows.map(experimentCard).join('')}</div></section>`;
  }).join('');
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  refreshState.textContent = 'Refreshing local evidence';
  try {
    if (!state.entries.length) {
      const response = await fetch('/api/evolve/catalog', {cache: 'no-store'});
      if (!response.ok) throw new Error(`catalog returned ${response.status}`);
      state.entries = (await response.json()).experiments;
    }
    const snapshotsResponse = await fetch('/api/evolve/catalog/snapshots', {cache: 'no-store'});
    if (!snapshotsResponse.ok) throw new Error(`catalog snapshots returned ${snapshotsResponse.status}`);
    const snapshotRows = (await snapshotsResponse.json()).snapshots;
    const bySlug = new Map(snapshotRows.map((row) => [row.slug, row]));
    state.rows = state.entries.map((entry) => {
      const row = bySlug.get(entry.slug);
      return derive(entry, row?.snapshot || null, row?.error || (row ? null : 'Snapshot missing'));
    });
    document.querySelector('#workspace-count').textContent = `${state.rows.length} workspaces`;
    renderSummary(state.rows);
    renderFilters();
    renderGroups();
    refreshState.textContent = `Updated ${new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'})}`;
  } catch (error) {
    groups.innerHTML = `<section class="empty-panel"><div><strong>Could not read the catalog</strong><p>${escapeHtml(error.message)}</p></div></section>`;
    refreshState.textContent = 'Refresh failed';
  } finally {
    state.refreshing = false;
  }
}

document.querySelector('#catalog-search').addEventListener('input', (event) => {
  state.query = event.currentTarget.value.trim();
  renderGroups();
});

refresh();
window.setInterval(refresh, 60000);
