import test from 'node:test';
import assert from 'node:assert/strict';

import {
  artifactHref,
  artifactPresentation,
  compareGenerationIds,
  finalResultGeneration,
  generationLineage,
  generationsThrough,
  scoreAxis,
  scoreTrend,
  splitDiffFiles,
  snapshotRevision,
  trainScoreChange,
} from '../../src/evolve/viewer/static/viewer-ui.js';

test('generation ordering treats 10 as newer than 2', () => {
  assert.equal(compareGenerationIds('2', '10'), -1);
  assert.deepEqual(
    generationsThrough([{genid: '0'}, {genid: '2'}, {genid: '10'}], '2').map((item) => item.genid),
    ['0', '2'],
  );
});

test('overview final result uses the best eligible canonical generation, not the latest attempt', () => {
  const result = finalResultGeneration([
    {genid: '0', score: 0.58, selection_eligible: true},
    {genid: '4', score: 0.68, selection_eligible: true},
    {genid: '9', score: 0.63, selection_eligible: true},
    {genid: '10', score: null, selection_eligible: false},
  ]);

  assert.equal(result.genid, '4');
});

test('champion lineage follows recorded parents rather than generation order', () => {
  const lineage = generationLineage([
    {genid: '0', parent: null},
    {genid: '1', parent: '0'},
    {genid: '2', parent: '0'},
    {genid: '4', parent: '1'},
  ], '4');

  assert.deepEqual(lineage.map((item) => item.genid), ['0', '1', '4']);
});

test('champion lineage stops safely at missing parents and cycles', () => {
  const missing = generationLineage([{genid: '4', parent: '1'}], '4');
  const cycle = generationLineage([{genid: '1', parent: '2'}, {genid: '2', parent: '1'}], '2');

  assert.deepEqual(missing.map((item) => item.genid), ['4']);
  assert.deepEqual(cycle.map((item) => item.genid), ['1', '2']);
});

test('score chart crops its score axis and keeps generation labels', () => {
  const html = scoreTrend([
    {genid: '0', score: 0.32},
    {genid: '1', score: 0.28},
    {genid: '10', score: 0.36},
  ], '10');

  assert.match(html, /Canonical score by generation/);
  assert.match(html, /viewBox="0 0 480 180"/);
  assert.match(html, />0\.4<.*>0\.35<.*>0\.3<.*>0\.25</s);
  assert.doesNotMatch(html, />0</);
  assert.match(html, />G0<.*>G1<.*>G10</s);
  assert.match(html, /Generation 10: 0\.36/);
  assert.match(html, /x1="78\.8" x2="78\.8"/);
  assert.match(html, /trend-dot selected/);
  assert.match(html, /class="trend-point" tabindex="0" role="img"/);
  assert.match(html, /<rect class="trend-hit"[^>]+height="136"/);
  assert.match(html, /<line class="trend-guide"/);
  assert.match(html, /class="trend-tooltip"/);
  assert.match(html, />G10: 0\.36</);
});

test('score axis retains zero only when observed values need it', () => {
  assert.deepEqual(scoreAxis([0.28, 0.32, 0.36]), {min: 0.25, max: 0.4, ticks: [0.4, 0.35, 0.3, 0.25]});
  assert.equal(scoreAxis([0, 0.1]).min, 0);
});

test('artifact presentation prettifies JSON and selects mature diff rendering', () => {
  assert.deepEqual(
    artifactPresentation({kind: 'json', label: 'result.json'}, '{"score":0.3}'),
    {mode: 'highlight', language: 'json', text: '{\n  "score": 0.3\n}'},
  );
  assert.equal(
    artifactPresentation({kind: 'diff', label: 'model_patch.diff'}, 'diff --git a/a b/a\n').mode,
    'diff',
  );
});

test('malformed JSON falls back to plain text mode', () => {
  assert.deepEqual(
    artifactPresentation({kind: 'json', label: 'broken.json'}, '{oops'),
    {mode: 'plain', language: 'plaintext', text: '{oops'},
  );
});

test('diff files are split into independently selectable patches', () => {
  const files = splitDiffFiles([
    'diff --git a/a.py b/a.py',
    '--- a/a.py',
    '+++ b/a.py',
    '@@ -1 +1 @@',
    '-old',
    '+new',
    'diff --git a/b.py b/b.py',
    '--- a/b.py',
    '+++ b/b.py',
    '@@ -0,0 +1,2 @@',
    '+one',
    '+two',
  ].join('\n'));

  assert.deepEqual(files.map(({path, additions, deletions}) => ({path, additions, deletions})), [
    {path: 'a.py', additions: 1, deletions: 1},
    {path: 'b.py', additions: 2, deletions: 0},
  ]);
  assert.doesNotMatch(files[0].text, /b\.py/);
  assert.doesNotMatch(files[1].text, /a\.py/);
});

test('artifact links stay inside the evolve preview', () => {
  assert.equal(artifactHref('abc def'), '/artifacts/abc%20def');
});

test('snapshot revision ignores refresh timestamps but detects experiment changes', () => {
  const first = {experiment: {id: 'run', updated_at: 'first'}, generations: [{genid: '0', score: 0.3}]};
  const refreshed = {experiment: {id: 'run', updated_at: 'second'}, generations: [{genid: '0', score: 0.3}]};
  const changed = {experiment: {id: 'run', updated_at: 'third'}, generations: [{genid: '0', score: 0.4}]};

  assert.equal(snapshotRevision(first), snapshotRevision(refreshed));
  assert.notEqual(snapshotRevision(first), snapshotRevision(changed));
});

test('GEPA train score comparison remains distinct from canonical performance', () => {
  const html = trainScoreChange(29, 34, 5);

  assert.match(html, /Before<\/span><strong>29/);
  assert.match(html, /After<\/span><strong>34/);
  assert.match(html, /\+5/);
  assert.match(html, /GEPA train score changed from 29 to 34/);
});
