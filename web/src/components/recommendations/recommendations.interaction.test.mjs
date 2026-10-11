import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse,compileScript,compileTemplate} from '@vue/compiler-sfc';
import * as Icons from '@element-plus/icons-vue';
import {ElButton,ElInput,ElIcon} from 'element-plus';
import * as config from './recConfig.js';
import {referenceToken} from '../../references/codec.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const cardSfc=parse(read('./RecCard.vue')).descriptor;
const viewSfc=parse(read('../../views/RecommendationsView.vue')).descriptor;
const sheetSfc=parse(read('./RecMobileSheet.vue')).descriptor;
const withoutImports=source=>source.replace(/^import[\s\S]*?;\n/gm,'');
const walk=nodes=>(nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[]),...walk(n.component?.subTree?[n.component.subTree]:[])]);
const now=Date.UTC(2026,9,10,2);
const sample={id:'card',lane:'external',category:'update',title:'一条推荐',reason:'为什么推荐给你',body:'完整说明。'.repeat(60),createdAtMs:now,seenAtMs:null,state:'new',questions:['如何使用','对我有什么帮助'],sources:[],feedback:[]};

function cardRuntime(overrides={}){
  const props=Vue.reactive({card:{...sample,...overrides},lead:false,busy:false,mobile:false}),events=[];
  const context=vm.createContext({...Vue,...Icons,...config,Icons,defineProps:()=>props,defineEmits:()=>(...args)=>events.push(args)});
  vm.runInContext(withoutImports(cardSfc.scriptSetup.content),context);
  const run=code=>vm.runInContext(code,context);
  const scope=Vue.proxyRefs(run('({props,card:computed(()=>props.card),mobile:computed(()=>props.mobile),lead:computed(()=>props.lead),busy:computed(()=>props.busy),emit,noteOpen,note,expanded,isOpenBear,unread,done,asked,meta,icon,publishers,following,actions,originLinkable,notes,muted,long,showFull,submitNote,toggle,CATEGORY_LABELS,clockText,sourceLabel,Check,ChatLineRound,TopRight,ArrowDown,EditPen,StarFilled,Star})'));
  async function render(){
    const compiled=Vue.compile(cardSfc.template.content);let tree;
    const app=Vue.createSSRApp({render(){tree=compiled.call(this,scope,[]);return tree;}});
    for(const [name,component] of Object.entries({ElButton,ElInput,ElIcon,...Icons}))app.component(name,component);
    app.config.warnHandler=message=>assert.fail(message);
    const html=await renderToString(app);
    return {html,nodes:walk([tree])};
  }
  return {props,events,run,render};
}

test('recommendation card and page compile with their actual bindings',()=>{
  for(const [filename,sfc] of [['RecCard.vue',cardSfc],['RecMobileSheet.vue',sheetSfc],['RecommendationsView.vue',viewSfc]]){
    const script=compileScript(sfc,{id:filename});
    assert.deepEqual(compileTemplate({source:sfc.template.content,filename,id:filename,compilerOptions:{bindingMetadata:script.bindings}}).errors,[]);
  }
});

test('unread precedes read across dates and each group is newest first without mutating input',()=>{
  const items=[
    {...sample,id:'read-new',seenAtMs:now,createdAtMs:now},
    {...sample,id:'unread-old',createdAtMs:now-86400e3,priority:9},
    {...sample,id:'unread-new',createdAtMs:now,priority:0},
    {...sample,id:'read-old',seenAtMs:now,createdAtMs:now-86400e3},
  ];
  const groups=config.groupFeed(items,now);
  assert.deepEqual(groups.flatMap(g=>g.items.map(c=>c.id)),['unread-new','unread-old','read-new','read-old']);
  assert.deepEqual(groups.map(g=>g.read),[false,false,true,true]);
  assert.equal(new Set(groups.map(g=>g.key)).size,groups.length);
  assert.equal(items[0].id,'read-new');
  items[2].seenAtMs=now;
  assert.deepEqual(config.groupFeed(items,now).flatMap(g=>g.items.map(c=>c.id)),['unread-old','unread-new','read-new','read-old']);
});

