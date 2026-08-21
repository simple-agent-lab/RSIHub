const escapeSvg = (value) => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

function generationKey(value) {
  const text = String(value);
  const [head, suffix = '0'] = text.split('-', 2);
  const leading = head.match(/^\d+/);
  return [leading ? Number(leading[0]) : -1, /^\d+$/.test(suffix) ? Number(suffix) : 0, text];
}

export function compareGenerationIds(left, right) {
  const a = generationKey(left);
  const b = generationKey(right);
  return Math.sign(a[0] - b[0] || a[1] - b[1] || a[2].localeCompare(b[2]));
}

export function generationsThrough(generations, selectedId) {
  return generations.filter((item) => compareGenerationIds(item.genid, selectedId) <= 0);
}

export function finalResultGeneration(generations) {
  return generations.reduce((best, candidate) => {
    if (candidate.score == null || candidate.selection_eligible === false) return best;
    if (best == null || Number(candidate.score) > Number(best.score)) return candidate;
    return best;
  }, null);
}

export function generationLineage(generations, selectedId) {
  const byId = new Map(generations.map((item) => [String(item.genid), item]));
  const lineage = [];
  const seen = new Set();
  let current = byId.get(String(selectedId));
  while (current && !seen.has(String(current.genid))) {
    lineage.push(current);
    seen.add(String(current.genid));
    current = current.parent == null ? null : byId.get(String(current.parent));
  }
  return lineage.reverse();
}

export function artifactHref(id) {
  return `/artifacts/${encodeURIComponent(id)}`;
}

export function snapshotRevision(snapshot) {
  if (!snapshot) return '';
  const experiment = {...(snapshot.experiment || {})};
  delete experiment.updated_at;
  return JSON.stringify({...snapshot, experiment});
}

export function trainScoreChange(before, after, delta = null) {
  if (before == null || after == null) return '<div class="empty">No GEPA train comparison was recorded.</div>';
  const start = Number(before);
  const end = Number(after);
  const change = delta == null ? end - start : Number(delta);
  const format = (value) => Number(value).toFixed(3).replace(/\.?0+$/, '');
  const tone = change > 0 ? 'plus' : change < 0 ? 'minus' : 'muted';
  const sign = change > 0 ? '+' : '';
  return `<div class="train-score-change" aria-label="GEPA train score changed from ${format(start)} to ${format(end)}">
    <div class="train-score-node"><span>Before</span><strong>${format(start)}</strong></div>
    <div class="train-score-arrow ${tone}"><span aria-hidden="true">→</span><strong>${sign}${format(change)}</strong></div>
    <div class="train-score-node"><span>After</span><strong>${format(end)}</strong></div>
  </div>`;
}

export function scoreAxis(scores) {
  const values = scores.map(Number).filter(Number.isFinite).map((value) => Math.max(0, Math.min(1, value)));
  if (!values.length) return {min: 0, max: 1, ticks: [1, 0.5, 0]};
  const low = Math.min(...values);
  const high = Math.max(...values);
  const padding = Math.max(0.03, (high - low) * 0.2);
  const rawMin = Math.max(0, low - padding);
  const rawMax = Math.min(1, high + padding);
  const targetStep = Math.max(0.01, (rawMax - rawMin) / 5);
  const steps = [0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1];
  const step = steps.find((candidate) => candidate >= targetStep) || 1;
  let min = Math.max(0, Math.floor(rawMin / step) * step);
  let max = Math.min(1, Math.ceil(rawMax / step) * step);
  if (max <= min) {
    min = Math.max(0, min - step);
    max = Math.min(1, max + step);
  }
  min = Number(min.toFixed(4));
  max = Number(max.toFixed(4));
  const ticks = [];
  for (let value = max; value >= min - step / 2; value -= step) ticks.push(Number(value.toFixed(4)));
  return {min, max, ticks};
}

export function artifactPresentation(metadata, text) {
  const kind = String(metadata.kind || '').toLowerCase();
  if (kind === 'diff' || kind === 'patch') return {mode: 'diff', language: 'diff', text};
  if (kind === 'json') {
    try {
      return {mode: 'highlight', language: 'json', text: JSON.stringify(JSON.parse(text), null, 2)};
    } catch {
      return {mode: 'plain', language: 'plaintext', text};
    }
  }
  const language = {
    yaml: 'yaml',
    yml: 'yaml',
    py: 'python',
    sh: 'bash',
    js: 'javascript',
    md: 'markdown',
  }[kind];
  return language
    ? {mode: 'highlight', language, text}
    : {mode: 'plain', language: 'plaintext', text};
}

