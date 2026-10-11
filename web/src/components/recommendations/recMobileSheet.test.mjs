import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import postcss from 'postcss';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {ElInput,ElIcon} from 'element-plus';
import * as Icons from '@element-plus/icons-vue';
import * as config from './recConfig.js';

const sfc=parse(readFileSync(new URL('./RecMobileSheet.vue',import.meta.url),'utf8')).descriptor;
const sheetCss=postcss.parse(readFileSync(new URL('./recommendations.mobile.css',import.meta.url),'utf8'));
function sheetStyle(selector,{dvh=true}={}){
  const result={};
  sheetCss.walkRules(rule=>{if(rule.selectors.includes(selector))rule.walkDecls(d=>{if(dvh||!d.value.includes('dvh'))result[d.prop]=d.value;});});
  return result;
}
// Evaluate declarations, not browser layout; dvh deliberately stays unchanged by the keyboard.
function cssValue(expression,{height=430,top=35,layoutHeight=844,safeTop=47,safeBottom=34,viewport=true}={}){
  const vars=viewport?{'--mobile-viewport-height':`${height}px`,'--mobile-viewport-top':`${top}px`}:{};
  const safe={'safe-area-inset-top':safeTop,'safe-area-inset-bottom':safeBottom};
  const text=expression.replace(/var\(([^,]+),\s*([^)]+)\)/g,(_,name,fallback)=>vars[name]??fallback)
    .replace(/env\(([^,]+),\s*[^)]+\)/g,(_,name)=>safe[name])
    .replace(/(\d+(?:\.\d+)?)(?:dvh|vh|%)/g,(_,n)=>Number(n)*layoutHeight/100)
    .replace(/px/g,'').replace(/calc\(/g,'(').replace(/\b(max|min)\(/g,'Math.$1(');
  assert.match(text,/^[\d\s.+*/(),\-a-zA-Z]+$/);
  return vm.runInNewContext(text);
}
const base={id:'mobile',lane:'external',category:'adjacent',createdAtMs:1,seenAtMs:null,title:'完整标题'.repeat(10),body:'完整正文\n'.repeat(200),reason:'推荐理由'.repeat(70),sources:[{url:'https://example.com/research',title:'完整来源'}],questions:['第一个问题','另一个完整问题'],feedback:[],originConversationUuid:'origin',originTitle:'原会话',originExists:true};
const walk=nodes=>(nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[]),...walk(n.component?.subTree?[n.component.subTree]:[])]);
function runtime(mode='detail'){
  const props=Vue.reactive({card:{...base},mode,busy:false}),events=[];
  const context=vm.createContext({...Vue,...config,...Icons,defineProps:()=>props,defineEmits:()=>(...args)=>events.push(args)});
  vm.runInContext(sfc.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm,''),context);
  const run=code=>vm.runInContext(code,context);
  const scope=Vue.proxyRefs({...config,...Icons,...run('({props,card:computed(()=>props.card),mode:computed(()=>props.mode),busy:computed(()=>props.busy),emit,note,content,unread,asked,done,notes,originLinkable,isOpenBear,following,actions,title,submitNote,dragging,closing,swipeStyle,startSwipe,moveSwipe,endSwipe,cancelSwipe,closeSheet})')});
  async function render(){
    let tree,slotTree;const compiled=Vue.compile(sfc.template.content);
    const app=Vue.createSSRApp({render(){tree=compiled.call(this,scope,[]);return tree;}});
    const DrawerSlots={props:['modelValue','title','withHeader','direction','size','appendToBody'],emits:['close','closed'],render(){return slotTree=Vue.h('section',[...(this.$slots.default?.()||[]),...(this.$slots.footer?.()||[])]);}};
    for(const [name,component] of Object.entries({ElDrawer:DrawerSlots,ElInput,ElIcon,...Icons}))app.component(name,component);
    app.config.warnHandler=message=>assert.fail(message);
    return {html:await renderToString(app),nodes:walk([slotTree||tree]),drawer:tree};
  }
  return {props,events,run,render};
}

