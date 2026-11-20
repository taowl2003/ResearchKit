'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {Blob} = require('node:buffer');
const downloads=[];
function node(tagName='') {
  return {tagName,textContent:'',hidden:false,disabled:false,checked:false,children:[],dataset:{},value:'',
    append(...items){this.children.push(...items);},prepend(...items){this.children.unshift(...items);},
    replaceChildren(...items){this.children=items;},get childElementCount(){return this.children.length;},
    querySelector(){return this.button;},remove(){},click(){if(this.tagName==='a')downloads.at(-1).name=this.download;},setAttribute(){}};
}
function find(root,predicate){if(predicate(root))return root;for(const child of root.children||[]){const hit=find(child,predicate);if(hit)return hit;}return null;}
const html=fs.readFileSync('core/researchkit/web_static/index.html','utf8');
assert.match(html, /id="dataset-visibility-controls"/, 'the real page must contain the preference control host without replacing search');
const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(match=>['#'+match[1],node()]));
const requests=[];let conflict=false;let resolveOld;let oldPending=false;let visibility={hidden_algorithms:[],hidden_datasets:[]};let visibilityFailure=false;let derivedHasMore=false;
const fullReadme='# 完整数据说明\n\n'+ '正文。\n'.repeat(5000)+'\n## 最后章节\n';
const raw={name:'共享练习',description:'短说明',provenance_ref:'README:README.md#sha256='+ 'a'.repeat(64),dataset_id:'whole-v1',kind:'directory',file_count:2000,total_size:8*1024**3,files:[{path:'__researchkit_dataset_tree__',size:8*1024**3,sha256:'a'.repeat(64)}]};
const projects=[{project_id:'project-a',name:'first',files:[
  {path:'data/rainfall.csv',size:12,editable:true},
  {path:'data/processed/big.bin',size:8*1024**3,editable:true},
  {path:'data/reference.csv',size:12,reference_only:true},
  {path:'data/managed.csv',size:12,managed_reference:true},
  {path:'data/folder',size:0,directory:true},
]},{project_id:'project-b',name:'second',files:[]}];
const response=body=>({ok:true,json:async()=>body});
const storage=new Map();
const context={Blob,window:{__researchkitAppTest:true},location:{hash:''},history:{replaceState(){}},URLSearchParams,
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},Headers:class{set(){}},File:class{},FormData:class{},
  URL:{createObjectURL(blob){downloads.push({blob});return'blob:test';},revokeObjectURL(){}},setTimeout(){return 1;},clearTimeout(){},
  document:{querySelector:id=>nodes.get(id),querySelectorAll:()=>[],addEventListener(){},dispatchEvent(){},createElement:tag=>node(tag),createTextNode:value=>({...node('#text'),textContent:value}),body:{append(){}}},
  fetch:async(path,options={})=>{
    requests.push({path,options});
    if(path==='/api/datasets')return response([raw]);
    if(path==='/api/personal-resource-visibility'){
      if(options.method==='PUT'){
        if(visibilityFailure)return{ok:false,status:503,json:async()=>({error:'偏好保存失败'})};
        const body=JSON.parse(options.body);visibility.hidden_datasets=visibility.hidden_datasets.filter(id=>id!==body.ref);
        if(body.hidden)visibility.hidden_datasets.push(body.ref);return response(visibility);
      }
      return response(visibility);
    }
    if(path==='/api/datasets/whole-v1/readme')return response({markdown:fullReadme,original:fullReadme,complete:true});
    if(path==='/api/projects?include_archived=true')return response(projects);
    if(path.startsWith('/api/derived-datasets?')){
      if(derivedHasMore&&path.includes('cursor='))return response({items:[{...raw,dataset_id:'next-visible',name:'下一页可用数据',owner_username:'test',visibility:'private'}],next_cursor:null});
      return response({items:[{...raw,owner_username:'test',visibility:'review'}],next_cursor:derivedHasMore?'next-page':null});
    }
    if(path==='/api/projects/project-a/datasets'&&options.method==='PUT'){
      if(conflict)return{ok:false,status:409,json:async()=>({error:'编辑冲突'})};
      return response({sha256:'b'.repeat(64),datasets:[{dataset_id:'whole-v1',file_count:1}],references:raw.files});
    }
    if(path==='/api/projects/project-a/datasets'&&oldPending)return new Promise(resolve=>{resolveOld=resolve;});
    if(path.endsWith('/datasets'))return response({sha256:null,datasets:[],references:[]});
    if(path==='/api/projects/project-a/derived')return response({name:'features',dataset_id:'derived-v1',source_reference_only_after_attach:true});
    throw new Error(`unexpected ${path}`);
  },confirm:()=>true,CustomEvent:class{},Intl};