export function splitDiffFiles(text) {
  const source = String(text || '');
  const starts = [...source.matchAll(/^diff --git /gm)].map((match) => match.index);
  const chunks = starts.length
    ? starts.map((start, index) => source.slice(start, starts[index + 1] ?? source.length))
    : source.trim() ? [source] : [];
  return chunks.map((chunk, index) => {
    const modified = chunk.match(/^\+\+\+ (?:b\/)?(.+)$/m)?.[1];
    const header = chunk.match(/^diff --git (?:"?a\/)?(.+?)"? (?:"?b\/)?(.+?)"?$/m);
    const path = modified && modified !== '/dev/null' ? modified : header?.[2] || header?.[1] || `File ${index + 1}`;
    const lines = chunk.split('\n');
    return {
      path,
      text: chunk,
      additions: lines.filter((line) => line.startsWith('+') && !line.startsWith('+++')).length,
      deletions: lines.filter((line) => line.startsWith('-') && !line.startsWith('---')).length,
    };
  });
}

export function scoreTrend(generations, selectedId = null) {
  const points = generations
    .filter((item) => item.score != null)
    .toSorted((a, b) => compareGenerationIds(a.genid, b.genid));
  if (!points.length) return '<div class="empty">No scored generations yet.</div>';

  const width = 480;
  const height = 180;
  const left = 36;
  const right = 16;
  const top = 14;
  const bottom = 30;
  const axis = scoreAxis(points.map((item) => item.score));
  const generationNumbers = points.map((item) => generationKey(item.genid)[0]);
  const generationMin = Math.min(...generationNumbers);
  const generationMax = Math.max(...generationNumbers);
  const useNumericAxis = generationMin >= 0
    && generationMax > generationMin
    && new Set(generationNumbers).size === generationNumbers.length;
  const rounded = (value) => Math.round(value * 100) / 100;
  const x = (index) => rounded(points.length === 1
    ? (left + width - right) / 2
    : left + (useNumericAxis
      ? (generationNumbers[index] - generationMin) / (generationMax - generationMin)
      : index / (points.length - 1)) * (width - left - right));
  const y = (score) => rounded(top
    + (axis.max - Math.max(axis.min, Math.min(axis.max, Number(score))))
      / (axis.max - axis.min) * (height - top - bottom));
  const ticks = axis.ticks.map((tick) => [tick, y(tick)]);
  const coordinates = points.map((item, index) => `${x(index)},${y(item.score)}`).join(' ');
  const tooltipWidth = 104;
  const tooltipHeight = 30;
  const plotHeight = height - top - bottom;
  const pointXs = points.map((_item, index) => x(index));
  const labelEvery = Math.max(1, Math.ceil(points.length / 10));
  const displayScore = (score) => Number(score).toFixed(3).replace(/\.?0+$/, '');

  return `<div class="trend-scroll"><svg class="trend" viewBox="0 0 ${width} ${height}" role="img" aria-label="Canonical score by generation">
    ${ticks.map(([tick, cy]) => `<line class="trend-grid" x1="${left}" x2="${width - right}" y1="${cy}" y2="${cy}"/><text class="trend-axis-label" x="${left - 7}" y="${cy + 3}">${displayScore(tick)}</text>`).join('')}
    <polyline class="trend-line" points="${coordinates}"/>
    ${points.map((item, index) => {
      const genid = escapeSvg(item.genid);
      const score = escapeSvg(displayScore(item.score));
      const selected = String(item.genid) === String(selectedId) ? ' selected' : '';
      const pointX = x(index);
      const pointY = y(item.score);
      const tooltipX = Math.max(left, Math.min(width - right - tooltipWidth, pointX - tooltipWidth / 2));
      const tooltipY = pointY < top + tooltipHeight + 8 ? pointY + 12 : pointY - tooltipHeight - 10;
      const hitLeft = index === 0 ? left : (pointXs[index - 1] + pointX) / 2;
      const hitRight = index === points.length - 1 ? width - right : (pointX + pointXs[index + 1]) / 2;
      return `<g class="trend-point" tabindex="0" role="img" aria-label="Generation ${genid}: ${score}">
        <rect class="trend-hit" x="${hitLeft}" y="${top}" width="${hitRight - hitLeft}" height="${plotHeight}"/>
        <line class="trend-guide" x1="${pointX}" x2="${pointX}" y1="${top}" y2="${height - bottom}"/>
        <circle class="trend-dot${selected}" cx="${pointX}" cy="${pointY}" r="4"/>
        <g class="trend-tooltip" aria-hidden="true" transform="translate(${tooltipX} ${tooltipY})">
          <rect width="${tooltipWidth}" height="${tooltipHeight}" rx="6"/>
          <text x="${tooltipWidth / 2}" y="19">G${genid}: ${score}</text>
        </g>
        ${index % labelEvery === 0 || index === points.length - 1 ? `<text class="trend-x-label" x="${pointX}" y="${height - 9}">G${genid}</text>` : ''}
      </g>`;
    }).join('')}
  </svg></div>`;
}
