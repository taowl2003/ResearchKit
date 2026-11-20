'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function node(tagName='') {
  return {tagName,children:[],dataset:{},textContent:'',value:'',disabled:false,hidden:false,className:'',
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
    setAttribute(name,value){this[name]=value;},querySelectorAll(){return [];}};
}
async function flush(){for(let i=0;i<8;i++)await Promise.resolve();}
const ids=['#data-management-tags','#data-management-tag-mode','#data-management-tag-clear','#data-management-list','#data-management-pagination','#data-management-status'];
const nodes=new Map(ids.map(id=>[id,node(id)]));
const mode=nodes.get('#data-management-tag-mode');mode.options=[{value:'any'},{value:'all'}];
const responses=[];
const context={window:{},document:{querySelector:id=>nodes.get(id),createElement:tag=>node(tag)},setTimeout(){return 1;},clearTimeout(){}};
vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/data-manager.js','utf8'),context);
const manager=context.window.ResearchKitDataManager;
manager.init({getMe:()=>({role:'teacher'}),api:async path=>new Promise((resolve,reject)=>responses.push({path,resolve,reject}))});
const state=manager._test.state;state.active=true;
assert.equal(state.tagMode,'all','a fresh page defaults to matching all selected labels');
const index=fs.readFileSync('core/researchkit/web_static/index.html','utf8');
assert.match(index, /id="data-management-tag-mode"><option value="all">同时具备全部标签（默认）/);
function page(extra={}){return {items:[],total:1,all_tags:['海洋','教学','气候'],available_tags:['海洋','教学'],selectable_tags:['海洋','教学'],tag_mode_totals:{any:1,all:1},...extra};}
function show(value){state.page=value;manager._test.renderTagFilters(value);}
function chip(text){return nodes.get('#data-management-tags').children.find(item=>item.textContent===text);}
(async()=>{
  show(page());
  assert.equal(chip('海洋').disabled,false);
  assert.equal(chip('气候').disabled,true,'absent tags stay visible but unavailable before a tag is selected');
  assert.match(chip('气候').className,/unavailable/);
  const blocked=chip('气候');blocked.onclick();
  assert.equal(responses.length,0,'even a stale/programmatic click cannot select an unavailable tag');
  chip('海洋').onclick();
  assert.equal(state.tags.join(','),'海洋');
  assert.equal(chip('海洋').disabled,false,'a selected tag is always cancelable during requests');
  assert.equal(chip('教学').disabled,true,'wait for the latest result before allowing another addition');
  manager._test.selectTag('教学');
  assert.equal(responses.length,1,'rapid additions cannot combine two stale availability decisions');
  responses[0].resolve(page({selectable_tags:['海洋','教学']}));await flush();
  assert.match(chip('海洋').className,/selected/);
  assert.equal(chip('教学').disabled,false);
  assert.equal(chip('气候').disabled,true);
  chip('教学').onclick();
  // Cancel the first tag before the second request returns. Only the last
  // response is allowed to update availability and matching-mode controls.
  chip('海洋').onclick();
  assert.equal(state.tags.join(','),'教学');
  responses[2].resolve(page({selectable_tags:['教学','气候']}));await flush();
  responses[1].resolve(page({selectable_tags:['海洋']}));await flush();
  assert.equal(chip('气候').disabled,false,'late responses cannot re-disable a valid tag');
  assert.equal(chip('海洋').disabled,true);
  state.tags=['海洋','气候'];state.tagMode='any';
  show(page({selectable_tags:['海洋','教学','气候'],tag_mode_totals:{any:2,all:0}}));
  assert.equal(mode.options.find(item=>item.value==='all').disabled,true,'switching to an empty ALL intersection is unavailable');
  mode.value='all';mode.onchange();
  assert.equal(state.tagMode,'any');assert.equal(mode.value,'any');
  show(page({total:0,selectable_tags:[],tag_mode_totals:{any:0,all:0}}));
  assert.equal(chip('海洋').disabled,false,'selected labels remain cancelable if a status/query removes every match');
  assert.equal(chip('教学').disabled,true);
  nodes.get('#data-management-tag-clear').onclick();
  responses.at(-1).reject(new Error('offline'));await flush();
  assert.equal(chip('教学').disabled,true,'failed refresh does not reuse stale availability');
  assert.match(nodes.get('#data-management-status').textContent,/offline/);
  console.log('tag filter UI passed: three states, stale/rapid clicks, empty mode switch and recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});
