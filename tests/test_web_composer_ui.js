'use strict';

// This uses Node's built-in vm only.  It checks the form-state codecs without
// requiring a browser or an additional DOM package.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
  return {textContent: '', onclick: null, querySelector: () => null, setAttribute() {}, remove() {}};
}

function load(fetch) {
  const nodes = new Map([['#composer-body', element()], ['#composer-message', element()], ['#composer-project', element()], ['#composer-back', element()]]);
  const window = {__researchkitComposerTest: true};
  const context = {
    window,
    document: {querySelector: selector => nodes.get(selector), dispatchEvent() {}},
    sessionStorage: {getItem: () => null},
    Headers: class { set() {} },
    fetch,
    confirm: () => true,
    CustomEvent: class {},
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/composer.js', 'utf8'), context, {filename: 'composer.js'});
  return window.__researchkitComposerTest;
}

const catalog = {plugins: [{ref: 'example.scale@1.0.0', parameters: {
  factor: {type: 'number', required: true}, requiredDefault: {type: 'number', required: true, default: 4}, optional: {type: 'string'}, mapping: {type: 'mapping'}, sequence: {type: 'sequence'}, enabled: {type: 'boolean'},
}, inputs: {value: 'scalar@1.0.0'}, outputs: {value: 'scalar@1.0.0'}}], contracts: []};
const hooks = load(() => Promise.reject(new Error('fetch should not run')));
hooks.state.catalog = catalog;
hooks.normalise({project_id: 'one', revision: 'a'.repeat(64), parameters: JSON.parse('{"zero":0,"enabled":false,"empty":"","object":{"a":1},"array":[1,2],"__proto__":"safe"}'), steps: [{alias: 'scale', ref: 'example.scale@1.0.0', parameters: {factor: 0, optional: '', mapping: {a: 1}, sequence: [1, 2], enabled: false}, inputs: {value: {kind: 'literal', value: {nested: [false, 0, '']}}}}], draft_contracts: [], entry: 'case.py', entry_text: '', managed: true});
const roundTrip = hooks.payload();
assert.deepEqual(JSON.parse(JSON.stringify(roundTrip.parameters)), JSON.parse('{"zero":0,"enabled":false,"empty":"","object":{"a":1},"array":[1,2],"__proto__":"safe"}'));
assert.deepEqual(JSON.parse(JSON.stringify(roundTrip.steps[0].parameters)), {factor: 0, optional: '', mapping: {a: 1}, sequence: [1, 2], enabled: false});
assert.deepEqual(JSON.parse(JSON.stringify(roundTrip.steps[0].inputs.value.value)), {nested: [false, 0, '']});
assert.equal(Object.getPrototypeOf(roundTrip.parameters), null);
assert.equal(Object.hasOwn(roundTrip.steps[0].parameters, 'requiredDefault'), false);
hooks.normalise({project_id: 'one', revision: 'a'.repeat(64), parameters: roundTrip.parameters, steps: roundTrip.steps, draft_contracts: [], entry: 'case.py', entry_text: '', managed: true});
assert.deepEqual(JSON.parse(JSON.stringify(hooks.payload().steps[0].inputs.value.value)), {nested: [false, 0, '']});
hooks.state.preview = {changed_paths: ['project.yaml']}; hooks.markDirty(); assert.equal(hooks.state.preview, null);

hooks.normalise({project_id: 'two', revision: 'b'.repeat(64), parameters: {}, steps: [{alias: 'scale', ref: 'example.scale@1.0.0', parameters: {factor: ''}, inputs: {value: {kind: 'literal', value: 0}}}], draft_contracts: [], entry: 'case.py', entry_text: '', managed: true});
assert.throws(() => hooks.payload(), /不能为空/);

hooks.normalise({project_id: 'three', revision: 'c'.repeat(64), parameters: {}, steps: [{alias: 'scale', ref: 'example.scale@1.0.0', parameters: {factor: 1}, inputs: {value: {kind: 'literal', value: false}}}], draft_contracts: [], entry: 'case.py', entry_text: '', managed: true});
assert.equal(Object.hasOwn(hooks.payload().steps[0].parameters, 'optional'), false);
assert.equal(Object.hasOwn(hooks.payload().steps[0].parameters, 'enabled'), false);

let resolveCatalog, resolveProject;
const stale = load(() => new Promise(resolve => { if (!resolveCatalog) resolveCatalog = resolve; else resolveProject = resolve; }));
stale.state.projectId = 'old';
const opening = stale.open('new');
stale.state.epoch += 1;
resolveCatalog({ok: true, json: async () => catalog});
resolveProject({ok: true, json: async () => ({project_id: 'new', revision: 'd'.repeat(64), parameters: {}, steps: [], draft_contracts: [], entry: 'case.py', entry_text: '', managed: false})});
opening.then(async () => {
  assert.equal(stale.state.projectId, 'old');
  const deferred = [];
  const racing = load(() => new Promise(resolve => deferred.push(resolve)));
  racing.state.catalog = catalog;
  const record = {project_id: 'race', revision: 'e'.repeat(64), parameters: {}, steps: [{alias: 'scale', ref: 'example.scale@1.0.0', parameters: {factor: 1}, inputs: {value: {kind: 'literal', value: 0}}}], draft_contracts: [], entry: 'case.py', entry_text: '', managed: true};
  racing.state.projectId = 'race'; racing.normalise(record);
  const preview = racing.previewProject();
  racing.state.epoch += 1; racing.state.projectId = 'changed-after-preview';
  deferred.shift()({ok: true, json: async () => ({project_yaml: 'x', entry_text: null, changed_paths: [], revision: record.revision})});
  await preview;
  assert.equal(racing.state.preview, null);
  racing.state.projectId = 'race'; racing.normalise(record);
  const savePayload = racing.payload();
  racing.state.preview = {fingerprint: JSON.stringify(savePayload)};
  const saving = racing.saveProject();
  racing.state.epoch += 1; racing.state.projectId = 'changed-after-save';
  deferred.shift()({ok: true, json: async () => ({...record, project_id: 'race'})});
  await saving;
  assert.equal(racing.state.projectId, 'changed-after-save');
}).catch(error => { console.error(error); process.exitCode = 1; });
