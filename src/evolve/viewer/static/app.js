import {
  artifactHref,
  artifactPresentation,
  finalResultGeneration,
  generationLineage,
  generationsThrough,
  lineageChart,
  scoreTrend,
  splitDiffFiles,
  snapshotRevision,
  trainScoreChange,
} from './viewer-ui.js';

const state = { snapshot: null, revision: '', timer: null, refreshing: false, artifactCache: new Map() };
const content = document.querySelector('#viewer-content');
const experimentName = document.querySelector('#experiment-name');
const healthPill = document.querySelector('#health-pill');
const refreshStatus = document.querySelector('#refresh-status');
const ROOT_PATH = (document.querySelector('meta[name="evolve-root"]')?.content || '').replace(/\/$/, '');
const catalogReturn = document.querySelector('#catalog-return');
if (catalogReturn && ROOT_PATH.startsWith('/experiments/')) catalogReturn.hidden = false;

const mountedUrl = (value) => {
  const url = String(value || '');
  if (!url.startsWith('/') || !ROOT_PATH || url === ROOT_PATH || url.startsWith(`${ROOT_PATH}/`)) return url;
  return `${ROOT_PATH}${url}`;
};
const currentRoute = () => {
  const path = window.location.pathname;
  if (!ROOT_PATH || !path.startsWith(ROOT_PATH)) return path;
  return path.slice(ROOT_PATH.length) || '/';
};

const escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const label = (value) => String(value ?? 'unknown').replaceAll('_', ' ');
const number = (value, digits = 3) => value == null ? '—' : Number(value).toFixed(digits).replace(/\.?0+$/, '');
const time = (value) => value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'No activity recorded';
const compactTime = (value) => value ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(
  Math.round((new Date(value).getTime() - Date.now()) / 60000), 'minute'
) : 'unknown';
const stageLabel = (value) => ({
  select: 'Select',
  rollout: 'Rollout',
  analyze: 'Analyze',
  mutate: 'Mutate',
  validate: 'Validate',
  novelty: 'Novelty',
  canonical_evaluation: 'Canonical Evaluation',
  gate: 'Gate',
  record: 'Record',
  reflect: 'Reflect',
})[value] || label(value);
const badge = (value) => `<span class="badge ${escapeHtml(value || 'unknown')}">${escapeHtml(label(value))}</span>`;

async function getJson(url) {
  const response = await fetch(mountedUrl(url), { cache: 'no-store' });
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  refreshStatus.textContent = 'Refreshing…';
  try {
    const nextSnapshot = await getJson('/api/evolve/snapshot');
    const nextRevision = snapshotRevision(nextSnapshot);
    const shouldRender = state.snapshot == null || nextRevision !== state.revision;
    const viewState = shouldRender && state.snapshot != null ? captureViewState() : null;
    state.snapshot = nextSnapshot;
    state.revision = nextRevision;
    updateChrome();
    if (shouldRender) {
      await renderRoute(currentRoute(), new URLSearchParams(window.location.search));
      restoreViewState(viewState);
    }
    refreshStatus.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
  } catch (error) {
    refreshStatus.textContent = 'Refresh failed';
    if (!state.snapshot) {
      content.innerHTML = `<div class="error-card"><strong>Could not read this experiment.</strong><p>${escapeHtml(error.message)}</p></div>`;
    }
  } finally {
    state.refreshing = false;
  }
}

function captureViewState() {
  const controls = [...content.querySelectorAll('input[id], select[id], textarea[id]')].map((control) => ({
    id: control.id,
    value: control.value,
    checked: 'checked' in control ? control.checked : null,
  }));
  const scrollers = [...content.querySelectorAll('.trend-scroll, .table-wrap, .artifact-preview')].map((element) => ({
    left: element.scrollLeft,
    top: element.scrollTop,
  }));
  return {
    route: `${window.location.pathname}${window.location.search}`,
    controls,
    scrollers,
    windowX: window.scrollX,
    windowY: window.scrollY,
    focusedId: content.contains(document.activeElement) ? document.activeElement.id : null,
    artifactWrap: document.querySelector('#artifact-preview')?.classList.contains('wrap') || false,
    performancePages: [...content.querySelectorAll('[data-performance-card]')].map((card) => Number(card.dataset.page) || 1),
    championStep: Number(content.querySelector('[data-champion-step]')?.dataset.championStep) || 0,
    diffFile: content.querySelector('[data-diff-file][aria-selected="true"]')?.dataset.diffFile ?? null,
    diffLayout: content.querySelector('[data-diff-layout][aria-pressed="true"]')?.dataset.diffLayout ?? null,
  };
}

function restoreViewState(saved) {
  if (!saved || saved.route !== `${window.location.pathname}${window.location.search}`) return;
  for (const item of saved.controls) {
    const control = document.getElementById(item.id);
    if (!control) continue;
    control.value = item.value;
    if (item.checked != null && 'checked' in control) control.checked = item.checked;
  }
  for (let index = 0; index < saved.championStep; index += 1) {
    content.querySelector('[data-champion-next]')?.click();
  }
  [...content.querySelectorAll('.trend-scroll, .table-wrap, .artifact-preview')].forEach((element, index) => {
    const position = saved.scrollers[index];
    if (!position) return;
    element.scrollLeft = position.left;
    element.scrollTop = position.top;
  });
  if (saved.artifactWrap) {
    const preview = document.querySelector('#artifact-preview');
    const button = document.querySelector('#artifact-wrap');
    preview?.classList.add('wrap');
    preview?.classList.remove('no-wrap');
    button?.setAttribute('aria-pressed', 'true');
    if (button) button.textContent = 'Do not wrap';
  }
  content.querySelectorAll('[data-performance-card]').forEach((card, index) => {
    setPerformancePage(card, saved.performancePages[index] || 1);
  });
  if (saved.diffFile != null) content.querySelector(`[data-diff-file="${saved.diffFile}"]`)?.click();
  if (saved.diffLayout != null) content.querySelector(`[data-diff-layout="${saved.diffLayout}"]`)?.click();
  document.getElementById(saved.focusedId)?.focus({preventScroll: true});
  window.scrollTo(saved.windowX, saved.windowY);
}

