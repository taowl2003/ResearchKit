'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');

const html = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8');
const app = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');

assert.doesNotMatch(html, /id="jobs"/, 'the retired Web Jobs page has no DOM route');
assert.doesNotMatch(html, /project-trial-controls/, 'project trial controls are removed from the shell');
assert.doesNotMatch(app, /await executionStatus\(\)/, 'startup never asks for a Web runner');
assert.doesNotMatch(app, /button\('检查与运行'/, 'projects do not expose a Web execution button');
assert.doesNotMatch(app, /button\('执行记录'/, 'fixed submissions do not expose legacy Jobs');
assert.doesNotMatch(app, /host\.append\(publication\);\s*reviewEvidence/, 'teacher review does not load legacy execution evidence');

console.log('retired Web Jobs UI tests passed');
