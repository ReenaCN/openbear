import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chainOptions,normalizeChain,composerText,finalAction,groupByDay,askedQuestions} from './recConfig.js';
import {normalizeReference,referenceToken,referenceFromUrl} from '../../references/codec.js';

const protocols={
  responses:[{key:'openai/deepseek',label:'DeepSeek'},{key:'openai/glm',label:'GLM'}],
  anthropic:[{key:'claude/opus',label:'Opus'}],
};

test('fallback models are limited to the first model protocol',()=>{
  assert.deepEqual(chainOptions(protocols,['openai/deepseek',''],1).map(m=>m.key),['openai/glm']);
  assert.equal(chainOptions(protocols,[],0).length,3);
  assert.deepEqual(normalizeChain(protocols,['openai/deepseek','claude/opus','openai/glm','openai/glm']),['openai/deepseek','openai/glm']);
});

test('a card becomes a rec reference plus the chosen question in the composer',()=>{
  const id='0f0e0d0c-1111-2222-3333-444455556666';
  const token=referenceToken({kind:'rec',id,label:'GraphRAG 2.0 发布'});
  assert.equal(token,`[GraphRAG 2.0 发布](openbear://ref/rec/${id})`);
  assert.equal(referenceFromUrl(`openbear://ref/rec/${id}`,'x').kind,'rec');
  assert.equal(normalizeReference({kind:'rec',id:'../../etc',label:'x'}),null);
  assert.equal(composerText(token,'它怎么做到的'),`${token} 它怎么做到的`);
  assert.equal(composerText(token,''),token);
});

test('feedback presentation and day grouping',()=>{
  assert.equal(finalAction({feedback:[{action:'ask'},{action:'known'},{action:'resolved'}]}),'resolved');
  assert.equal(finalAction({feedback:[{action:'follow'}]}),'');
  assert.deepEqual([...askedQuestions({feedback:[{action:'ask',detail:'a'},{action:'note',detail:'b'}]})],['a']);
  const now=Date.UTC(2026,9,9,6);
  const groups=groupByDay([{createdAtMs:now},{createdAtMs:now-3600e3},{createdAtMs:now-86400e3}],now);
  assert.deepEqual(groups.map(g=>[g.label,g.items.length]),[['今天',2],['昨天',1]]);
});