function updateChrome() {
  const experiment = state.snapshot.experiment;
  experimentName.textContent = experiment.id;
  experimentName.title = experiment.workspace;
  healthPill.className = `status-pill ${experiment.health}`;
  healthPill.textContent = label(experiment.health);
  document.title = `${experiment.id} · RSIHub`;
}

function activateNavigation(name) {
  document.querySelectorAll('[data-nav]').forEach((link) => {
    link.classList.toggle('active', link.dataset.nav === name);
    if (link.dataset.nav === name) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

async function renderRoute(pathname, params) {
  if (!state.snapshot) return;
  content.classList.remove('diff-mode');
  if (pathname.startsWith('/artifacts/')) {
    activateNavigation(null);
    await renderArtifact(decodeURIComponent(pathname.slice('/artifacts/'.length)), params);
  } else if (pathname === '/trials') {
    activateNavigation('trials');
    await renderTrials(params);
  } else if (pathname === '/generations') {
    activateNavigation('generations');
    renderGenerations();
  } else if (pathname.startsWith('/generations/')) {
    activateNavigation('generations');
    await renderGeneration(decodeURIComponent(pathname.slice('/generations/'.length)));
  } else {
    activateNavigation('overview');
    await renderOverview();
  }
  localizeLinks();
}

function localizeLinks() {
  document.querySelectorAll('a[data-evolve-link]').forEach((link) => {
    const href = link.getAttribute('href');
    if (href?.startsWith('/')) link.setAttribute('href', mountedUrl(href));
  });
}

async function renderOverview() {
  const snapshot = state.snapshot;
  const experiment = snapshot.experiment;
  const finalResult = finalResultGeneration(snapshot.generations);
  const finalResultId = finalResult?.genid || null;
  const championChanges = finalResult ? await loadChampionChanges(finalResult, snapshot.generations) : null;
  const finalDetail = finalResultId ? championChanges?.details.get(finalResultId) : null;
  const recent = snapshot.generations.slice(-6).reverse();
  content.innerHTML = `
    <div class="page-heading">
      <div><h2>Experiment overview</h2><p>Global final result, experiment health, and generation history.</p></div>
      ${finalResultId ? `<div class="page-actions"><a class="button" data-evolve-link href="/generations/${encodeURIComponent(finalResultId)}">Open champion agent · G${escapeHtml(finalResultId)}</a></div>` : ''}
    </div>
    <div class="stack">
      ${healthCard(experiment, finalDetail, true)}
      ${lineageCard(snapshot.generations, finalResult)}
      <div class="grid-two">
        ${championDiffCard(championChanges)}
        ${performanceCard(finalDetail, snapshot.generations, true)}
      </div>
      ${generationTable(recent, 'Recent generations')}
    </div>`;
  bindPerformancePagers();
}

function lineageCard(generations, champion) {
  const rejected = generations.filter((item) => item.score == null || String(item.status).includes('rejected') || String(item.status).includes('failed')).length;
  return `<section class="card evolution-lineage-card">
    <div class="card-header">
      <div><h3>Evolution tree</h3><p>Parent topology and selection score across every generation</p></div>
      <a class="button" href="/generations" data-evolve-link>View all generations</a>
    </div>
    <div class="lineage-box">${lineageChart(generations, champion)}</div>
    <div class="lineage-legend" aria-label="Evolution tree legend">
      <span><i class="lineage-legend-dot accepted"></i>${generations.length - rejected} evaluated</span>
      <span><i class="lineage-legend-dot champion"></i>Champion · G${escapeHtml(champion?.genid ?? '—')}</span>
      <span><i class="lineage-legend-dot rejected"></i>${rejected} rejected or failed</span>
    </div>
  </section>`;
}

async function loadChampionChanges(champion, generations) {
  const lineage = generationLineage(generations, champion.genid);
  const detailEntries = await Promise.all(lineage.map(async (item) => {
    try {
      return [item.genid, await getJson(`/api/evolve/generations/${encodeURIComponent(item.genid)}`)];
    } catch {
      return [item.genid, null];
    }
  }));
  const details = new Map(detailEntries);
  const genesis = lineage[0];
  let files = [];
  let available = false;
  if (genesis && genesis.genid !== champion.genid) {
    const query = new URLSearchParams({context: '8', base: genesis.genid});
    try {
      const response = await fetch(mountedUrl(`/api/evolve/generations/${encodeURIComponent(champion.genid)}/diff?${query}`), {cache: 'no-store'});
      if (response.ok) {
        files = splitDiffFiles(await response.text());
        available = true;
      }
    } catch { /* Keep the overview available when a Git diff cannot be read. */ }
  }
  return {champion, lineage, details, genesis, files, available};
}

function championDiffCard(review) {
  if (!review) {
    return '<section class="card champion-diff-card"><div class="empty"><strong>No champion diff yet</strong>Changes will appear after an eligible generation is evaluated.</div></section>';
  }
  const {champion, lineage, details, genesis, files, available} = review;
  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);
  const firstChange = lineage.slice(1).map((item) => details.get(item.genid)).find((detail) => detail?.change?.patch_artifact_id);
  const diffHref = firstChange
    ? `${artifactHref(firstChange.change.patch_artifact_id)}?champion=${encodeURIComponent(champion.genid)}`
    : null;
  return `<section class="card champion-diff-card">
    <div class="card-header">
      <div><h3>Champion diff</h3><p>Genesis G${escapeHtml(genesis?.genid ?? '—')} → Champion G${escapeHtml(champion.genid)}</p></div>
      <div class="diff-stat"><span>${files.length} files</span><span class="plus">+${additions}</span><span class="minus">−${deletions}</span></div>
    </div>
    <div class="champion-diff-section">
      <div class="champion-diff-label"><strong>Champion files</strong><span>All final target changes</span></div>
      ${files.length ? `<ul class="file-list champion-file-list">${files.map((file) => `<li><span>${escapeHtml(file.path)}</span><small><i class="plus">+${file.additions}</i><i class="minus">−${file.deletions}</i></small></li>`).join('')}</ul>` : `<div class="champion-diff-empty">${available ? 'The champion matches the genesis target.' : 'The cumulative Git diff is unavailable.'}</div>`}
    </div>
    ${diffHref ? `<div class="champion-diff-actions"><a class="button primary" data-evolve-link href="${diffHref}">View diff</a><span>Replay accepted changes generation by generation</span></div>` : ''}
  </section>`;
}

function healthCard(experiment, detail, globalResult = false) {
  const stages = detail?.stages || [];
  const warnings = experiment.warnings || [];
  const sealedScore = detail?.performance?.sealed_score;
  const displayGeneration = globalResult ? detail?.summary?.genid : experiment.focus_generation;
  const displayHealth = globalResult ? detail?.summary?.status || 'unknown' : experiment.health;
  const description = globalResult
    ? 'Global champion from canonical evaluation'
    : experiment.current_stage ? `Current stage: ${stageLabel(experiment.current_stage)}` : time(experiment.last_activity_at);
  return `<section class="card health-card">
    <div class="health-banner">
      <div>
        <span class="status-pill ${escapeHtml(displayHealth)}">${escapeHtml(label(displayHealth))}</span>
        <h2>${displayGeneration ? `${globalResult ? 'Champion agent · ' : ''}Generation ${escapeHtml(displayGeneration)}` : 'Waiting for the first generation'}</h2>
        <p>${escapeHtml(description)}</p>
      </div>
      <div class="health-metrics">
        <div class="metric-big"><strong>${number(experiment.best_score)}</strong><span>Best canonical score</span></div>
        ${globalResult ? `<div class="metric-big"><strong>${number(sealedScore)}</strong><span>Sealed score</span></div>` : ''}
      </div>
    </div>
    ${stages.length ? `<div class="stage-strip" style="--stage-count:${stages.length}" aria-label="Generation stages">${stages.map(stageItem).join('')}</div>` : ''}
    ${warnings.length ? `<ul class="warning-list">${warnings.map((warning) => `<li><strong>${escapeHtml(label(warning.code))}:</strong> ${escapeHtml(warning.message)}</li>`).join('')}</ul>` : ''}
  </section>`;
}

function stageItem(stage) {
  const progress = stage.progress_completed != null
    ? `${stage.progress_completed}${stage.progress_total != null ? ` / ${stage.progress_total}` : ''}` : label(stage.state);
  return `<div class="stage ${escapeHtml(stage.state)}"><strong>${escapeHtml(stageLabel(stage.name))}</strong>${escapeHtml(progress)}</div>`;
}

function changeCard(detail) {
  const change = detail?.change;
  if (!change || (!change.rationale && !change.changed_paths.length)) {
    return `<section class="card"><div class="card-header"><div><h3>Latest modification</h3><p>Why the candidate changed</p></div></div><div class="empty"><strong>No modification evidence</strong>Artifacts will appear after the modify stage.</div></section>`;
  }
  return `<section class="card">
    <div class="card-header"><div><h3>Latest modification</h3><p>Generation ${escapeHtml(detail.summary.genid)} from parent ${escapeHtml(detail.summary.parent || '—')}</p></div><div class="diff-stat"><span class="plus">+${change.insertions}</span><span class="minus">−${change.deletions}</span></div></div>
    <p class="change-rationale">${escapeHtml(change.rationale || 'No rationale was recorded.')}</p>
    <ul class="file-list">${change.changed_paths.slice(0, 8).map((path) => `<li><span>${escapeHtml(path)}</span></li>`).join('')}</ul>
    ${change.patch_artifact_id ? `<p><a class="button primary" data-evolve-link href="${artifactHref(change.patch_artifact_id)}">View diff</a></p>` : ''}
  </section>`;
}

function performanceCard(detail, generations, globalResult = false) {
  const performance = detail?.performance || {};
  const delta = performance.delta;
  const hasTrainScore = performance.train_score_before != null && performance.train_score_after != null;
  const showTrainPage = hasTrainScore && !globalResult;
  const canonicalSubtitle = globalResult
    ? `Global champion · Generation ${escapeHtml(detail?.summary?.genid || '—')}`
    : 'Canonical evaluation only';
  return `<section class="card performance-card" data-performance-card data-page="1">
    <div class="card-header"><div><h3>${globalResult ? 'Final performance' : 'Performance'}</h3><p data-performance-subtitle data-canonical-label="${canonicalSubtitle}">${canonicalSubtitle}</p></div><div class="performance-header-actions">${performance.contract_certified == null ? '' : badge(performance.contract_certified ? 'certified' : 'uncertified')}${showTrainPage ? '<div class="performance-pager" aria-label="Performance pages"><button class="performance-page-button" type="button" data-performance-previous aria-label="Previous performance page" disabled>‹</button><span><strong data-performance-page-number>1</strong> / 2</span><button class="performance-page-button" type="button" data-performance-next aria-label="Next performance page">›</button></div>' : ''}</div></div>
    <div class="performance-pages">
      <div class="performance-page is-active" data-performance-page="1" aria-hidden="false">
        <div class="score-value">${number(performance.score)}${delta == null ? '' : `<span class="score-delta ${delta >= 0 ? 'plus' : 'minus'}">${delta >= 0 ? '+' : ''}${number(delta)}</span>`}</div>
        ${scoreTrend(generations, detail?.summary?.genid)}
        <div class="legend"><span><strong>${performance.observed_trials ?? '—'}</strong> observed trials</span><span><strong>${performance.expected_trials ?? '—'}</strong> expected</span><span>${performance.comparable ? 'Parent delta comparable' : 'Parent delta not comparable'}</span></div>
      </div>
      ${showTrainPage ? `<div class="performance-page" data-performance-page="2" aria-hidden="true">
        ${trainScoreChange(performance.train_score_before, performance.train_score_after, performance.train_delta)}
        <div class="train-score-note"><strong>GEPA validation minibatch</strong><span>This train comparison decides whether the proposal proceeds to canonical evaluation.</span></div>
      </div>` : ''}
    </div>
  </section>`;
}

function setPerformancePage(card, page) {
  const selected = Math.max(1, Math.min(2, Number(page) || 1));
  card.dataset.page = String(selected);
  card.querySelectorAll('[data-performance-page]').forEach((panel) => {
    const active = Number(panel.dataset.performancePage) === selected;
    panel.classList.toggle('is-active', active);
    panel.setAttribute('aria-hidden', String(!active));
  });
  const numberLabel = card.querySelector('[data-performance-page-number]');
  const subtitle = card.querySelector('[data-performance-subtitle]');
  if (numberLabel) numberLabel.textContent = String(selected);
  if (subtitle) subtitle.textContent = selected === 1 ? subtitle.dataset.canonicalLabel : 'GEPA train score change';
  const previous = card.querySelector('[data-performance-previous]');
  const next = card.querySelector('[data-performance-next]');
  if (previous) previous.disabled = selected === 1;
  if (next) next.disabled = selected === 2;
}

function bindPerformancePagers() {
  document.querySelectorAll('[data-performance-card]').forEach((card) => {
    card.querySelector('[data-performance-previous]')?.addEventListener('click', () => setPerformancePage(card, 1));
    card.querySelector('[data-performance-next]')?.addEventListener('click', () => setPerformancePage(card, 2));
  });
}

function renderGenerations() {
  const generations = [...state.snapshot.generations].reverse();
  content.innerHTML = `<div class="page-heading"><div><h2>Generations</h2><p>${generations.length} recorded candidates and baselines.</p></div><div class="page-actions"><a class="button" href="/" data-evolve-link>← Overview</a></div></div>${generationTable(generations, null)}`;
}

function generationTable(generations, title) {
  return `<section class="card">
    ${title ? `<div class="card-header"><div><h3>${escapeHtml(title)}</h3><p>Newest first</p></div><a class="button" href="/generations" data-evolve-link>View all</a></div>` : ''}
    ${generations.length ? `<div class="table-wrap"><table><thead><tr><th>Generation</th><th>Status</th><th>Current stage</th><th class="numeric">Score</th><th class="numeric">Files</th><th class="numeric">Diff</th></tr></thead><tbody>${generations.map((generation) => `<tr>
      <td><a class="row-link" data-evolve-link href="/generations/${encodeURIComponent(generation.genid)}">Generation ${escapeHtml(generation.genid)}</a><div class="subtle">Parent ${escapeHtml(generation.parent || '—')}</div></td>
      <td>${badge(generation.status)}</td><td>${escapeHtml(label(generation.current_stage || 'finished'))}</td><td class="numeric">${number(generation.score)}</td><td class="numeric">${generation.change_files}</td><td class="numeric"><span class="plus">+${generation.insertions}</span> <span class="minus">−${generation.deletions}</span></td>
    </tr>`).join('')}</tbody></table></div>` : '<div class="empty"><strong>No generations yet</strong>The viewer will update when archive rows appear.</div>'}
  </section>`;
}

async function renderGeneration(genid) {
  let detail;
  try { detail = await getJson(`/api/evolve/generations/${encodeURIComponent(genid)}`); }
  catch (error) { content.innerHTML = `<div class="error-card"><strong>Generation not found.</strong><p>${escapeHtml(error.message)}</p><p><a class="button" data-evolve-link href="/generations">← Generations</a></p></div>`; return; }
  const summary = detail.summary;
  content.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">Generation detail</p><h2>Generation ${escapeHtml(summary.genid)}</h2><div class="detail-meta"><span>Status ${badge(summary.status)}</span><span>Parent <strong>${escapeHtml(summary.parent || '—')}</strong></span><span>Score <strong>${number(summary.score)}</strong></span></div></div><div class="page-actions"><a class="button" data-evolve-link href="/generations">← Generations</a><a class="button" data-evolve-link href="/trials?generation=${encodeURIComponent(summary.genid)}">View trials</a></div></div>
    <div class="stack">
      <section class="card"><div class="card-header"><div><h3>Stage progress</h3><p>Evidence inferred from this generation's artifacts</p></div></div><div class="stage-strip" style="--stage-count:${detail.stages.length}">${detail.stages.map(stageItem).join('')}</div></section>
      <div class="grid-two">${changeCard(detail)}${performanceCard(detail, generationsThrough(state.snapshot.generations, summary.genid))}</div>
      ${artifactCard(detail.artifacts)}
    </div>`;
  bindPerformancePagers();
}

function artifactCard(artifacts) {
  return `<section class="card"><div class="card-header"><div><h3>Artifacts</h3><p>Registered stage and evaluation evidence</p></div><span class="muted">${artifacts.length} files</span></div>
    ${artifacts.length ? `<ul class="artifact-list">${artifacts.map((artifact) => `<li><a ${artifact.previewable ? `href="${artifactHref(artifact.id)}" data-evolve-link` : ''}><span>${escapeHtml(artifact.relative_path)}</span><span class="subtle">${formatBytes(artifact.size)}</span></a></li>`).join('')}</ul>` : '<div class="empty">No registered artifacts for this generation.</div>'}
  </section>`;
}

function diffFileTabs(diffFiles) {
  return `<div class="diff-file-tabs" role="tablist" aria-label="Modified files">
    ${diffFiles.map((file, index) => `<button type="button" role="tab" data-diff-file="${index}" aria-selected="${index === 0}" tabindex="${index === 0 ? 0 : -1}">
      <span>${escapeHtml(file.path)}</span><small><i class="plus">+${file.additions}</i><i class="minus">−${file.deletions}</i></small>
    </button>`).join('')}
  </div>`;
}

async function loadArtifact(artifactId) {
  const cached = state.artifactCache.get(artifactId);
  if (cached) return cached;
  const metadata = await getJson(`/api/evolve/artifacts/${encodeURIComponent(artifactId)}/metadata`);
  const response = await fetch(mountedUrl(metadata.content_url), {cache: 'no-store'});
  if (!response.ok) throw new Error(`${metadata.content_url} returned ${response.status}`);
  const loaded = {metadata, text: await response.text()};
  state.artifactCache.set(artifactId, loaded);
  return loaded;
}

async function championArtifactProgression(artifactId, genid, params) {
  const championId = params?.get('champion');
  if (!championId || !genid) return null;
  const champion = finalResultGeneration(state.snapshot.generations);
  if (!champion || champion.genid !== championId) return null;
  const lineage = generationLineage(state.snapshot.generations, championId);
  const details = await Promise.all(lineage.slice(1).map(async (item) => {
    try {
      return await getJson(`/api/evolve/generations/${encodeURIComponent(item.genid)}`);
    } catch {
      return null;
    }
  }));
  if (!details.length || details[0]?.change?.patch_artifact_id !== artifactId || lineage[1]?.genid !== genid) return null;
  const steps = await Promise.all(details.map(async (detail, index) => {
    const summary = lineage[index + 1];
    let text = '';
    try {
      const response = await fetch(mountedUrl(`/api/evolve/generations/${encodeURIComponent(summary.genid)}/diff?context=8`), {cache: 'no-store'});
      if (response.ok) text = await response.text();
    } catch { /* An unavailable step remains visible in the replay. */ }
    return {summary, parent: lineage[index], detail, text, files: splitDiffFiles(text)};
  }));
  const filesByPath = new Map();
  for (const step of steps) {
    for (const path of step.detail?.change?.changed_paths || []) {
      if (!filesByPath.has(path)) filesByPath.set(path, {path, additions: 0, deletions: 0});
    }
    for (const file of step.files) {
      const aggregate = filesByPath.get(file.path) || {path: file.path, additions: 0, deletions: 0};
      aggregate.additions += file.additions;
      aggregate.deletions += file.deletions;
      filesByPath.set(file.path, aggregate);
    }
  }
  return {champion, genesis: lineage[0], steps, files: [...filesByPath.values()]};
}

async function renderArtifact(artifactId, params = new URLSearchParams()) {
  content.innerHTML = '<section class="loading-card" aria-busy="true"><span class="spinner" aria-hidden="true"></span><div><strong>Loading artifact</strong><p>Reading the bounded preview.</p></div></section>';
  let loaded;
  try {
    loaded = await loadArtifact(artifactId);
  } catch (error) {
    content.innerHTML = `<div class="error-card"><strong>Could not read this artifact.</strong><p>${escapeHtml(error.message)}</p><p><a class="button" data-evolve-link href="/">← Overview</a></p></div>`;
    return;
  }
  const {metadata, text} = loaded;
  const generationMatch = metadata.relative_path.match(/^runs\/gen-([^/]+)\//);
  const genid = generationMatch?.[1] || null;
  const generation = genid ? state.snapshot.generations.find((item) => item.genid === genid) : null;
  let presentation = artifactPresentation(metadata, text);
  const isDiff = presentation.mode === 'diff';
  const progression = isDiff ? await championArtifactProgression(artifactId, genid, params) : null;
  if (progression) {
    renderChampionArtifact(progression);
    return;
  }
  const backHref = genid ? `/generations/${encodeURIComponent(genid)}` : '/';
  let expandedContext = null;
  if (isDiff && genid) {
    try {
      const response = await fetch(mountedUrl(`/api/evolve/generations/${encodeURIComponent(genid)}/diff?context=8`), {cache: 'no-store'});
      if (response.ok) {
        const expanded = await response.text();
        if (expanded) {
          presentation = {...presentation, text: expanded};
          expandedContext = 8;
        }
      }
    } catch { /* Fall back to the registered patch artifact. */ }
  }
  const diffFiles = isDiff ? splitDiffFiles(presentation.text) : [];
  const title = isDiff && genid ? `Generation ${genid} diff` : metadata.label;
  content.classList.toggle('diff-mode', isDiff);
  content.innerHTML = `
    <div class="page-heading artifact-heading">
      <div><p class="eyebrow">${isDiff ? 'Generation comparison' : 'Artifact preview'}</p><h2>${escapeHtml(title)}</h2><p class="artifact-path">${escapeHtml(metadata.relative_path)}</p></div>
      <div class="page-actions">
        <a class="button" data-evolve-link href="${backHref}">← ${genid ? `Generation ${escapeHtml(genid)}` : 'Overview'}</a>
        <a class="button" target="_blank" href="${escapeHtml(mountedUrl(metadata.content_url))}">Raw</a>
        ${isDiff ? '' : '<button class="button" id="artifact-wrap" type="button" aria-pressed="false">Wrap lines</button>'}
      </div>
    </div>
    ${metadata.truncated ? '<div class="artifact-notice">Preview limited to the first 1 MiB of this artifact.</div>' : ''}
    <section class="card artifact-card">
      ${isDiff ? `<div class="diff-toolbar">
        <div class="diff-generation-flow">
          <span><small>Original</small><strong>${generation?.parent == null ? 'Parent version' : `Generation ${escapeHtml(generation.parent)}`}</strong></span>
          <b aria-hidden="true">→</b>
          <span><small>Modified</small><strong>Generation ${escapeHtml(genid || '—')}</strong></span>
        </div>
        <div class="diff-summary" aria-label="Diff summary">
          <span><strong>${generation?.change_files ?? '—'}</strong> files</span>
          <span class="plus">+${generation?.insertions ?? '—'}</span>
          <span class="minus">−${generation?.deletions ?? '—'}</span>
          ${expandedContext == null ? '' : `<span>${expandedContext} lines context</span>`}
        </div>
        <div class="diff-toolbar-actions">
          <div class="diff-segmented" aria-label="Diff layout">
            <button class="diff-layout-button" type="button" data-diff-layout="side-by-side" aria-pressed="true">Split</button>
            <button class="diff-layout-button" type="button" data-diff-layout="line-by-line" aria-pressed="false">Unified</button>
          </div>
          <button class="button" id="artifact-wrap" type="button" aria-pressed="false">Wrap lines</button>
        </div>
      </div>` : `<div class="artifact-meta"><span>${escapeHtml(metadata.kind || 'text')}</span><span>${formatBytes(metadata.size)}</span></div>`}
      ${diffFiles.length > 1 ? diffFileTabs(diffFiles) : ''}
      ${isDiff && genid ? `<div id="diff-comparison-labels" class="diff-comparison-labels">
        <div><span>Original</span><strong>${generation?.parent == null ? 'Parent version' : `Generation ${escapeHtml(generation.parent)}`}</strong></div>
        <div><span>Modified</span><strong>Generation ${escapeHtml(genid)}</strong></div>
      </div>` : ''}
      <div id="artifact-preview" class="artifact-preview no-wrap"></div>
    </section>`;

  bindArtifactPreview(presentation, diffFiles, isDiff);
}

function renderChampionArtifact(progression) {
  content.classList.add('diff-mode');
  content.innerHTML = `
    <div class="page-heading artifact-heading">
      <div><p class="eyebrow">Champion replay</p><h2>Champion evolution diff</h2><p class="artifact-path" data-champion-step-label></p></div>
      <div class="page-actions">
        <a class="button" data-evolve-link href="/">← Overview</a>
        <button class="button" type="button" data-champion-previous disabled>← Previous</button>
        <button class="button primary champion-next-button" type="button" data-champion-next>Next →</button>
      </div>
    </div>
    <section class="card artifact-card" data-champion-step="0">
      <div class="diff-toolbar">
        <div class="diff-generation-flow">
          <span><small>Original</small><strong data-step-original></strong></span>
          <b aria-hidden="true">→</b>
          <span><small>Modified</small><strong data-step-modified></strong></span>
        </div>
        <div class="diff-summary" aria-label="Diff summary">
          <span><strong data-step-files>0</strong> files</span>
          <span class="plus" data-step-additions>+0</span>
          <span class="minus" data-step-deletions>−0</span>
          <span>8 lines context</span>
        </div>
        <div class="diff-toolbar-actions">
          <div class="diff-segmented" aria-label="Diff layout">
            <button class="diff-layout-button" type="button" data-diff-layout="side-by-side" aria-pressed="true">Split</button>
            <button class="diff-layout-button" type="button" data-diff-layout="line-by-line" aria-pressed="false">Unified</button>
          </div>
          <button class="button" id="artifact-wrap" type="button" aria-pressed="false">Wrap lines</button>
        </div>
      </div>
      ${diffFileTabs(progression.files)}
      <div id="diff-comparison-labels" class="diff-comparison-labels">
        <div><span>Original</span><strong data-step-original></strong></div>
        <div><span>Modified</span><strong data-step-modified></strong></div>
      </div>
      <div id="artifact-preview" class="artifact-preview no-wrap"></div>
    </section>`;
  bindChampionReplay(progression);
}

function bindChampionReplay(progression) {
  const preview = content.querySelector('#artifact-preview');
  const card = content.querySelector('[data-champion-step]');
  const previous = content.querySelector('[data-champion-previous]');
  const next = content.querySelector('[data-champion-next]');
  const wrapButton = content.querySelector('#artifact-wrap');
  let stepIndex = 0;
  let selectedFile = progression.steps[0]?.files[0]?.path || progression.files[0]?.path || null;
  let selectedLayout = 'side-by-side';
  let filePinned = false;

  const resetScroll = () => {
    preview.scrollLeft = 0;
    preview.scrollTop = 0;
    preview.querySelectorAll('.d2h-file-side-diff').forEach((pane) => { pane.scrollLeft = 0; });
  };
  const renderSelection = () => {
    const step = progression.steps[stepIndex];
    const stepFile = step.files.find((file) => file.path === selectedFile);
    const additions = step.files.reduce((total, file) => total + file.additions, 0);
    const deletions = step.files.reduce((total, file) => total + file.deletions, 0);
    const originalLabel = `Generation ${step.parent.genid}`;
    const modifiedLabel = `Generation ${step.summary.genid}`;
    card.dataset.championStep = String(stepIndex);
    card.dataset.diffLayout = selectedLayout;
    content.querySelector('[data-champion-step-label]').textContent = `Step ${stepIndex + 1} of ${progression.steps.length} · ${originalLabel} → ${modifiedLabel}`;
    content.querySelectorAll('[data-step-original]').forEach((element) => { element.textContent = originalLabel; });
    content.querySelectorAll('[data-step-modified]').forEach((element) => { element.textContent = modifiedLabel; });
    content.querySelector('[data-step-files]').textContent = String(step.files.length);
    content.querySelector('[data-step-additions]').textContent = `+${additions}`;
    content.querySelector('[data-step-deletions]').textContent = `−${deletions}`;
    previous.disabled = stepIndex === 0;
    next.disabled = stepIndex === progression.steps.length - 1;
    next.textContent = next.disabled ? 'Champion reached' : `Next · Generation ${progression.steps[stepIndex + 1].summary.genid} →`;
    preview.replaceChildren();
    preview.classList.remove('diff-preview');
    if (stepFile) {
      renderArtifactPresentation(preview, {mode: 'diff', language: 'diff', text: stepFile.text}, {
        outputFormat: selectedLayout,
        drawFileList: false,
      });
    } else {
      preview.innerHTML = `<div class="champion-no-change"><strong>${escapeHtml(selectedFile || 'This file')}</strong><span>did not change from ${escapeHtml(originalLabel)} to ${escapeHtml(modifiedLabel)}.</span></div>`;
    }
    content.querySelector('#diff-comparison-labels')?.toggleAttribute('hidden', selectedLayout !== 'side-by-side');
    content.querySelectorAll('[data-diff-layout]').forEach((button) => {
      const active = button.dataset.diffLayout === selectedLayout;
      button.classList.toggle('primary', active);
      button.setAttribute('aria-pressed', String(active));
    });
    content.querySelectorAll('[data-diff-file]').forEach((button, index) => {
      const active = progression.files[index]?.path === selectedFile;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
  };
  const selectFile = (index, focus = false, shouldReset = true) => {
    const bounded = Math.max(0, Math.min(progression.files.length - 1, index));
    selectedFile = progression.files[bounded]?.path || null;
    filePinned = true;
    renderSelection();
    if (shouldReset) resetScroll();
    if (focus) content.querySelector(`[data-diff-file="${bounded}"]`)?.focus();
  };
  const selectStep = (index) => {
    stepIndex = Math.max(0, Math.min(progression.steps.length - 1, index));
    if (!filePinned && progression.steps[stepIndex].files[0]) {
      selectedFile = progression.steps[stepIndex].files[0].path;
    }
    renderSelection();
    resetScroll();
  };
  previous.addEventListener('click', () => selectStep(stepIndex - 1));
  next.addEventListener('click', () => selectStep(stepIndex + 1));
  wrapButton.addEventListener('click', () => {
    const wrapping = preview.classList.toggle('wrap');
    preview.classList.toggle('no-wrap', !wrapping);
    wrapButton.setAttribute('aria-pressed', String(wrapping));
    wrapButton.textContent = wrapping ? 'Do not wrap' : 'Wrap lines';
  });
  content.querySelectorAll('[data-diff-layout]').forEach((button) => {
    button.addEventListener('click', () => {
      selectedLayout = button.dataset.diffLayout;
      renderSelection();
    });
  });
  content.querySelectorAll('[data-diff-file]').forEach((button) => {
    button.addEventListener('click', (event) => selectFile(Number(button.dataset.diffFile), false, event.isTrusted));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const current = progression.files.findIndex((file) => file.path === selectedFile);
      const offset = event.key === 'ArrowRight' ? 1 : -1;
      selectFile((current + offset + progression.files.length) % progression.files.length, true);
    });
  });
  renderSelection();
}

function bindArtifactPreview(presentation, diffFiles, isDiff, options = {}) {
  const preview = content.querySelector('#artifact-preview');
  const wrapButton = content.querySelector('#artifact-wrap');
  wrapButton?.addEventListener('click', () => {
    const wrapping = preview.classList.toggle('wrap');
    preview.classList.toggle('no-wrap', !wrapping);
    wrapButton.setAttribute('aria-pressed', String(wrapping));
    wrapButton.textContent = wrapping ? 'Do not wrap' : 'Wrap lines';
  });
  let selectedFile = Math.max(0, diffFiles.findIndex((file) => file.path === options.initialFilePath));
  let selectedLayout = isDiff ? 'side-by-side' : 'line-by-line';
  const renderSelection = () => {
    content.querySelector('.artifact-card').dataset.diffLayout = selectedLayout;
    preview.replaceChildren();
    preview.classList.remove('diff-preview');
    const selectedPresentation = diffFiles.length ? {...presentation, text: diffFiles[selectedFile].text} : presentation;
    renderArtifactPresentation(preview, selectedPresentation, {outputFormat: selectedLayout, drawFileList: false});
    content.querySelector('#diff-comparison-labels')?.toggleAttribute('hidden', selectedLayout !== 'side-by-side');
    content.querySelectorAll('[data-diff-layout]').forEach((button) => {
      const active = button.dataset.diffLayout === selectedLayout;
      button.classList.toggle('primary', active);
      button.setAttribute('aria-pressed', String(active));
    });
    content.querySelectorAll('[data-diff-file]').forEach((button, index) => {
      const active = index === selectedFile;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
  };
  const selectFile = (index, focus = false, resetScroll = true) => {
    selectedFile = Math.max(0, Math.min(diffFiles.length - 1, index));
    renderSelection();
    if (resetScroll) {
      preview.scrollLeft = 0;
      preview.scrollTop = 0;
      preview.querySelectorAll('.d2h-file-side-diff').forEach((pane) => { pane.scrollLeft = 0; });
    }
    if (focus) content.querySelector(`[data-diff-file="${selectedFile}"]`)?.focus();
  };
  content.querySelectorAll('[data-diff-layout]').forEach((button) => {
    button.addEventListener('click', () => {
      selectedLayout = button.dataset.diffLayout;
      renderSelection();
    });
  });
  content.querySelectorAll('[data-diff-file]').forEach((button) => {
    button.addEventListener('click', (event) => selectFile(Number(button.dataset.diffFile), false, event.isTrusted));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const offset = event.key === 'ArrowRight' ? 1 : -1;
      selectFile((selectedFile + offset + diffFiles.length) % diffFiles.length, true);
    });
  });
  renderSelection();
}

function renderArtifactPresentation(container, presentation, options = {}) {
  try {
    if (presentation.mode === 'diff') {
      if (!globalThis.Diff2Html) throw new Error('Diff renderer is unavailable');
      container.classList.add('diff-preview');
      container.innerHTML = globalThis.Diff2Html.html(presentation.text, {
        drawFileList: options.drawFileList ?? true,
        matching: 'none',
        outputFormat: options.outputFormat || 'side-by-side',
        diffMaxChanges: 5000,
      });
      return;
    }
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    if (presentation.mode === 'highlight') {
      if (!globalThis.hljs) throw new Error('Syntax highlighter is unavailable');
      code.className = `language-${presentation.language}`;
      code.innerHTML = globalThis.hljs.highlight(presentation.text, {language: presentation.language}).value;
    } else {
      code.textContent = presentation.text;
    }
    pre.append(code);
    container.append(pre);
  } catch (error) {
    container.classList.remove('diff-preview');
    const warning = document.createElement('div');
    warning.className = 'artifact-render-warning';
    warning.textContent = `${error.message}; showing plain text.`;
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    code.textContent = presentation.text;
    pre.append(code);
    container.replaceChildren(warning, pre);
  }
}

async function renderTrials(params) {
  const apiParams = new URLSearchParams(params);
  if (!apiParams.has('page')) apiParams.set('page', '1');
  if (!apiParams.has('page_size')) apiParams.set('page_size', '50');
  const data = await getJson(`/api/evolve/trials?${apiParams}`);
  const generations = [...state.snapshot.generations].reverse();
  const selectedGeneration = params.get('generation');
  const backHref = selectedGeneration ? `/generations/${encodeURIComponent(selectedGeneration)}` : '/';
  const backLabel = selectedGeneration ? `Generation ${escapeHtml(selectedGeneration)}` : 'Overview';
  content.innerHTML = `
    <div class="page-heading"><div><h2>Trials</h2><p>Canonical outcomes with direct access to full Harbor inspection.</p></div><div class="page-actions"><a class="button" data-evolve-link href="${backHref}">← ${backLabel}</a></div></div>
    <section class="card">
      <form id="trial-filters" class="filters trial-filters">
        <div class="field"><label for="filter-generation">Generation</label><select id="filter-generation" name="generation"><option value="">All generations</option>${generations.map((generation) => `<option value="${escapeHtml(generation.genid)}" ${params.get('generation') === generation.genid ? 'selected' : ''}>${escapeHtml(generation.genid)}</option>`).join('')}</select></div>
        <div class="field"><label for="filter-purpose">Purpose</label><select id="filter-purpose" name="purpose"><option value="">All purposes</option>${['candidate', 'genesis', 'rollout', 'anchor'].map((purpose) => `<option ${params.get('purpose') === purpose ? 'selected' : ''}>${purpose}</option>`).join('')}</select></div>
        <div class="field"><label for="filter-status">Status</label><select id="filter-status" name="status"><option value="">All statuses</option>${['complete', 'benchmark_complete', 'error', 'unknown'].map((status) => `<option ${params.get('status') === status ? 'selected' : ''}>${status}</option>`).join('')}</select></div>
        <div class="field"><label for="filter-task">Exact task</label><input id="filter-task" name="task" value="${escapeHtml(params.get('task') || '')}" placeholder="Task name"></div>
        <div class="filter-action"><button class="button primary" type="submit">Apply</button></div>
      </form>
      ${trialTable(data.items)}
      ${pagination(data, params)}
    </section>`;
  document.querySelector('#trial-filters').addEventListener('submit', applyTrialFilters);
  document.querySelectorAll('[data-page]').forEach((button) => button.addEventListener('click', () => {
    const next = new URLSearchParams(window.location.search); next.set('page', button.dataset.page); navigate(`/trials?${next}`);
  }));
}

function trialTable(trials) {
  if (!trials.length) return '<div class="empty"><strong>No trials match these filters</strong>Clear one or more filters to widen the result.</div>';
  return `<div class="table-wrap trial-table"><table><thead><tr><th>Task</th><th>Generation</th><th>Purpose</th><th>Status</th><th class="numeric">Reward</th><th class="numeric">Duration</th><th>Inspection</th></tr></thead><tbody>${trials.map((trial) => `<tr>
    <td><span class="mono trial-task">${escapeHtml(trial.task)}</span><div class="subtle trial-repetition">Repetition ${trial.repetition}</div></td><td><a class="row-link" data-evolve-link href="/generations/${encodeURIComponent(trial.generation)}">${escapeHtml(trial.generation)}</a></td><td>${escapeHtml(label(trial.purpose))}</td><td>${badge(trial.status)}</td><td class="numeric">${number(trial.reward)}</td><td class="numeric">${trial.duration_ms == null ? '—' : `${number(trial.duration_ms / 1000, 2)}s`}</td><td>${trial.harbor_url ? `<a class="button inspection-button" target="_blank" rel="noopener" href="${escapeHtml(mountedUrl(trial.harbor_url))}">Full Harbor inspection <span aria-hidden="true">↗</span></a>` : '<span class="subtle">Not linked</span>'}</td>
  </tr>`).join('')}</tbody></table></div>`;
}

function pagination(data, params) {
  const first = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const last = Math.min(data.page * data.page_size, data.total);
  return `<div class="pagination"><span>Showing ${first}–${last} of ${data.total}</span><div><button class="button" data-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>Previous</button><button class="button" data-page="${data.page + 1}" ${data.page >= data.total_pages ? 'disabled' : ''}>Next</button></div></div>`;
}

function applyTrialFilters(event) {
  event.preventDefault();
  const values = new FormData(event.currentTarget);
  const params = new URLSearchParams();
  for (const [key, value] of values) if (value) params.set(key, value);
  navigate(`/trials${params.size ? `?${params}` : ''}`);
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function navigate(url) {
  history.pushState({}, '', mountedUrl(url));
  renderRoute(currentRoute(), new URLSearchParams(window.location.search));
  content.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

document.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-evolve-link]');
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || link.origin !== location.origin) return;
  event.preventDefault(); navigate(`${link.pathname}${link.search}`);
});
window.addEventListener('popstate', () => renderRoute(currentRoute(), new URLSearchParams(window.location.search)));

await refresh();
state.timer = window.setInterval(refresh, 3000);
