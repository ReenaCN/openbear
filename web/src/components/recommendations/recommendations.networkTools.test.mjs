import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse,compileScript,compileTemplate} from '@vue/compiler-sfc';
import * as UI from 'element-plus';
import * as Icons from '@element-plus/icons-vue';
import postcss from 'postcss';
import * as config from './recConfig.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const descriptor=parse(read('../../views/RecommendationsView.vue')).descriptor;
const script=compileScript(descriptor,{id:'recommendation-network-settings'});
const walk=nodes=>(nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[]),...walk(n.component?.subTree?[n.component.subTree]:[])]);
const options=[
  {name:'mcp__other__discover',description:'搜索公开资料',available:true,source:'mcp'},
  {name:'mcp__other__document',description:'读取网页正文',available:true,source:'mcp'},
];
const initial=()=>({settings:{enabled:false,models:['local/model'],networkTools:{search:[],fetch:[]},scopeMode:'all',scopeFolders:[],
  followEnabled:true,followMinNewUserMessages:4,followCooldownMinutes:10,followConcurrency:1,followIdleMinutes:3,
  trackingIntervalMinutes:45,initialDays:7,runTimeoutMinutes:40,dailyBudgetUsd:null,dailyBudgetTokens:null},
  revision:2,protocols:{chat:[{key:'local/model',label:'模型'}]},networkToolOptions:structuredClone(options),
  defaultPrompts:{system:'system',daily:'daily',follow:'follow'},activity:{days:7,hourHistogram:Array(24).fill(0)},folders:[],derivedDailyTime:'03:00'});

function runtime({phone=false}={}){
  let data=initial();const saves=[];
  const placeholder=Vue.defineComponent({render(){return Vue.h('div');}});
  const context=vm.createContext({...Vue,...Icons,...config,
    defineEmits:()=>()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},watch:()=>{},useAdminPhone:()=>Vue.ref(phone),
    AdminPageHeader:placeholder,RecCard:placeholder,RecMobileSheet:placeholder,AdaptiveMdEditor:placeholder,
    referenceToken:()=>'',copyTextToClipboard:async()=>{},setRecommendationUnseen:()=>{},crypto:globalThis.crypto,
    ElMessage:{success:()=>{},warning:()=>{},error:error=>assert.fail(error)},ElMessageBox:{confirm:async()=>{}},apiError:error=>String(error),
    Api:{recSettings:async()=>structuredClone(data),recSaveSettings:async body=>{
      saves.push(structuredClone(body));data={...data,settings:structuredClone(body.settings),revision:data.revision+1};return structuredClone(data);
    },recOverview:async()=>null},
  });
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm,''),context);
  const run=code=>vm.runInContext(code,context);
  const scope=Vue.proxyRefs(run(`({${Object.keys(script.bindings).join(',')}})`));
  async function render(){
    let tree;const compiled=Vue.compile(descriptor.template.content);
    const app=Vue.createSSRApp({render(){tree=compiled.call(this,scope,[]);return tree;}});
    for(const [name,component] of Object.entries({...UI,...Icons}))if(name.startsWith('El')||name in Icons)app.component(name,component);
    for(const name of ['AdminPageHeader','RecCard','RecMobileSheet','AdaptiveMdEditor'])app.component(name,placeholder);
    app.provide(UI.ID_INJECTION_KEY,{prefix:1024,current:0});
    app.provide(UI.ZINDEX_INJECTION_KEY,{current:0});
    app.config.warnHandler=message=>assert.fail(message);
    return {html:await renderToString(app),nodes:walk([tree])};
  }
  return {run,render,saves,setData:value=>{data=value;},data:()=>structuredClone(data)};
}

function networkSelect(view,key){return view.nodes.find(node=>node.type===UI.ElSelect&&node.props.id==='rec-network-'+key);}

test('network settings compile against the actual page bindings',()=>{
  assert.deepEqual(compileTemplate({source:descriptor.template.content,filename:'RecommendationsView.vue',id:'network',compilerOptions:{bindingMetadata:script.bindings}}).errors,[]);
});

test('tool options use registry availability, retain unavailable selections and do not infer purpose',()=>{
  const unusual={name:'OpaqueTool42',description:'No role guessing',available:true};
  const result=config.networkToolOptions([...options,unusual],['offline-selection']);
  assert.deepEqual(result.map(item=>item.name),[...options.map(item=>item.name),'OpaqueTool42','offline-selection']);
  assert.equal(result.at(-1).available,false);
  assert.deepEqual(config.networkToolOptions([],[]),[]);
  assert.equal(config.networkToolWarning('搜索工具',[options[0].name],options),'');
  assert.match(config.networkToolWarning('搜索工具',[],options),/尚未选择/);
  assert.match(config.networkToolWarning('抓取工具',['offline-selection'],[]),/不可用.*选择已保留/);
});

test('actual select change events save explicit roles and selected unavailable tools remain removable',async()=>{
  const r=runtime();await r.run('loadSettings()');r.run('tab.value="settings"');
  let view=await r.render();
  networkSelect(view,'search').props['onUpdate:modelValue']([options[0].name]);
  networkSelect(view,'fetch').props['onUpdate:modelValue']([options[1].name]);
  assert.equal(r.run('dirty.value'),true);
  assert.equal(r.run('networkLimited.value'),false);
  await r.run('saveSettings()');
  assert.deepEqual(JSON.parse(JSON.stringify(r.saves[0].settings.networkTools)),{search:[options[0].name],fetch:[options[1].name]});
  assert.equal(r.run('dirty.value'),false);
  const offline=r.data();offline.networkToolOptions=[];r.setData(offline);
  await r.run('refreshNetworkTools()');view=await r.render();
  assert.match(view.html,/选择已保留/);
  assert.equal(networkSelect(view,'search').props.modelValue[0],options[0].name);
  networkSelect(view,'search').props['onUpdate:modelValue']([]);
  await r.run('saveSettings()');
  assert.deepEqual(JSON.parse(JSON.stringify(r.saves[1].settings.networkTools)),{search:[],fetch:[options[1].name]});
});