test('read and every feedback state share the same muted presentation',async()=>{
  const r=cardRuntime();
  assert.match((await r.render()).html,/is-unread/);
  assert.equal(r.run('muted.value'),false);
  for(const state of ['seen','known','resolved','dismissed','adopted','engaged','followed']){
    Object.assign(r.props.card,{seenAtMs:now,state});
    const {html}=await r.render();
    assert.match(html,/is-muted/);assert.doesNotMatch(html,/is-unread|class="rec-read"/);
  }
});

test('title and both text paragraphs toggle the same expansion without reading the card',async()=>{
  const r=cardRuntime();
  for(const cls of ['rec-story-title','rec-story-why','rec-story-body']){
    const {nodes}=await r.render();
    const node=nodes.find(n=>n.props?.class?.split(' ').includes(cls));
    const before=r.run('expanded.value');
    node.props.onClick({target:{closest:()=>null}});
    assert.equal(r.run('expanded.value'),!before);
  }
  assert.deepEqual(r.events,[]);
});

test('note opens below the fixed footer; typing and submitting do not activate a question',async()=>{
  const r=cardRuntime();
  let view=await r.render();
  view.nodes.find(n=>n.props?.['aria-controls']==='rec-note-card').props.onClick();
  view=await r.render();
  assert.ok(view.html.indexOf('</footer>')<view.html.indexOf('id="rec-note-card"'));
  assert.match(view.html,/<textarea/);
  assert.equal(r.run('noteOpen.value'),true);
  assert.deepEqual(r.events,[]);
  r.run('note.value="  我更关心手机使用体验  ";submitNote()');
  assert.equal(r.events.length,1);
  assert.deepEqual([r.events[0][0],r.events[0][2],r.events[0][3]],['feedback','note','我更关心手机使用体验']);
  assert.equal(r.run('noteOpen.value'),false);
});

function feedRuntime(){
  let database=Array.from({length:105},(_,i)=>({...sample,id:String(i).padStart(3,'0'),createdAtMs:now-i*1000}));
  const calls=[];
  const context=vm.createContext({...Vue,...config,console,
    defineEmits:()=>()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},watch:()=>{},useAdminPhone:()=>Vue.ref(false),
    crypto:globalThis.crypto,referenceToken,ElMessage:{error:()=>{},success:()=>{}},apiError:e=>String(e),
    Api:{recCards:async query=>{
      calls.push({...query});
      const items=config.groupFeed(database.filter(c=>!query.unread||!c.seenAtMs),now).flatMap(g=>g.items);
      return {items:structuredClone(items.slice(query.offset,query.offset+query.limit)),total:items.length};
    }},apiError:e=>String(e),setRecommendationUnseen:()=>{},
  });
  vm.runInContext(withoutImports(viewSfc.scriptSetup.content),context);
  return {calls,run:code=>vm.runInContext(code,context),read:ids=>{for(const c of database)if(ids.includes(c.id))c.seenAtMs=now;}};
}

test('pagination refills the ordered prefix after reads instead of skipping the next unread cards',async()=>{
  const r=feedRuntime();
  await r.run('loadCards()');assert.equal(r.run('cards.value.length'),40);
  const ids=['000','001','002','003','004'];r.read(ids);
  r.run(`markLocallyRead(${JSON.stringify(ids)})`);
  await r.run('loadCards({append:true})');
  assert.deepEqual(r.calls.at(-1),{limit:80,offset:0,lane:undefined,unread:undefined});
  assert.equal(r.run('cards.value[0].id'),'005');
  assert.equal(r.run('cards.value[39].id'),'044');
  assert.equal(r.run('new Set(cards.value.map(c=>c.id)).size'),80);
  await r.run('loadCards({append:true})');
  assert.equal(r.run('cards.value.length'),105);
  assert.deepEqual(Array.from(r.run('cards.value.slice(-5).map(c=>c.id)')),ids);
});

test('mobile cards have five compact actions including direct copy and inspect never marks read',async()=>{
  const r=cardRuntime();r.props.mobile=true;r.props.lead=true;
  const view=await r.render();
  assert.match(view.html,/class="rec-mobile-card is-unread"/);
  assert.doesNotMatch(view.html,/rec-story-why|rec-story-asks|rec-story-foot|textarea/);
  const footer=view.nodes.find(n=>n.props?.class==='rec-mobile-actions');
  assert.equal(footer.children.filter(n=>n.type==='button').length,5);
  view.nodes.find(n=>n.props?.class==='rec-mobile-copy').props.onClick();
  assert.deepEqual([r.events[0][0],r.events[0][2]],['inspect','detail']);
  footer.children.find(n=>n.props?.class==='rec-mobile-chat').props.onClick();
  assert.deepEqual([r.events[1][0],r.events[1][2]],['inspect','chat']);
  footer.children.find(n=>n.props?.['aria-label']==='复制推荐引用').props.onClick();
  assert.deepEqual(r.events[2],['copy',r.props.card]);
  assert.equal(r.events.length,3);
  assert.equal(r.props.card.seenAtMs,null);
});

