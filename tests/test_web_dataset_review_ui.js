'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const context = {window: {__datasetReviewNoAutoStart: true}};
context.globalThis = context.window;
vm.runInNewContext(fs.readFileSync('tools/dataset-review.js', 'utf8'), context, {filename: 'dataset-review.js'});
const review = context.window.DatasetReview;

function candidate(id, name, state = 'verified') {
  return {candidate_id: id, dataset_id_hint: id + '-v1', relative_root: name, root: '/data/' + name,
    status: 'pending-review', selected: true, metadata: {name, wiki_ref: 'wiki-' + name, wiki_fact_state: state,
      description: 'description-' + name}};
}

const source = {schema: 'researchkit.web-dataset-candidates/v1', source_root: '/data', candidates: [
  candidate('first', 'Rainfall'), candidate('parent', 'Parent', 'parent-card-only'), candidate('third', 'Other'),
]};

function node(tag = '') {
  return {tag, textContent: '', value: '', checked: false, children: [], listeners: {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    fire(name) { this.listeners[name]({target: this}); },
  };
}

const ids = ['name-filter', 'path-filter', 'wiki-filter', 'cards', 'count', 'notice',
  'select-visible', 'clear-selected', 'download'];
const nodes = new Map(ids.map(id => [id, node(id)]));
const document = {getElementById: id => nodes.get(id), createElement: tag => node(tag), createTextNode: text => node('text', text)};
let confirms = 0;
const controller = review.createApp(document, {confirm: () => { confirms += 1; return true; }}, source);

assert.deepEqual(JSON.parse(JSON.stringify(controller.review.candidates.map(item => item.selected))), [false, false, false]);
nodes.get('name-filter').value = 'rain';
nodes.get('name-filter').fire('input');
assert.equal(nodes.get('cards').children.length, 1);
nodes.get('select-visible').fire('click');
assert.equal(controller.review.candidates[0].selected, true);

nodes.get('name-filter').value = '';
nodes.get('wiki-filter').value = 'wiki-parent';
nodes.get('wiki-filter').fire('input');
assert.equal(nodes.get('cards').children.length, 1);
nodes.get('select-visible').fire('click');
assert.equal(controller.review.candidates[1].selected, false);
assert.match(nodes.get('notice').textContent, /逐项确认/);
const parentCheckbox = nodes.get('cards').children[0].children[0];
parentCheckbox.checked = true;
parentCheckbox.fire('change');
assert.equal(confirms, 1);
assert.equal(controller.review.candidates[1].selected, true);

const exported = JSON.parse(controller.exportText());
assert.equal(exported.source_root, source.source_root);
const stableFields = items => JSON.parse(JSON.stringify(items.map(item => ({id: item.candidate_id, root: item.root, metadata: item.metadata}))));
assert.deepEqual(stableFields(exported.candidates), stableFields(source.candidates));
assert.deepEqual(JSON.parse(JSON.stringify(exported.candidates.map(item => item.selected))), [true, true, false]);
