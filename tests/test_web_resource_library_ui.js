'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function node(tagName='') {
  return {tagName,textContent:'',value:'',children:[],dataset:{},className:'',disabled:false,hidden:false,
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
    remove(){this.removed=true;},querySelector(selector){return find(this,item=>selector==='.'+item.className&&!item.removed);}};
}
function find(root,predicate){if(predicate(root))return root;for(const child of root.children||[]){if(child.removed)continue;const hit=find(child,predicate);if(hit)return hit;}return null;}
function collect(root,predicate,out=[]){if(predicate(root))out.push(root);for(const child of root.children||[])if(!child.removed)collect(child,predicate,out);return out;}
function allText(root){return [root.textContent||'',...(root.children||[]).filter(x=>!x.removed).map(allText)].join('\n');}
function load(initialRole='teacher'){
  const nodes=new Map(['#resource-library-list','#resource-library-message','#resource-library-refresh','#resource-management-list','#resource-management-message','#resource-management-refresh'].map(id=>[id,node()]));
  let role=initialRole,accessFailure=false,catalogHook=null,cleanupInfo=null,visibility={hidden_algorithms:[],hidden_datasets:[]},visibilityFailure=false;
  let accessRows=[{kind:'template',ref:'rain.case@1.0.0'},{kind:'algorithm',ref:'scale@1.0.0'}];
  const calls=[];
  const catalog=()=>({templates:[{ref:'rain.case@1.0.0',name:'降雨演示'}],plugins:[{ref:'scale@1.0.0',name:'<img src=x onerror=bad()>',description:'工具'}],contracts:[{id:'scalar@1.0.0',description:'数值'}]});
  const context={window:{},document:{querySelector:id=>nodes.get(id),querySelectorAll:()=>[],createElement:tag=>node(tag)},JSON,Map,Set,Promise,Math,Date};
  for(const name of ['lifecycle.js','workflow.js'])vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/'+name,'utf8'),context,{filename:name});
  const hooks=context.window.ResearchKitAssignments;
  hooks.init({getMe:()=>({role}),getProjects:()=>[{project_id:'own',name:'我的项目'}],post:async(path,body)=>{calls.push({path,body});return {};},api:async(path,options)=>{
    calls.push({path,options});
    if(path==='/api/catalog')return catalogHook?catalogHook():catalog();
      if(path==='/api/component-access?include_inactive=true'){
        if(accessFailure)throw new Error('暂时不可读');
      return accessRows;
    }
    if(path==='/api/datasets')return [];
    if(path==='/api/personal-resource-visibility'){
      if(visibilityFailure)throw new Error('偏好暂不可读');
      if(options?.method==='PUT'){
        const body=JSON.parse(options.body);const key=body.kind==='algorithm'?'hidden_algorithms':'hidden_datasets';
        visibility[key]=visibility[key].filter(value=>value!==body.ref);if(body.hidden)visibility[key].push(body.ref);return visibility;
      }
      return visibility;
    }
    if(path==='/api/derived-datasets/access')return {};
    if(path.startsWith('/api/asset-cleanup/check?'))return cleanupInfo||{can_delete:false,blocking_cases:[{ref:'other.case@1.0.0',name:'其他案例'}]};
    throw new Error('unexpected '+path);
  }});
  return {hooks,nodes,calls,setRole:value=>{role=value;},setFailure:value=>{accessFailure=value;},setCatalogHook:value=>{catalogHook=value;},setAccessRows:value=>{accessRows=value;},setCleanupInfo:value=>{cleanupInfo=value;},setVisibility:value=>{visibility=value;},setVisibilityFailure:value=>{visibilityFailure=value;},catalog};
}
(async()=>{
  const app=load();await app.hooks.loadManagement();
  const list=app.nodes.get('#resource-management-list');
  assert.match(allText(list),/降雨演示/);
  assert.doesNotMatch(allText(list),/中央存储概览|相同文件内容只保存一份|关联数据集新版本|按项目清理/);
  assert.equal(app.calls.some(x=>x.path==='/api/storage'),false,'management never requests global storage accounting');
  assert.doesNotMatch(allText(list),/<img src=x onerror=bad\(\)>/,'case management only lists cases');
  assert.equal(collect(list,x=>x.tagName==='button'&&x.textContent==='删除案例').length,1,'each case version has one clear delete action');
  assert.equal(app.calls.some(x=>x.options?.method==='PUT'),false,'merely listing resources does not change them');
  const card=find(list,x=>x.className==='card resource-card'&&allText(x).includes('rain.case@1.0.0'));
  await find(card,x=>x.tagName==='button'&&x.textContent==='删除案例').onclick();
  assert.match(allText(card),/other\.case@1\.0\.0/,'blocking cases are listed before deletion');
  assert.equal(find(card,x=>x.tagName==='button'&&x.textContent==='确认删除案例'),null,'a referenced resource cannot be confirmed for deletion');
  assert.ok(app.calls.some(x=>x.path==='/api/component-access?include_inactive=true'),'teacher management requests inactive platform versions');
  assert.equal(app.calls.some(x=>x.path==='/api/personal-resource-visibility'),false,'teacher management never reads personal visibility preferences');
  const historical=load();
  historical.setCatalogHook(()=>({templates:[],plugins:[],contracts:[]}));
  historical.setAccessRows([
    {kind:'template',ref:'calibration-template@1.0.0',display_name:'校准案例',description:'已下架的校准模板',tags:['校准'],family_id:'calibration-template',parent_ref:null,lifecycle:{state:'retired'}},
    {kind:'template',ref:'archive-template@2.0.0',display_name:'归档案例',description:'回收站模板',tags:['归档'],family_id:'archive-template',parent_ref:null,lifecycle:{state:'trashed'}},
  ]);
  await historical.hooks.loadManagement();
  const historicalList=historical.nodes.get('#resource-management-list');
  assert.match(allText(historicalList),/calibration-template@1\.0\.0/,'retired template remains visible to teacher management');
  assert.match(allText(historicalList),/已下架/);
  assert.match(allText(historicalList),/archive-template@2\.0\.0/,'trashed template remains visible to teacher management');
  assert.match(allText(historicalList),/已移入回收站/);
  assert.match(allText(historicalList),/仍在平台库内，引用依赖保留/);
  const historicalDelete=find(historicalList,x=>x.tagName==='button'&&x.textContent==='删除案例');
  historical.setCleanupInfo({can_delete:true});await historicalDelete.onclick();
  assert.ok(historical.calls.some(x=>x.path.startsWith('/api/asset-cleanup/check?kind=template&ref=calibration-template%401.0.0')),
    'inactive cases use the same dependency check before deletion');
  const historicalConfirm=find(historicalList,x=>x.tagName==='button'&&x.textContent==='确认删除案例');
  assert.ok(historicalConfirm,'an unreferenced inactive case reaches the existing confirmation step');
  await historicalConfirm.onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(historical.calls.find(x=>x.path==='/api/asset-cleanup/direct').body)),
    {kind:'template',ref:'calibration-template@1.0.0',confirmation:'永久清理'},
    'inactive case deletion keeps the existing direct-cleanup confirmation chain');
  const student=load('student');await student.hooks.loadLibrary();
  assert.match(allText(student.nodes.get('#resource-library-list')),/scale@1.0.0/);
  assert.doesNotMatch(allText(student.nodes.get('#resource-library-list')),/降雨演示|scalar@1.0.0/,'personal algorithm library does not repeat cases or contracts');
  assert.ok(find(student.nodes.get('#resource-library-list'),x=>x.textContent==='加入项目'));
  assert.equal(student.calls.length,2);assert.equal(student.calls[0].path,'/api/catalog');
  assert.equal(student.calls[1].path,'/api/personal-resource-visibility');
  assert.equal(student.calls.some(x=>String(x.path).includes('include_inactive')),
    false,'student library never requests inactive platform resources');
  assert.equal(find(student.nodes.get('#resource-library-list'),x=>x.className==='resource-management'),null,'student receives no management controls');
  assert.ok(find(student.nodes.get('#resource-library-list'),x=>x.tagName==='button'&&x.textContent==='从我的算法库移除'),'personal algorithm rows offer a display-only removal action');
  const removePersonal=find(student.nodes.get('#resource-library-list'),x=>x.tagName==='button'&&x.textContent==='从我的算法库移除');
  await removePersonal.onclick();
  assert.deepEqual(JSON.parse(student.calls.find(x=>x.path==='/api/personal-resource-visibility'&&x.options?.method==='PUT').options.body),
    {kind:'algorithm',ref:'scale@1.0.0',hidden:true},'personal removal persists only a visibility preference');
  assert.doesNotMatch(allText(student.nodes.get('#resource-library-list')),/scale@1\.0\.0/,'removed algorithms are hidden by default');
  const hiddenToggle=find(student.nodes.get('#resource-library-list'),x=>x.tagName==='input'&&x.type==='checkbox');
  hiddenToggle.checked=true;await hiddenToggle.onchange();
  assert.match(allText(student.nodes.get('#resource-library-list')),/scale@1\.0\.0/,'the removed algorithm can be shown for recovery');
  const restore=find(student.nodes.get('#resource-library-list'),x=>x.tagName==='button'&&x.textContent==='恢复显示');await restore.onclick();
  assert.ok(student.calls.some(x=>x.path==='/api/personal-resource-visibility'&&x.options?.method==='PUT'&&JSON.parse(x.options.body).hidden===false),'recovery persists the opposite preference');
  const taskOnly=load();
  taskOnly.setCatalogHook(()=>({templates:[],plugins:[{ref:'retained.task@1.0.0',name:'任务保留副本',source:'task'}],contracts:[]}));
  taskOnly.hooks.setManagementModule('algorithm');await taskOnly.hooks.loadManagement();
  assert.doesNotMatch(allText(taskOnly.nodes.get('#resource-management-list')),/retained\.task|任务保留副本/,
    'retained task grants must not resurrect deleted platform algorithms in management');
  await taskOnly.hooks.loadLibrary();
  assert.match(allText(taskOnly.nodes.get('#resource-library-list')),/retained\.task@1\.0\.0/,
    'personal use of the already granted task algorithm remains available');
  assert.match(allText(taskOnly.nodes.get('#resource-library-list')),/历史任务保留资源/,'task-granted algorithms have their historical source label');
  const teacherPersonal=load('teacher');await teacherPersonal.hooks.loadLibrary();
  assert.equal(find(teacherPersonal.nodes.get('#resource-library-list'),x=>x.className==='resource-management'),null,'teacher personal library has no teacher-management controls');
  const unavailable=load();await unavailable.hooks.loadManagement();unavailable.setFailure(true);await unavailable.hooks.loadManagement();
  assert.match(unavailable.nodes.get('#resource-management-message').textContent,/部分授权信息暂不可读/);
  assert.ok(find(unavailable.nodes.get('#resource-management-list'),x=>x.textContent==='删除案例'),'deletion remains available without optional access metadata');
  const late=load();let resolve;late.setCatalogHook(()=>new Promise(done=>{resolve=done;}));
  const pending=late.hooks.loadManagement();late.setRole('student');late.setCatalogHook(()=>({plugins:[{ref:'new@1.0.0',name:'学生可读'}]}));await late.hooks.loadLibrary();resolve(late.catalog());await pending;
  assert.match(allText(late.nodes.get('#resource-library-list')),/学生可读/);assert.doesNotMatch(allText(late.nodes.get('#resource-library-list')),/降雨演示/,'late teacher response cannot overwrite current student view');
  const leaving=load();let finish;leaving.setCatalogHook(()=>new Promise(done=>{finish=done;}));const waiting=leaving.hooks.loadManagement();leaving.hooks.leaveLibrary();finish(leaving.catalog());await waiting;
  assert.equal(leaving.nodes.get('#resource-management-list').children.length,0,'leaving invalidates pending loads');
  const stale=load();await stale.hooks.loadManagement();const remove=find(stale.nodes.get('#resource-management-list'),x=>x.tagName==='button'&&x.textContent==='删除案例');stale.setRole('student');await remove.onclick();
  assert.equal(stale.calls.some(x=>x.path.startsWith('/api/asset-cleanup/check?')),false,'stale teacher control cannot start cleanup after identity changes');
  console.log('resource library UI passed: role-separated listing, plain resource cleanup, safety gates, stale loads and errors');
})().catch(error=>{console.error(error);process.exitCode=1;});