test('continue only fills the composer; it never sends',()=>{
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  const app=readFileSync(new URL('../../App.vue',import.meta.url),'utf8');
  const consoleView=readFileSync(new URL('../../views/consoleView/ConsoleView.vue',import.meta.url),'utf8');
  assert.match(view,/emit\('open-recommendation-draft'/);
  assert.doesNotMatch(view,/sendMessage|Api\.send/);
  const apply=consoleView.slice(consoleView.indexOf('async function applyRecommendationDraft'),consoleView.indexOf('watch(() => [props.recommendationDraft'));
  assert.match(apply,/draft\.value = String\(request\.text/);
  assert.doesNotMatch(apply,/send\(|submit|Api\./);
  assert.match(app,/data-testid="recommendations-entry"[\s\S]*@click="selectNav\('recommendations'\)"/);
  assert.match(app,/key: 'recommendations'[^\n]*headerOnly: true/);
});

test('a cold load of /recommendations is not redirected into a conversation by the tree',()=>{
  const app=readFileSync(new URL('../../App.vue',import.meta.url),'utf8');
  const tree=readFileSync(new URL('../ConversationTree.vue',import.meta.url),'utf8');
  assert.match(app,/:auto-open-selected="active !== 'recommendations'"/);
  const branch=tree.slice(tree.indexOf('if (!props.autoOpenSelected)'),tree.indexOf('emit("new-conversation", "");')+1);
  assert.ok(branch.indexOf('autoOpenSelected')<branch.indexOf('emit("open"'),'the guard precedes both automatic navigations');
});

test('settings: dirty tracking ignores empty slots, the master switch saves on its own',async()=>{
  const {settingsDirty}=await import('./recConfig.js');
  const saved={enabled:false,models:['openai/deepseek'],dailyBudgetUsd:null,initialDays:7};
  assert.equal(settingsDirty(saved,{...saved,models:['openai/deepseek','']},protocols),false);
  assert.equal(settingsDirty(saved,{...saved,enabled:true},protocols),false);
  assert.equal(settingsDirty(saved,{...saved,dailyBudgetUsd:0},protocols),false);
  assert.equal(settingsDirty(saved,{...saved,initialDays:8},protocols),true);
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  assert.match(view,/class="rec-savebar"/);
  assert.match(view,/@update:model-value="toggleEnabled"/);
  // Saving never waits for the overview refresh.
  const save=view.slice(view.indexOf('async function saveSettings'),view.indexOf('async function toggleEnabled'));
  assert.match(save,/void loadOverview\(\)/);
  assert.doesNotMatch(save,/await loadOverview\(\)/);
});

test('a run without a conversation never emits an empty open-conversation',()=>{
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  const open=view.slice(view.indexOf('function openRun'),view.indexOf('async function ask'));
  assert.match(open,/if\(runLinkable\(run\)\)emit\('open-conversation'/);
});

test('recommendation tool calls and trigger messages are presented in Chinese',async()=>{
  const {toolActionSummary,toolDisplayLabel}=await import('../../views/consoleView/toolCallPresentation.js');
  assert.equal(toolDisplayLabel('Recommendation'),'推荐');
  assert.equal(toolActionSummary('Recommendation',{action:'profile.upsert',params:{kind:'ongoing',topic:'OpenBear 推荐',status:'active'}}),'记录画像：进行中的事 · “OpenBear 推荐” · 进行中');
  assert.equal(toolActionSummary('Recommendation',{action:'source.next'}),'读取下一批对话材料');
  const list=readFileSync(new URL('../../views/consoleView/TurnList.vue',import.meta.url),'utf8');
  assert.match(list,/RecommendationMessageCard v-else-if="turn\.user\.source === 'recommendation'"/);
});

test('entry shows an unseen badge; mobile chat header surfaces new recommendations',()=>{
  const app=readFileSync(new URL('../../App.vue',import.meta.url),'utf8');
  assert.match(app,/class="rec-entry-badge"/);
  assert.match(app,/data-testid="mobile-recommendations-entry"/);
  assert.match(app,/startRecommendationBadge\(\)/);
});

test('cards are read only by an explicit action, never by being displayed',()=>{
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  const card=readFileSync(new URL('./RecCard.vue',import.meta.url),'utf8');
  assert.doesNotMatch(view,/scheduleSeen|seenTimer/);
  assert.match(view,/async function markAllRead[\s\S]*Api\.recSeen\(\{all:true\}\)/);
  assert.match(view,/@read="markRead"/);
  assert.match(card,/class="rec-read"[^>]*@click="emit\('read',card\)"/);
  // Feedback actions are on the card, not hidden in a menu; the origin shows its title.
  assert.doesNotMatch(card,/el-dropdown/);
  assert.match(card,/card\.originTitle/);
  // The continue-location line (folder · model) is no longer shown on the card.
  assert.doesNotMatch(card,/续聊位置|location\.model/);
});

test('follows are matters; repo/page checks are optional signals attached to them',async()=>{
  const {groupFollows,sourceLabel,leadCardId}=await import('./recConfig.js');
  const grouped=groupFollows([{id:'1',kind:'topic',topic:'主动式 Agent',cardId:'c'},{id:'2',kind:'github_release',topic:'主动式 Agent',target:'a/b',cardId:'c'},{id:'3',kind:'npm',topic:'vite',target:'vite'}]);
  assert.deepEqual(grouped.map(g=>[g.id,g.signals.length]),[['1',1],['3',0]]);
  assert.equal(sourceLabel({url:'https://arxiv.org/abs/2607.08716',title:'https://arxiv.org/abs/2607.08716'}),'arxiv.org/abs/2607.08716');
  assert.equal(leadCardId([{items:[{id:'a',priority:0},{id:'b',priority:2}]}]),'b');
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  assert.match(view,/Api\.recAddFollow\(\{kind:'topic'/);
});

test('organizing is a background job: the page has no manual run trigger',()=>{
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  const api=readFileSync(new URL('../../api.js',import.meta.url),'utf8');
  assert.doesNotMatch(view,/立即整理|recStartRun/);
  assert.doesNotMatch(api,/recStartRun/);
  assert.match(view,/stopRun\(activeRun\)/);
});

test('prompts are editable in settings; empty means the built-in default',async()=>{
  const {settingsDirty}=await import('./recConfig.js');
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  assert.match(view,/field:'promptSystem'[\s\S]*field:'promptDaily'[\s\S]*field:'promptFollow'/);
  assert.match(view,/String\(value\|\|''\)\.trim\(\)===promptDefault\.value\.trim\(\)\?'':value/);
  assert.match(view,/<AdaptiveMdEditor[^>]*v-model="promptText"/);
  assert.equal(settingsDirty({promptSystem:'',models:[]},{promptSystem:'改过',models:[]},{}),true);
});

test('deleted conversations are not rendered as links; managed folder cannot be deleted from the tree',()=>{
  const card=readFileSync(new URL('./RecCard.vue',import.meta.url),'utf8');
  const view=readFileSync(new URL('../../views/RecommendationsView.vue',import.meta.url),'utf8');
  const tree=readFileSync(new URL('../ConversationTree.vue',import.meta.url),'utf8');
  assert.match(card,/originExists!==false/);
  assert.doesNotMatch(card,/v-if="card\.originConversationUuid"/);
  assert.match(view,/function runLinkable\(run\)\{return Boolean\(run\?\.conversationUuid\)&&run\.conversationExists!==false;\}/);
  assert.match(view,/sourceExists!==false/);
  assert.match(view,/overview\.models\.unavailable/);
  assert.match(tree,/:disabled="menu\.row\?\.managedBy === 'recommendations'"/);
  assert.match(tree,/class="node-managed"/);
});


test('saving or disabling a chain preserves healthy fallback after the head was removed',()=>{
  const saved=['openai/removed','openai/glm'];
  assert.deepEqual(normalizeChain(protocols,saved),saved);
  assert.deepEqual(normalizeChain(protocols,['openai/removed','also/removed']),['openai/removed','also/removed']);
  assert.deepEqual(normalizeChain(protocols,['openai/removed','openai/glm','claude/opus']),saved);
  assert.deepEqual(chainOptions(protocols,saved,2).map(m=>m.key),['openai/deepseek']);
});
