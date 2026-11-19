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
eq(data.billing_context?.unit, 'USD', 'default price unit');
eq(data.billing_context?.speed_multipliers?.priority, 2.5, 'subscription Fast multiplier');
eq(data.billing_context?.usd_equivalent?.credits_per_usd, 25, 'dollar normalization');
ok(!/<th[^>]*>[^<]*(?:credits|API|[／/]答)[^<]*<\/th>/i.test(html), 'table must show only dollar price without an answer suffix');
const priceHead = html.match(/<table id="costTable">[\s\S]*?<\/thead>/)?.[0] || '';
eq((priceHead.match(/<th>/g) || []).length, 3, 'single final price column plus identity and coverage');
ok(priceHead.includes('<th>估算费用（美元）</th>'), 'price heading');
for (const sample of data.samples || []) {
  if (typeof sample.estimated_subscription_credits === 'number')
    eq(sample.estimated_subscription_usd, sample.estimated_subscription_credits / 25, 'sample dollar amount must be converted');
  else eq(sample.estimated_subscription_usd, null, 'unknown credits cannot become a dollar price');
}
const executable = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)]
  .filter(m => !m[1].includes('application/json'));
eq(executable.length, 1);
ok(!/<script[^>]+src\s*=/i.test(html), 'external executable resource');
ok(!/<(?:img|iframe|link)[^>]+(?:src|href)\s*=\s*["']https?:/i.test(html), 'external rendering resource');
ok(!/\bon(?:click|error|load)\s*=/i.test(html), 'inline event handler');
ok(html.includes('@media(max-width:560px)') && html.includes('overflow:auto'), 'responsive fallback missing');
for (const name of ['compact.html', 'comparison-data.json', 'grading-details.json', 'conditions.csv', 'samples.csv', 'fast-observations.csv',
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
// Build the expected display groups independently from the page implementation.
const topics = new Map();
for (const group of data.cases || []) {
  const key = JSON.stringify([group.case_id, group.rubric_sha256, group.scoring_version, group.execution_mode]);
  if (!topics.has(key)) topics.set(key, { ...group, id: `topic-${topics.size + 1}`, conditions: [], member_case_ids: [], sources: [] });
  const topic = topics.get(key);
  eq(topic.dimension_maxima || topic.weights, group.dimension_maxima || group.weights, 'topic score scale mismatch');
  topic.conditions.push(...(group.conditions || []));
  topic.member_case_ids.push(group.id);
  for (const source of group.sources || []) if (!topic.sources.some(s => s.id === source.id)) topic.sources.push(source);
}
const cases = [...topics.values()];
eq(vm.runInContext('cases.length', ctx), cases.length, 'all matching profiles must share the topic view');
eq((node('#caseSelect').innerHTML.match(/<option /g) || []).length, cases.length, 'topic selector options');
const topicLabels = cases.map((_, index) => vm.runInContext(`topicLabel(cases[${index}])`, ctx));
eq(new Set(topicLabels).size, cases.length, 'topic selector labels must distinguish different rubrics');
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
  for (const table of ['qualityTable tbody', 'performanceTable tbody', 'costTable tbody', 'agenticTable tbody']) {
    eq(countRows(node('#' + table).innerHTML), groups.length, table + ' filter count');
  }
  if (data.final_tps_estimation) {
    eq(node('#proxyTpsSection').hidden, false, 'proxy section visible');
    ok(node('#proxyTpsNote').textContent.includes('映射未核实'), 'proxy mapping disclosure missing');
    ok(node('#proxyTpsNote').textContent.includes('不是原生正文 token 用量'), 'proxy attribution disclosure missing');
    eq(countRows(node('#proxyTpsTable tbody').innerHTML), groups.length, 'proxy table filter count');
    const available = groups.filter(g => finite(g.statistics?.estimated_final_text_tps?.median));
    eq((node('#proxyTpsChart').innerHTML.match(/class="center"/g) || []).length, available.length, 'proxy chart availability');
    for (const g of groups) {
      const expected = `${g.statistics?.estimated_final_text_tps?.n ?? 0} / ${g.valid_completed ?? 0}`;
      ok(node('#proxyTpsTable tbody').innerHTML.includes(expected), 'proxy n missing');
    }
  } else {
    eq(node('#proxyTpsSection').hidden, true, 'proxy section hidden without opt-in');
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
    ['costChart', 'estimated_subscription_usd', 'mean'], ['answerReadyChart', 'answer_ready_s', 'median'],
    ['finalTtftChart', 'final_ttft_s', 'median'], ['progressChart', 'first_progress_s', 'median'],
    ['rootThroughputChart', 'root_output_e2e_tps', 'median'], ['agentCountChart', 'delegation_count', 'median']
  ]) {
    const markup = node('#' + chart).innerHTML;
    const available = groups.filter(g => finite(g.statistics?.[key]?.[center]));
    ok(!/(?:NaN|Infinity|undefined)(?:[<" ]|$)/.test(markup), chart + ' non-finite output');
    eq((markup.match(/<svg /g) || []).length, available.length ? 1 : 0, chart + ' availability');
    if (!available.length) continue;
    if (chart === 'costChart') ok(markup.includes('$'), 'dollar prefix missing from price chart');
    ok(markup.includes(center === 'mean' ? '均值' : '中位数'), chart + ' statistic label');
    eq((markup.match(/class="center"/g) || []).length, available.length, chart + ' center dots');
    eq((markup.match(/class="sample"/g) || []).length,
      available.reduce((sum, g) => sum + (g.rows || []).filter(r => finite(r[key])).length, 0),
      chart + ' raw dots');
    ok(markup.includes('viewBox="0 0 '), chart + ' scalable SVG');
  }
  const conditionIds = new Set(groups.map(g => g.id));
  for (const g of groups) {
    const amount = g.statistics?.estimated_subscription_usd?.mean;
    if (finite(amount)) ok(node('#costTable tbody').innerHTML.includes('$' + amount.toFixed(4)), 'dollar amount missing from price table');
  }
  const pairs = (data.paired_speed_observations || []).filter(p => c.member_case_ids.includes(p.case) &&
    conditionIds.has(p.standard_condition) && conditionIds.has(p.fast_condition));
  eq(countRows(node('#fastTable').innerHTML), pairs.length ? pairs.length + 1 : 0, 'explicit Fast pairs only');
  if (pairs.some(p => p.kind === 'historical-observation'))
    ok(node('#fastTable').innerHTML.includes('跨批历史观察'), 'cross-batch warning missing');
  if (pairs.some(p => p.kind === 'same-batch'))
    ok(node('#fastTable').innerHTML.includes('同批观察'), 'same-batch label missing');
}
const compact = JSON.parse(fs.readFileSync(path.join(root, 'compact-data.json'), 'utf8'));
const compactHtml = fs.readFileSync(path.join(root, 'compact.html'), 'utf8');
const sourceConditions = new Map((data.cases || []).flatMap(c => (c.conditions || []).map(g => [g.id, g])));
eq(compact.condition_count, sourceConditions.size, 'compact condition count');
eq(compact.topic_count, cases.length, 'compact topic count');
eq(new Set(compact.rows.map(r => r.condition_id)).size, sourceConditions.size, 'compact duplicate or missing condition');
eq((compactHtml.match(/data-condition=/g) || []).length, sourceConditions.size, 'compact HTML row count');
ok(compactHtml.includes('href="report.html"'), 'compact full-report link');
ok(!/<script\b/i.test(compactHtml), 'compact static view must not execute scripts');
ok(compactHtml.includes('<th scope="col">估算费用（美元）</th>'), 'compact dollar heading');
ok(!/<th[^>]*>[^<]*(?:credits|API|[／/]答)[^<]*<\/th>/i.test(compactHtml), 'compact second price or answer suffix');
for (const row of compact.rows) {
  const g = sourceConditions.get(row.condition_id);
  ok(g, 'unknown compact condition');
  eq(row.case, g.case, 'compact original case identity');
  eq(row.execution_profile_id, g.execution_profile_id, 'compact original profile');
  eq(row.valid_n, g.valid_completed, 'compact actual n');
  eq(row.planned_n, g.planned, 'compact planned n');
  eq(row.billing_unit, 'USD', 'compact currency');
  if (finite(row.estimated_subscription_usd_mean))
    ok(compactHtml.includes('$' + row.estimated_subscription_usd_mean.toFixed(4)), 'compact dollar prefix or amount');
  for (const [key, expected] of Object.entries(g.statistics || {})) for (const field of ['n', 'mean', 'median', 'min', 'max']) {
    if (Object.hasOwn(row, `${key}_${field}`)) eq(row[`${key}_${field}`], expected[field], 'compact metric differs');
  }
  ok(compactHtml.includes(`data-condition="${escaped(row.condition_id)}"`), 'compact identity absent from HTML');
}
for (const [selector, item] of nodes) {
  ok(!/javascript\s*:|<script[^>]*>/.test(item.innerHTML), 'unsafe generated markup in ' + selector);
}
console.log(JSON.stringify({ status: 'passed', counted_assertions: checks,
  filter_combinations: combinations, display_topics: cases.length, compact_conditions: compact.condition_count,
  offline_stub_dom: true, real_browser: false,
  source_data_deep_equal: true,
  limits: ['Runs page JavaScript with a minimal DOM and checks generated markup; does not verify browser CSS layout, rendering or assistive technology.'] }));