test('desktop copy stays outside the wrapping questions and feedback actions',async()=>{
  const r=cardRuntime();
  for(const lane of ['external','openbear']){
    r.props.card.lane=lane;
    const view=await r.render();
    const footer=view.nodes.find(n=>n.props?.class==='rec-story-foot');
    const actions=footer.children.find(n=>n.props?.class==='rec-foot-actions');
    const copy=footer.children.find(n=>n.props?.['aria-label']==='复制推荐引用');
    assert.ok(actions);assert.ok(copy);
    const asks=view.nodes.find(n=>n.props?.class==='rec-asks-list');
    assert.equal(walk([asks,actions]).some(n=>n.props?.['aria-label']==='复制推荐引用'),false);
    copy.props.onClick();
    assert.deepEqual(r.events.at(-1),['copy',r.props.card]);
    assert.equal(r.props.card.seenAtMs,null);
    assert.equal(r.run('noteOpen.value'),false);
  }
  assert.equal(r.events.length,2);
});

test('mobile panel is stable across read regrouping and closes only after successful actions',async()=>{
  const r=feedRuntime();await r.run('loadCards();');
  r.run('inspectMobile(cards.value[0],"process");markLocallyRead([cards.value[0].id]);');
  assert.equal(r.run('mobileCard.value.id'),'000');
  await r.run('finishMobileAction(async()=>false,cards.value[0])');
  assert.equal(r.run('mobileSheet.id'),'000');
  await r.run('finishMobileAction(async()=>true,cards.value[0])');
  assert.equal(r.run('mobileSheet.id'),'');
  r.run('inspectMobile(cards.value[0],"process")');
  await r.run('finishMobileAction(async()=>{mobileSheet.id="001";return true;},cards.value[0])');
  assert.equal(r.run('mobileSheet.id'),'001');
});

test('failed feedback preserves the open panel and unread state',async()=>{
  const r=feedRuntime();await r.run('loadCards()');
  r.run('inspectMobile(cards.value[0],"process");Api.recFeedback=async()=>{throw new Error("offline")};');
  await r.run('finishMobileAction(feedback,cards.value[0],"note","保留我的草稿")');
  assert.equal(r.run('mobileSheet.id'),'000');
  assert.equal(r.run('mobileCard.value.seenAtMs'),null);
  assert.equal(r.run('busyCard.value'),'');
});

test('unread-only pagination also refills after the loaded rows are read',async()=>{
  const r=feedRuntime();r.run('unreadOnly.value=true');
  await r.run('loadCards()');
  const ids=Array.from({length:40},(_,i)=>String(i).padStart(3,'0'));r.read(ids);
  r.run('markLocallyRead(true)');assert.equal(r.run('groups.value.length'),0);
  await r.run('loadCards({append:true})');
  assert.equal(r.run('cards.value[0].id'),'040');
  assert.equal(r.run('cards.value.length'),65);
  assert.equal(r.run('cardsTotal.value'),65);
});

test('desktop card shows the to-check list with the expanded text and keeps the mobile list row unchanged',async()=>{
  const short={body:'短。',toCheck:['我们的计时是否把命令执行时间也算进了空转？']};
  const r=cardRuntime(short);
  assert.equal(r.run('long.value'),true);
  assert.doesNotMatch((await r.render()).html,/rec-story-check/);
  r.run('toggle')({target:{closest:()=>null}});
  const html=(await r.render()).html;
  assert.match(html,/class="rec-story-check"/);assert.ok(html.includes(short.toCheck[0]));
  r.props.mobile=true;
  const mobile=(await r.render()).html;
  assert.doesNotMatch(mobile,/rec-story-check|rec-sheet-check/);assert.ok(!mobile.includes(short.toCheck[0]));
});