function touchEvent(x,y,{region='header',interactive=false,count=1,cancelable=true}={}){
  return {touches:Array.from({length:count},(_,identifier)=>({identifier,clientX:x,clientY:y})),cancelable,prevented:false,
    target:{closest:selector=>selector==='.rec-sheet-content'?(region==='content'?{}:null):((interactive||region==='footer')?{}:null)},
    preventDefault(){this.prevented=true;}};
}

test('the body-level sheet tracks the visual viewport once and fits above the keyboard',async()=>{
  const {drawer}=await runtime('process').render();
  assert.equal(drawer.props.direction,'btt');
  assert.ok(Object.hasOwn(drawer.props,'append-to-body'));
  assert.equal(drawer.props['modal-class'],'rec-mobile-sheet-overlay');
  const overlay=sheetStyle('.rec-mobile-sheet-overlay');
  assert.equal(overlay.bottom,'auto');assert.equal(overlay.transform,undefined);
  for(const dvh of [true,false]){
    const panel=sheetStyle('.rec-mobile-drawer.el-drawer',{dvh});
    assert.equal(panel.transform,undefined);assert.equal(panel.bottom,undefined);
    for(const env of [
      {height:844,top:0,layoutHeight:844,safeTop:47},
      {height:430,top:35,layoutHeight:844,safeTop:47},
      {height:300,top:20,layoutHeight:667,safeTop:0},
      {height:190,top:16,layoutHeight:390,safeTop:0},
      {height:390,top:0,layoutHeight:390,safeTop:0},
    ]){
      const top=cssValue(overlay.top,env),height=cssValue(overlay.height,env);
      const maxHeight=cssValue(panel['max-height'],env);
      assert.equal(top,env.top);assert.equal(height,env.height);
      assert.equal(top+height,env.top+env.height,'bottom edge follows the visible keyboard boundary');
      assert.equal(maxHeight,Math.min(env.layoutHeight*.88,env.height-Math.max(12,env.safeTop)));
      assert.ok(top+height-maxHeight>=env.top+Math.max(12,env.safeTop));
    }
    const fallback={viewport:false,layoutHeight:844};
    assert.equal(cssValue(overlay.top,fallback),0);
    assert.equal(cssValue(overlay.height,fallback),844);
    assert.equal(cssValue(panel['max-height'],fallback),844*.88);
  }
});

test('keyboard state removes only the extra footer safe area and preserves normal spacing',()=>{
  const footer=sheetStyle('.rec-mobile-drawer .el-drawer__footer');
  const keyboard=sheetStyle('html[data-openbear-keyboard] .rec-mobile-drawer .el-drawer__footer');
  assert.match(footer.padding,/^11px 16px /);
  const normal=footer.padding.replace(/^11px 16px /,'');
  for(const safeBottom of [0,21,34]){
    for(const open of [false,true,false]){
      assert.equal(cssValue(open?keyboard['padding-bottom']:normal,{safeBottom}),open?13:Math.max(13,safeBottom));
    }
  }
  const body=sheetStyle('.rec-mobile-drawer .el-drawer__body'),content=sheetStyle('.rec-sheet-content');
  assert.equal(body['min-height'],'0');assert.equal(body.overflow,'hidden');assert.equal(body.padding,'0');
  assert.equal(content['min-height'],'0');assert.equal(content.overflow,'auto');
  assert.equal(content['overscroll-behavior'],'contain');assert.equal(footer.flex,'none');
  assert.equal(sheetStyle('.rec-sheet-header').flex,'none');
  assert.equal(sheetStyle('.rec-sheet-note .el-textarea__inner')['font-size'],'16px');
});

