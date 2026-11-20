'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {Blob} = require('node:buffer');
const vm = require('node:vm');
const downloads = [], revoked = [], pending = [];
const context = {window: {}, Blob,
  URL: {createObjectURL(blob) { downloads.push({blob}); return 'blob:readme'; }, revokeObjectURL(url) { revoked.push(url); }},
  document: {body: {append() {}}, createElement() { return {click() { downloads.at(-1).filename = this.download; }, remove() {}}; }},
  setTimeout(fn) { pending.push(fn); }};
vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/readme-export.js', 'utf8'), context);
const exporter = context.window.ResearchKitReadmeExport;
(async () => {
  const original = '\ufeff---\r\ntags: [海洋, 教学]\r\n---\r\n# README\r\n\r\n' + '完整内容\n'.repeat(5000) + '**最后一行**';
  const entry = {id: 'demo', name: '数据集/说明', tags: ['海洋', '教学'], hasReadme: true,
    load: async () => ({original, markdown: '# README\n正文\n\n**最后一行**', complete: true})};
  const single = await exporter.build([entry]);
  assert.deepEqual(Buffer.from(await single.arrayBuffer()), Buffer.from(original), 'original bytes including UTF-8 BOM, tags and CRLF survive individual export');
  const combined = await exporter.build([entry, {id:'no-readme', name:'空说明', hasReadme:false}], {combined:true});
  const combinedText = await combined.text();
  assert.match(combinedText, /共 2 个/); assert.match(combinedText, /海洋、教学/); assert.match(combinedText, /最后一行/);
  assert.match(combinedText, /空说明/); assert.match(combinedText, /未提供 README.md/);
  await assert.rejects(exporter.build([]), /筛选结果为空/);
  await assert.rejects(exporter.build([{...entry, load:async()=>({markdown:'summary',complete:true})}]), /刷新扫描/);
  await assert.rejects(exporter.build([{...entry, load:async()=>({original:'truncated',complete:false})}]), /完整 README/);
  const control = {disabled:false}, status = {};
  let resolve;
  exporter.bind(control, status, () => new Promise(done => {resolve=done;}));
  const running = control.onclick(); await control.onclick();
  assert.equal(control.disabled,true,'double click cannot start a duplicate download');
  resolve([entry]); await running;
  assert.equal(downloads.length,1); assert.equal(control.disabled,false);
  assert.equal(downloads[0].filename,'数据集_说明-README.md');
  assert.match(status.textContent,/已导出原始/);
  pending.forEach(fn=>fn()); assert.deepEqual(revoked,['blob:readme']);
  exporter.bind(control,status,async()=>[entry,{...entry,load:async()=>{throw new Error('未获授权');}}],{combined:true});
  await control.onclick();
  assert.equal(downloads.length,1,'permission failure after reading one entry must not download a partial file');
  assert.match(status.textContent,/导出失败.*未获授权/);
  // Enforce the aggregate memory bound before constructing a large download.
  const chunk='x'.repeat(1024*1024);
  await assert.rejects(exporter.build(Array.from({length:65},(_,i)=>({...entry,id:String(i),load:async()=>({original:chunk,markdown:chunk,complete:true})})),{combined:true}),/64 MiB/);
  console.log('README export UI passed: exact bytes, all bodies, missing README, no partial download, bounded size, repeat-click guard');
})().catch(error=>{console.error(error);process.exitCode=1;});
