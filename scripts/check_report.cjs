// Offline script/DOM regression check; does not open a browser or use a network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const page = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...page.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
assert.equal(scripts.length, 2, 'Expected JSON data plus one trusted interaction script');
assert.match(scripts[0][1], /application\/json/);
const data = JSON.parse(scripts[0][2]);
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.innerHTML = ''; this.textContent = ''; this.index = -1; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  set selectedIndex(i) { this.index = i; }
  get selectedIndex() { return this.index; }
  get value() { return this.tag === 'select' ? String(this.children[this.index]?.value ?? '') : this._value; }
  set value(v) { this._value = v; }
}
const ids = {};
const filterIds = ['model-filter', 'effort-filter', 'tier-filter'];
const tiered = page.includes('id="tier-filter"');
for (const id of ['data', 'left', 'right', 'left-answer', 'right-answer', 'chart', ...(tiered ? filterIds : [])]) ids[id] = new Element(['left', 'right', ...filterIds].includes(id) ? 'select' : 'div');
ids.data.textContent = scripts[0][2];
const document = {getElementById(id) {assert.ok(ids[id], 'Missing expected element'); return ids[id];}, createElement(tag) {return new Element(tag);}, createElementNS(ns, tag) {return new Element(tag);}};
new vm.Script(scripts[1][2], {filename: 'benchmark-report-interactions.js'}).runInNewContext({document});
assert.equal(ids.left.children.length, data.answers.length);
assert.equal(ids.right.children.length, data.answers.length);
if (data.answers.length) {
  assert.equal(ids['left-answer'].innerHTML, data.answers[0].html || '无答案');
  ids.left.selectedIndex = data.answers.length - 1;
  ids.left.listeners.change();
  assert.equal(ids['left-answer'].innerHTML, data.answers.at(-1).html || '无答案');
  assert.equal(ids['right-answer'].innerHTML, data.answers[Math.min(1, data.answers.length - 1)].html || '无答案');
}
if (tiered) {
  const controls = [['model-filter', 'model'], ['effort-filter', 'effort'], ['tier-filter', 'service_tier']];
  function assertFiltered() {
    const matching = data.answers.filter(a => controls.every(([id, key]) => !ids[id].value || a[key] === ids[id].value));
    for (const side of ['left', 'right']) {
      assert.equal(ids[side].children.length, matching.length);
      matching.forEach((a, i) => assert.equal(ids[side].children[i].textContent, a.label));
      const index = side === 'right' && matching.length > 1 ? 1 : 0;
      assert.equal(ids[side + '-answer'].innerHTML, matching[index]?.html || '无匹配答案');
    }
  }
  for (const [id, key] of controls) {
    assert.equal(ids[id].children.length, new Set(data.answers.map(a => a[key])).size + 1);
    ids[id].selectedIndex = ids[id].children.length - 1;
    ids[id].listeners.change();
    assertFiltered();
    ids[id].selectedIndex = 0;
    ids[id].listeners.change();
  }
  for (const [id] of controls) ids[id].selectedIndex = ids[id].children.length - 1;
  ids['tier-filter'].listeners.change();
  assertFiltered();
  for (const [id] of controls) ids[id].selectedIndex = 0;
  ids['tier-filter'].listeners.change();
  assertFiltered();
}
assert.equal(ids.chart.children.length, 1);
const svg = ids.chart.children[0];
assert.equal(svg.attrs.role, 'img');
assert.equal(svg.children.filter(e => e.tag === 'circle').length, data.cells.filter(c => c.statistics.elapsed_s.n).length);
if (tiered) for (const c of data.cells) assert.ok(svg.children.some(e => e.tag === 'text' && e.textContent === c.model + '/' + c.effort + '/' + c.speed_mode));
assert.match(page, /@media\(max-width:850px\)/);
assert.match(page, /<meta name="viewport"/);
console.log(JSON.stringify({passed: true, answers: data.answers.length, conditions: data.cells.length, answer_selection: 'passed', ...(tiered ? {answer_filters: 'passed', tier_labels: 'passed'} : {}), chart_structure: 'passed', syntax: 'passed', browser_visual_check: 'not performed; offline DOM simulation only', network_requests: 0}));