test('downward drag preserves its release position and waits for the exit animation before unmounting',async()=>{
  const r=runtime();const {drawer}=await r.render();
  r.run('content.value={scrollTop:240}');
  drawer.props.onTouchstart(touchEvent(100,150));
  const move=touchEvent(104,250);drawer.props.onTouchmove(move);
  assert.equal(move.prevented,true);
  assert.equal(r.run('swipeStyle.value.transform'),'translateY(100px)');
  assert.equal(r.run('dragging.value'),true);
  drawer.props.onTouchend();drawer.props.onTouchend();
  assert.equal(r.run('closing.value'),true);
  assert.equal(r.run('dragging.value'),false);
  assert.equal(r.run('swipeStyle.value["--rec-sheet-drag-y"]'),'100px');
  assert.equal(r.run('swipeStyle.value.transform'),undefined);
  assert.deepEqual(r.events,[]);
  const closingDrawer=(await r.render()).drawer;
  assert.equal(closingDrawer.props['model-value'],false);
  closingDrawer.props.onClose();
  closingDrawer.props.onTouchstart(touchEvent(100,150));
  closingDrawer.props.onTouchcancel();
  assert.equal(r.run('dragOffset.value'),100);
  assert.deepEqual(r.events,[]);
  closingDrawer.props.onClosed();
  assert.deepEqual(r.events,[['close']]);
});

test('close button and native backdrop or Escape closure wait for the same animation',async()=>{
  for(const trigger of ['button','native']){
    const r=runtime();const view=await r.render();
    if(trigger==='button')view.nodes.find(n=>n.props?.['aria-label']==='关闭').props.onClick();
    else view.drawer.props.onClose();
    assert.equal(r.run('closing.value'),true);assert.deepEqual(r.events,[]);
    view.drawer.props.onClosed();assert.deepEqual(r.events,[['close']]);
  }
});

test('a short pull or touch cancellation resets the panel without closing',()=>{
  for(const finish of ['endSwipe','cancelSwipe']){
    const r=runtime();
    r.run('startSwipe')(touchEvent(100,150));
    r.run('moveSwipe')(touchEvent(100,finish==='cancelSwipe'?280:190));
    r.run(finish)();
    assert.equal(r.run('dragOffset.value'),0);
    assert.equal(r.run('dragging.value'),false);
    assert.equal(r.run('swipeStyle.value'),undefined);
    assert.deepEqual(r.events,[]);
  }
});

test('content can dismiss only when the downward gesture starts at the top',()=>{
  for(const scrollTop of [0,200]){
    const r=runtime();r.run(`content.value={scrollTop:${scrollTop}}`);
    r.run('startSwipe')(touchEvent(100,250,{region:'content'}));
    // Reaching the top during a native scroll does not turn it into a dismissal.
    r.run('content.value.scrollTop=0');
    const move=touchEvent(100,370,{region:'content'});r.run('moveSwipe')(move);r.run('endSwipe')();
    assert.equal(move.prevented,scrollTop===0);
    assert.equal(r.run('closing.value'),scrollTop===0);
    assert.deepEqual(r.events,[]);
  }
});

test('upward and sideways reading gestures remain native even if the finger then moves down',()=>{
  for(const [x,y] of [[100,120],[160,160]]){
    const r=runtime();r.run('content.value={scrollTop:0}');
    r.run('startSwipe')(touchEvent(100,150,{region:'content'}));
    const first=touchEvent(x,y,{region:'content'}),second=touchEvent(100,300,{region:'content'});
    r.run('moveSwipe')(first);r.run('moveSwipe')(second);r.run('endSwipe')();
    assert.equal(first.prevented,false);assert.equal(second.prevented,false);
    assert.deepEqual(r.events,[]);
  }
});

test('controls, footer, multiple fingers and browser-owned gestures never dismiss',()=>{
  for(const options of [{interactive:true},{region:'footer'},{count:2},{cancelable:false}]){
    const r=runtime();
    r.run('startSwipe')(touchEvent(100,150,options));
    const move=touchEvent(100,280,options);r.run('moveSwipe')(move);r.run('endSwipe')();
    assert.equal(move.prevented,false);assert.deepEqual(r.events,[]);
  }
  const r=runtime();r.run('startSwipe')(touchEvent(100,150));
  r.run('moveSwipe')(touchEvent(100,280));
  r.run('moveSwipe')(touchEvent(100,300,{count:2}));r.run('endSwipe')();
  assert.equal(r.run('dragOffset.value'),0);assert.deepEqual(r.events,[]);
});