for(const name of ['markdown.js','readme-export.js'])vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/'+name,'utf8'),context);
vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js','utf8'),context,{filename:'app.js'});
const hooks=context.window.__researchkitAppTest;
(async()=>{
  hooks.setMe({username:'test',uid:2001,role:'student'});
  await hooks.datasets();
  const readmeExpand=find(nodes.get('#dataset-list'),item=>item.className==='readme-expand');
  assert.equal(Boolean(readmeExpand.open),false);
  readmeExpand.open=true;await readmeExpand.ontoggle();
  const lastHeading=find(readmeExpand,item=>item.tagName==='h2'&&find(item,part=>part.textContent==='最后章节'));
  assert.ok(lastHeading,'authorized personal view renders the complete README tail');
  const exportOne=find(nodes.get('#dataset-list'),item=>item.textContent==='导出 README.md');
  await exportOne.onclick();assert.equal(await downloads.at(-1).blob.text(),fullReadme);
  await nodes.get('#dataset-export').onclick();assert.match(await downloads.at(-1).blob.text(),/共享练习/);
  // The global export respects the exact same local search as the visible cards.
  nodes.get('#dataset-query').value='无匹配';nodes.get('#dataset-query').oninput();
  const before=downloads.length;await nodes.get('#dataset-export').onclick();
  assert.equal(downloads.length,before);assert.match(nodes.get('#dataset-export-status').textContent,/筛选结果为空/);
  nodes.get('#dataset-query').value='';nodes.get('#dataset-query').oninput();
  nodes.get('#dataset-project').value='project-a';await nodes.get('#dataset-project').onchange();
  const choose=find(nodes.get('#dataset-list'),item=>item.tagName==='input'&&item.type==='checkbox');
  choose.checked=true;choose.onchange();
  assert.equal(nodes.get('#add-project-datasets').disabled,false,'whole 8 GiB tree is attachable without legacy file limits');
  const expand=find(nodes.get('#dataset-list'),item=>item.textContent==='全部展开');expand.onclick();
  assert.equal(find(nodes.get('#dataset-list'),item=>item.tagName==='input'&&item.type==='checkbox').checked,true,'folding preserves selections');
  await hooks.attachProjectDatasets();
  const put=requests.find(value=>value.options.method==='PUT');
  assert.deepEqual(JSON.parse(put.options.body),{dataset_ids:['whole-v1'],expected_sha256:null});
  assert.match(nodes.get('#dataset-project-state').textContent,/已引用 1/);
  conflict=true;await hooks.attachProjectDatasets();
  assert.match(nodes.get('#output').textContent,/编辑冲突/);
  assert.equal(nodes.get('#add-project-datasets').disabled,false,'conflict refreshes current file revision');
  const derivablePaths=nodes.get('#derived-project-file').children.map(option=>option.value);
  assert.ok(derivablePaths.includes('data/rainfall.csv'),'ordinary data/ files are available for derived-data import');
  assert.ok(derivablePaths.includes('data/processed/big.bin'),'existing processed-data import remains available');
  assert.equal(derivablePaths.includes('data/reference.csv'),false,'reference-only files stay out of derived-data import');
  assert.equal(derivablePaths.includes('data/managed.csv'),false,'managed references stay out of derived-data import');
  assert.equal(derivablePaths.includes('data/folder'),false,'directories stay out of derived-data import');
  nodes.get('#derived-project-file').value='data/rainfall.csv';nodes.get('#derived-project-file').onchange();
  nodes.get('#derived-project-name').value='rainfall-derived';nodes.get('#derived-project-name').oninput();
  assert.equal(nodes.get('#save-project-derived').disabled,false,'a tutorial CSV can be saved as derived data');
  await hooks.saveProjectDerived();
  assert.deepEqual(JSON.parse(requests.find(value=>value.path.endsWith('/derived')&&JSON.parse(value.options.body).path==='data/rainfall.csv').options.body),
    {path:'data/rainfall.csv',name:'rainfall-derived'});
  nodes.get('#derived-project-file').value='data/processed/big.bin';nodes.get('#derived-project-file').onchange();
  nodes.get('#derived-project-name').value='features';nodes.get('#derived-project-name').oninput();
  assert.equal(nodes.get('#save-project-derived').disabled,false,'large ordinary project outputs can be imported');
  await hooks.saveProjectDerived();
  assert.deepEqual(JSON.parse(requests.find(value=>value.path.endsWith('/derived')&&JSON.parse(value.options.body).path==='data/processed/big.bin').options.body),{path:'data/processed/big.bin',name:'features'});
  assert.match(nodes.get('#output').textContent,/加入引用后提交不会再打包/);
  // A late response for the previous project must not overwrite the newly selected project.
  oldPending=true;nodes.get('#dataset-project').value='project-a';const pending=nodes.get('#dataset-project').onchange();
  nodes.get('#dataset-project').value='project-b';await nodes.get('#dataset-project').onchange();
  resolveOld(response({sha256:'c'.repeat(64),datasets:[{dataset_id:'stale',file_count:1}]}));await pending;
  assert.equal(nodes.get('#dataset-project').value,'project-b');
  assert.match(nodes.get('#dataset-project-state').textContent,/暂未引用/);
  await hooks.datasets();await hooks.derivedDatasets();
  const removePersonal=find(nodes.get('#dataset-list'),item=>item.tagName==='button'&&item.textContent==='从我的数据列表移除');
  await removePersonal.onclick();
  const hideRequest=requests.find(value=>value.path==='/api/personal-resource-visibility'&&value.options.method==='PUT');
  assert.deepEqual(JSON.parse(hideRequest.options.body),{kind:'dataset',ref:'whole-v1',hidden:true},'data removal persists only the personal visibility setting');
  assert.equal(find(nodes.get('#dataset-list'),item=>item.textContent==='whole-v1'),null,'the main data list immediately filters a removed dataset');
  assert.equal(find(nodes.get('#derived-dataset-list'),item=>item.textContent==='whole-v1'),null,'the derived-data list applies the same hidden dataset setting');
  const showHidden=find(nodes.get('#dataset-visibility-controls'),item=>item.tagName==='input'&&item.type==='checkbox');
  showHidden.checked=true;showHidden.onchange();
  const restore=find(nodes.get('#dataset-list'),item=>item.tagName==='button'&&item.textContent==='恢复显示');
  await restore.onclick();
  assert.ok(requests.some(value=>value.path==='/api/personal-resource-visibility'&&value.options.method==='PUT'&&JSON.parse(value.options.body).hidden===false),'data recovery persists the opposite preference');
  const chooseAgain=find(nodes.get('#dataset-list'),item=>item.tagName==='input'&&item.type==='checkbox');
  chooseAgain.checked=true;chooseAgain.onchange();visibilityFailure=true;
  const failRemove=find(nodes.get('#dataset-list'),item=>item.tagName==='button'&&item.textContent==='从我的数据列表移除');
  await failRemove.onclick();
  assert.equal(chooseAgain.checked,true,'a failed preference update preserves pending data selection');
  visibilityFailure=false;hooks.setMe({username:'teacher',uid:2002,role:'teacher'});
  await hooks.datasets();await hooks.derivedDatasets();
  assert.equal(find(nodes.get('#derived-dataset-list'),item=>item.tagName==='button'&&item.textContent==='授权审核读取'),null,'teacher personal data view has no audit-grant button');
  assert.equal(find(nodes.get('#derived-dataset-list'),item=>item.tagName==='button'&&item.textContent==='发布为可复用数据'),null,'teacher personal data view has no publish button');
  // Hiding a whole page must not make the next page inaccessible.
  derivedHasMore=true;await hooks.datasets();await hooks.derivedDatasets();
  const showToggle=find(nodes.get('#dataset-visibility-controls'),item=>item.tagName==='input'&&item.type==='checkbox');
  showToggle.checked=false;showToggle.onchange();
  const removeFirstPage=find(nodes.get('#derived-dataset-list'),item=>item.tagName==='button'&&item.textContent==='从我的数据列表移除');
  await removeFirstPage.onclick();
  assert.equal(nodes.get('#derived-dataset-listing').hidden,false,'pagination stays available when this page is personally hidden');
  const nextPage=find(nodes.get('#derived-dataset-list'),item=>item.tagName==='button'&&item.textContent==='加载更多派生数据');
  assert.ok(nextPage,'hidden first page still offers the next page');await nextPage.onclick();
  assert.ok(find(nodes.get('#derived-dataset-list'),item=>item.textContent.includes('下一页可用数据')),'the next visible page remains usable');
  console.log('dataset workflow UI passed: whole tree, folding, CAS conflict, streamed import, stale response');
})().catch(error=>{console.error(error);process.exitCode=1;});
