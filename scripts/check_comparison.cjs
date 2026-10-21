/* Offline execution and generated-markup checks. This deliberately uses a
 * minimal DOM stub: it is not a browser, CSS layout engine or visual QA. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(process.argv[2] || '.');
const html = fs.readFileSync(path.join(root, 'report.html'), 'utf8');
const data = JSON.parse(fs.readFileSync(path.join(root, 'comparison-data.json'), 'utf8'));
let checks = 0;
const ok = (value, message) => { assert(value, message); checks++; };
const eq = (a, b, message) => { assert.deepEqual(a, b, message); checks++; };
const match = html.match(/<script[^>]*id="comparison-data"[^>]*>([\s\S]*?)<\/script>/);
ok(match, 'embedded data missing');
eq(JSON.parse(match[1]), data, 'embedded data differs from export');
const executable = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)]
  .filter(m => !m[1].includes('application/json'));
eq(executable.length, 1);
ok(!/<script[^>]+src\s*=/i.test(html), 'external executable resource');
ok(!/<(?:img|iframe|link)[^>]+(?:src|href)\s*=\s*["']https?:/i.test(html), 'external rendering resource');
ok(!/\bon(?:click|error|load)\s*=/i.test(html), 'inline event handler');
ok(html.includes('@media(max-width:560px)') && html.includes('overflow:auto'), 'responsive fallback missing');
for (const name of ['comparison-data.json', 'grading-details.json', 'conditions.csv', 'samples.csv', 'fast-observations.csv',
                    'manifest.json', 'validation.json', 'comparison.zip']) {
  ok(html.includes('href="' + name + '"'), 'missing download: ' + name);
}
const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const nodes = new Map();
function node(selector) {
  const id = selector.replace(/^#/, '').split(' ')[0];
  assert(ids.has(id), 'unexpected DOM selector: ' + selector);
  if (!nodes.has(selector)) nodes.set(selector, {
    value: '', textContent: '', innerHTML: '', addEventListener() {}
  });
  return nodes.get(selector);
}
node('#comparison-data').textContent = match[1];
node('#sortSelect').value = 'configuration';
const document = {
  title: '',
  getElementById(id) { return node('#' + id); },
  querySelector(selector) { return node(selector); }
};
const ctx = vm.createContext({ document });
vm.runInContext(executable[0][2], ctx, { timeout: 10000 });
for (const unsafe of ['javascript:alert(1)', '  javascript:alert(1)', 'java\nscript:alert(1)', 'data:text/html,hello']) {
  eq(vm.runInContext('safeURL(' + JSON.stringify(unsafe) + ')', ctx), '#', 'unsafe link scheme');
}
eq(vm.runInContext('safeURL("answers/a.txt")', ctx), 'answers/a.txt');
const escaped = value => vm.runInContext('esc(' + JSON.stringify(value) + ')', ctx);
for (const limit of [...(data.limitations || []), ...(data.assessment?.limitations || [])]) {
  ok(node('#limitations').innerHTML.includes(escaped(limit)), 'source or assessment limitation missing');
}
for (const source of data.source_batches || []) {
  const sourceMarkup = node('#sources').innerHTML;
  ok(sourceMarkup.includes(escaped(source.price_as_of || '未知')), 'historical price date missing');
  if (source.evaluation?.mode === 'offline-regrade') {
    ok(sourceMarkup.includes('离线重评'), 'regrade disclosure missing');
    ok(sourceMarkup.includes('事后细则：' + (source.evaluation.post_hoc === true ? '是' : source.evaluation.post_hoc === false ? '否' : '未知')));
    ok(sourceMarkup.includes('评分者此前见过模型身份：' + (source.evaluation.evaluator_previously_saw_model_identity === true ? '是' : source.evaluation.evaluator_previously_saw_model_identity === false ? '否' : '未知')));
    if (source.evaluation.note) ok(sourceMarkup.includes(escaped(source.evaluation.note)), 'regrade note missing');
  }
  if (source.id) for (const name of ['input/prompt.txt',
    source.evaluation?.mode === 'offline-regrade' ? 'evaluation/rubric.json' : 'input/gold.json',
    'grading/scores.json', 'prices.json', 'summary.json']) {
    ok(sourceMarkup.includes('href="sources/' + encodeURIComponent(source.id) + '/' + name + '"'), 'source review link missing');
    ok(fs.existsSync(path.join(root, 'sources', source.id, name)), 'source review target missing');
  }
}
const finite = x => typeof x === 'number' && Number.isFinite(x);
const cases = data.cases || [];
const dimensions = ['accuracy', 'coverage', 'readability', 'insight'];
const countRows = markup => (markup.match(/<tr>/g) || []).length;
eq(node('#caseSelect').value, cases[0]?.id || '');
eq(node('#effortSelect').value, 'all');
const efforts = ['all', ...new Set(cases.flatMap(c => c.conditions || []).map(g => g.effort))];
let combinations = 0;
for (const c of cases) for (const effort of efforts) for (const sort of ['configuration', 'elapsed']) {
  node('#caseSelect').value = c.id;
  node('#effortSelect').value = effort;
  node('#sortSelect').value = sort;
  vm.runInContext('renderCase()', ctx, { timeout: 10000 });
  combinations++;
  const groups = (c.conditions || []).filter(g => effort === 'all' || g.effort === effort);
  for (const table of ['qualityTable tbody', 'performanceTable tbody', 'costTable tbody']) {
    eq(countRows(node('#' + table).innerHTML), groups.length, table + ' filter count');
  }
  eq(countRows(node('#crossCaseTable tbody').innerHTML), cases.reduce((sum, each) => sum +
    (each.conditions || []).filter(g => effort === 'all' || g.effort === effort).length, 0));
  const maxima = c.dimension_maxima || c.weights || {};
  for (const key of dimensions) {
    if (!finite(maxima[key])) ok(node('#qualityHead').innerHTML.includes('未知'), 'missing dimension must be unknown');
    else ok(node('#qualityHead').innerHTML.includes('/ ' + maxima[key]), 'original scale missing');
  }
  for (const [chart, key, center] of [
    ['elapsedChart', 'elapsed_s', 'median'], ['ttftChart', 'ttft_visible_s', 'median'],
    ['tpsChart', 'visible_decode_tps', 'median'], ['outputChart', 'output_tokens', 'median'],
    ['costChart', 'estimated_api_usd', 'mean']
  ]) {
    const markup = node('#' + chart).innerHTML;
    const available = groups.filter(g => finite(g.statistics?.[key]?.[center]));
    ok(!/(?:NaN|Infinity|undefined)(?:[<" ]|$)/.test(markup), chart + ' non-finite output');
    eq((markup.match(/<svg /g) || []).length, available.length ? 1 : 0, chart + ' availability');
    if (!available.length) continue;
    ok(markup.includes(center === 'mean' ? '均值' : '中位数'), chart + ' statistic label');
    eq((markup.match(/class="center"/g) || []).length, available.length, chart + ' center dots');
    eq((markup.match(/class="sample"/g) || []).length,
      available.reduce((sum, g) => sum + (g.rows || []).filter(r => finite(r[key])).length, 0),
      chart + ' raw dots');
    ok(markup.includes('viewBox="0 0 '), chart + ' scalable SVG');
  }
  const conditionIds = new Set(groups.map(g => g.id));
  const pairs = (data.paired_speed_observations || []).filter(p => p.case === c.id &&
    conditionIds.has(p.standard_condition) && conditionIds.has(p.fast_condition));
  eq(countRows(node('#fastTable').innerHTML), pairs.length ? pairs.length + 1 : 0, 'explicit Fast pairs only');
  if (pairs.some(p => p.kind === 'historical-observation'))
    ok(node('#fastTable').innerHTML.includes('跨批历史观察'), 'cross-batch warning missing');
  if (pairs.some(p => p.kind === 'same-batch'))
    ok(node('#fastTable').innerHTML.includes('同批观察'), 'same-batch label missing');
}
for (const [selector, item] of nodes) {
  ok(!/javascript\s*:|<script[^>]*>/.test(item.innerHTML), 'unsafe generated markup in ' + selector);
}
console.log(JSON.stringify({ status: 'passed', counted_assertions: checks,
  filter_combinations: combinations, offline_stub_dom: true, real_browser: false,
  source_data_deep_equal: true,
  limits: ['Runs page JavaScript with a minimal DOM and checks generated markup; does not verify browser CSS layout, rendering or assistive technology.'] }));