test('switching the panel mode resets an unfinished pull',async()=>{
  const r=runtime();r.run('startSwipe')(touchEvent(100,150));r.run('moveSwipe')(touchEvent(100,280));
  r.props.mode='chat';await Vue.nextTick();r.run('endSwipe')();
  assert.equal(r.run('dragOffset.value'),0);assert.deepEqual(r.events,[]);
});

test('detail retains full title, reason, body and sources and does not mark read on opening',async()=>{
  const r=runtime();const view=await r.render();
  assert.ok(view.html.includes(base.title));assert.ok(view.html.includes(base.reason));assert.ok(view.html.includes(base.body));
  assert.match(view.html,/href="https:\/\/example.com\/research"/);
  assert.match(view.html,/class="rec-sheet-origin"/);
  assert.deepEqual(r.events,[]);
  r.props.card.originExists=false;
  assert.doesNotMatch((await r.render()).html,/class="rec-sheet-origin"/);
  assert.doesNotMatch(view.html,/rec-sheet-check/);
  r.props.card.toCheck=['我们的计时是否把命令执行时间也算进了空转？'];
  const checked=(await r.render()).html;
  assert.match(checked,/class="rec-sheet-check"/);assert.ok(checked.includes(r.props.card.toCheck[0]));
});

test('questions and custom ask emit exactly one draft request, never a send',async()=>{
  const r=runtime('chat');const view=await r.render();
  const group=view.nodes.find(n=>n.props?.class==='rec-sheet-questions');
  const buttons=walk([group]).filter(n=>n.type==='button');
  assert.equal(buttons.length,3);
  buttons[0].props.onClick();
  assert.deepEqual([r.events[0][0],r.events[0][2]],['ask','第一个问题']);
  buttons.at(-1).props.onClick();
  assert.deepEqual([r.events[1][0],r.events[1][2]],['ask','']);
  assert.match(view.html,/不会自动发送/);
  assert.doesNotMatch(view.html,/<textarea/);
});

test('chat footer copies the card reference without asking or closing',async()=>{
  const r=runtime('chat');const view=await r.render();
  assert.match(view.html,/返回推荐<\/button><button[^>]*>.*复制到已有会话<\/button>/s);
  const buttons=view.nodes.filter(n=>n.type==='button');
  buttons.at(-1).props.onClick();
  assert.deepEqual(r.events.map(e=>e[0]),['copy']);
  assert.equal(r.events[0][1].id,base.id);
});
test('note submits feedback only, retains draft until success, and guards busy or blank submits',async()=>{
  const r=runtime('process');const view=await r.render();
  assert.match(view.html,/<textarea/);assert.doesNotMatch(view.html,/rec-sheet-questions/);
  r.run('note.value="  这条我更想看迁移限制  ";submitNote()');
  assert.equal(r.events.length,1);
  assert.deepEqual([r.events[0][0],r.events[0][2],r.events[0][3]],['feedback','note','这条我更想看迁移限制']);
  assert.equal(r.run('note.value'),'  这条我更想看迁移限制  ');
  r.props.busy=true;r.run('submitNote()');assert.equal(r.events.length,1);
  r.props.busy=false;r.run('note.value="  ";submitNote()');assert.equal(r.events.length,1);
  r.props.card={...r.props.card,id:'next'};await Vue.nextTick();assert.equal(r.run('note.value'),'');
});

test('OpenBear and external cards retain their own feedback actions and follow state',async()=>{
  const r=runtime('process');
  r.props.card.state='followed';assert.match((await r.render()).html,/取消关注/);
  r.props.card={...base,lane:'openbear'};
  const view=await r.render();
  assert.match(view.html,/已采纳/);assert.match(view.html,/不需要/);
  assert.doesNotMatch(view.html,/不感兴趣|持续关注|取消关注/);
});