test('missing and offline hints render only in recommendation settings, healthy selections stay quiet',async()=>{
  const r=runtime();const empty=r.data();empty.networkToolOptions=[];r.setData(empty);
  await r.run('loadSettings()');
  for(const tab of ['cards','follows','profile','runs']){
    r.run(`tab.value=${JSON.stringify(tab)}`);
    const {html}=await r.render();
    assert.doesNotMatch(html,/rec-network-tools|rec-network-warning|联网发现可能受限|尚未选择搜索工具/);
  }
  r.run('tab.value="settings"');
  let view=await r.render();
  assert.match(view.html,/尚未选择搜索工具/);assert.match(view.html,/尚未选择抓取工具/);assert.match(view.html,/内部推荐仍可运行/);
  const healthy=initial();healthy.settings.networkTools={search:[options[0].name],fetch:[options[1].name]};r.setData(healthy);
  await r.run('loadSettings()');view=await r.render();
  assert.doesNotMatch(view.html,/class="rec-network-warning"|联网发现可能受限/);
  assert.match(view.html,/aria-label="推荐联网工具"/);
});

test('refreshing availability preserves unsaved edits and failed refresh stays in settings',async()=>{
  const r=runtime();await r.run('loadSettings()');r.run('tab.value="settings";settingsForm.initialDays=12;settingsForm.networkTools.search=["offline-name"]');
  const data=r.data();data.networkToolOptions=[];r.setData(data);
  await r.run('refreshNetworkTools()');
  assert.equal(r.run('settingsForm.initialDays'),12);
  assert.deepEqual(Array.from(r.run('settingsForm.networkTools.search')),['offline-name']);
  assert.equal(r.run('dirty.value'),true);
  r.run('Api.recSettings=async()=>{throw new Error("offline")};');
  await r.run('refreshNetworkTools()');
  assert.match((await r.render()).html,/刷新失败/);
  r.run('tab.value="cards"');assert.doesNotMatch((await r.render()).html,/刷新失败/);
});

test('phone controls bound tag growth and long names; CSS keeps 320/375px single-column layout',async()=>{
  const r=runtime({phone:true});const data=r.data();
  data.settings.networkTools.search=Array.from({length:20},(_,i)=>`mcp__very_long_third_party_name_${i}__${'x'.repeat(70)}`);
  data.networkToolOptions=data.settings.networkTools.search.map(name=>({name,description:'说明'.repeat(100),available:true}));
  r.setData(data);await r.run('loadSettings()');r.run('tab.value="settings"');const view=await r.render();
  const select=networkSelect(view,'search');
  assert.equal(select.component.props.collapseTags,true);
  assert.equal(select.component.props.maxCollapseTags,1);
  assert.equal(select.component.props.collapseTagsTooltip,false); // Removal is available by touch in the menu, not hover.
  const css=postcss.parse(read('./recommendations.css'));
  function declarations(selector,width){
    const out={};css.walkRules(rule=>{
      if(!rule.selector.split(',').map(s=>s.trim()).includes(selector))return;
      for(let parent=rule.parent;parent;parent=parent.parent){
        if(parent.type==='atrule'&&parent.name==='media'){
          const max=parent.params.match(/max-width:\s*(\d+)px/),min=parent.params.match(/min-width:\s*(\d+)px/);
          if((max&&width>Number(max[1]))||(min&&width<Number(min[1])))return;
        }
      }
      rule.walkDecls(d=>{out[d.prop]=d.value;});
    });return out;
  }
  for(const width of [320,375,760]){
    assert.equal(declarations('.rec-form',width)['grid-template-columns'],'minmax(0,1fr)');
    assert.equal(declarations('.rec-network-tools .el-select__wrapper',width)['min-height'],'44px');
  }
  assert.equal(declarations('.rec-form',1024)['grid-template-columns'],'repeat(2,minmax(0,1fr))');
  assert.equal(declarations('.rec-network-options',320)['max-width'],'calc(100vw - 24px)');
  assert.equal(declarations('.rec-network-option-name',320)['overflow-wrap'],'anywhere');
  assert.equal(declarations('.rec-network-option-description',320)['-webkit-line-clamp'],'2');
  assert.equal(declarations('.rec-network-options .el-select-dropdown__item',320)['min-height'],'44px');
});


test('actual settings save and disable preserve a healthy fallback after head removal',async()=>{
  const r=runtime(),data=initial();
  data.settings.enabled=true;data.settings.models=['removed/head','local/model'];
  r.setData(data);await r.run('loadSettings()');
  r.run('settingsForm.initialDays=12');
  await r.run('saveSettings()');
  await r.run('toggleEnabled(false)');
  assert.equal(r.saves.length,2);
  for(const save of r.saves)assert.deepEqual(Array.from(save.settings.models),['removed/head','local/model']);
  assert.equal(r.saves[1].settings.enabled,false);
  assert.equal(r.saves[1].settings.initialDays,12);
});
