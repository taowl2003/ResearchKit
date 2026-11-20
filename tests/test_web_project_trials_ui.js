'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');

const html = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8');
const app = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');
const experiments = fs.readFileSync('core/researchkit/web_static/experiments.js', 'utf8');

assert.doesNotMatch(html, /\/trial/, 'the project trial page is not linked by the shell');
const projectsRenderer = app.slice(app.indexOf('function renderProjects'), app.indexOf('async function projects'));
assert.doesNotMatch(projectsRenderer, /openProjectJobs|检查并试运行/, 'the normal project renderer has no project trial control');
assert.match(app, /IDE 实验方案与产物/, 'projects surface IDE schemes and saved results');
assert.match(experiments, /准备 IDE 实验入口/, 'projects without an entry can create the IDE helper only');
assert.match(experiments, /不会改写 case\.py/, 'the UI describes the non-overwrite boundary');

console.log('IDE experiment UI tests passed');
