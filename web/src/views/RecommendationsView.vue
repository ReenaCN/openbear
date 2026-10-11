<script setup>
import {computed,onBeforeUnmount,onMounted,reactive,ref,watch} from 'vue';
import {ElMessage,ElMessageBox} from 'element-plus';
import {Check,Delete,EditPen,Plus,Search,View,Link,ArrowDown,Refresh} from '@element-plus/icons-vue';
import {Api,apiError} from '../api.js';
import AdminPageHeader from '../components/AdminPageHeader.vue';
import RecCard from '../components/recommendations/RecCard.vue';
import RecMobileSheet from '../components/recommendations/RecMobileSheet.vue';
import {useAdminPhone} from '../adminViewport.js';
import AdaptiveMdEditor from '../components/AdaptiveMdEditor.vue';
import {referenceToken} from '../references/codec.js';
import {copyTextToClipboard} from '../utils/clipboard.js';
import {setRecommendationUnseen} from '../components/recommendations/recBadge.js';
import {TABS,LANES,PROFILE_KINDS,PROFILE_STATUS,SIGNALS,FOLLOW_KINDS,FOLLOW_KIND_META,RUN_KINDS,RUN_KIND_HINTS,RUN_TRIGGERS,RUN_STATUS,NOTIFY_MODES,SCOPE_MODES,CATEGORY_LABELS,
  groupFeed,dayKey,timeText,clockText,money,compactTokens,chainOptions,normalizeChain,priceText,composerText,modelShort,followUrl,settingsDirty,
  leadCardId,categoryCounts,digestDate,greeting,groupFollows,NETWORK_TOOL_ROLES,networkToolOptions,networkToolWarning} from '../components/recommendations/recConfig.js';
import '../components/recommendations/recommendations.css';
import '../components/recommendations/recommendations.mobile.css';

const emit=defineEmits(['mobile-header-ready','open-conversation','open-recommendation-draft']);
const tab=ref('cards'),lane=ref(''),overview=ref(null),cards=ref([]),cardsTotal=ref(0),loadingCards=ref(false),busyCard=ref(''),error=ref('');
const unreadOnly=ref(false),markingAll=ref(false);
const isPhone=useAdminPhone(),mobileSheet=reactive({id:'',mode:'detail'});
const mobileCard=computed(()=>cards.value.find(card=>card.id===mobileSheet.id)||null);
function inspectMobile(card,mode){mobileSheet.id=card.id;mobileSheet.mode=mode;}
async function finishMobileAction(action,card,...args){
  if(await action(card,...args)&&mobileSheet.id===card.id)mobileSheet.id='';
}
function openMobileOrigin(uuid){mobileSheet.id='';emit('open-conversation',uuid);}
watch([isPhone,tab],()=>{mobileSheet.id='';});
const follows=ref([]),profile=ref([]),runs=ref([]),settingsData=ref(null),settingsForm=reactive({}),savingSettings=ref(false),togglingEnabled=ref(false);
const profileDialog=reactive({open:false,saving:false,item:null,form:{}});
const profileFilter=ref(''),profileQuery=ref(''),expanded=ref(new Set());
const followForm=reactive({topic:'',note:'',signalOpen:false,kind:'github_release',target:''}),addingFollow=ref(false);
let pollTimer=0,disposed=false,cardsRequest=0,cardsOrderRevision=0,loadedOrderRevision=0;

const activeRun=computed(()=>(overview.value?.runs||[]).find(run=>['starting','running'].includes(run.status)));
const lastRun=computed(()=>(overview.value?.runs||[])[0]);
const groups=computed(()=>groupFeed(unreadOnly.value?cards.value.filter(card=>!card.seenAtMs):cards.value));
const leadId=computed(()=>lane.value||unreadOnly.value?'':leadCardId(groups.value.filter(group=>!group.read)));
const todayMix=computed(()=>categoryCounts(cards.value.filter(card=>dayKey(card.createdAtMs)===dayKey(Date.now()))).slice(0,4));
const newCount=computed(()=>Number(overview.value?.unseen ?? cards.value.filter(card=>!card.seenAtMs).length));

async function loadOverview(){
  try{overview.value=await Api.recOverview();setRecommendationUnseen(overview.value?.unseen);}
  catch(e){error.value=apiError(e);}
}
async function loadCards({append=false}={}){
  const request=++cardsRequest,revision=cardsOrderRevision;
  const query={lane:lane.value||undefined,unread:unreadOnly.value?1:undefined};
  const target=append?cards.value.length+40:40;
  // A read/feedback moves rows across the page boundary. Refill the loaded prefix
  // before extending it, rather than skipping unseen rows with a stale offset.
  let offset=append&&revision===loadedOrderRevision?cards.value.length:0;
  const items=offset?[...cards.value]:[];
  let total=cardsTotal.value;
  loadingCards.value=true;
  try{
    do{
      const data=await Api.recCards({...query,limit:Math.min(100,target-offset),offset});
      if(disposed||request!==cardsRequest)return;
      items.push(...data.items);total=data.total;offset+=data.items.length;
      if(!data.items.length)break;
    }while(offset<target&&offset<total);
    cards.value=items;cardsTotal.value=total;loadedOrderRevision=revision;
  }catch(e){if(request===cardsRequest)error.value=apiError(e);}
  finally{if(request===cardsRequest)loadingCards.value=false;}
}
// Read is explicit: a card stays unread until its 已读 button, 全部已读, or an action on it.
function markLocallyRead(ids){
  const now=Date.now();
  for(const card of cards.value)if(!card.seenAtMs&&(ids===true||ids.includes(card.id))){
    card.seenAtMs=now;cardsOrderRevision++;
  }
}
async function markRead(card){
  busyCard.value=card.id;
  try{const data=await Api.recSeen({ids:[card.id]});markLocallyRead([card.id]);setUnseen(data.unseen);return true;}
  catch(e){ElMessage.error(apiError(e));}
  finally{busyCard.value='';}
}
async function markAllRead(){
  markingAll.value=true;
  try{const data=await Api.recSeen({all:true});markLocallyRead(true);setUnseen(data.unseen);if(unreadOnly.value)await loadCards();}
  catch(e){ElMessage.error(apiError(e));}
  finally{markingAll.value=false;}
}
function setUnseen(value){
  setRecommendationUnseen(value);
  if(overview.value)overview.value={...overview.value,unseen:Number(value)||0};
}
async function loadFollows(){try{follows.value=(await Api.recFollows()).items;}catch(e){error.value=apiError(e);}}
async function loadProfile(){try{profile.value=(await Api.recProfile()).items;}catch(e){error.value=apiError(e);}}
async function loadRuns(){try{runs.value=(await Api.recRuns({limit:60})).items;}catch(e){error.value=apiError(e);}}
function applySettings(data){
  settingsData.value=data;
  for(const key of Object.keys(settingsForm))delete settingsForm[key];
  Object.assign(settingsForm,JSON.parse(JSON.stringify(data.settings)));
  if(!settingsForm.models.length)settingsForm.models=[''];
  settingsForm.networkTools??={search:[],fetch:[]};
  networkToolsError.value='';
}
async function loadSettings(){try{applySettings(await Api.recSettings());}catch(e){error.value=apiError(e);}}
async function loadTab(){
  error.value='';
  if(tab.value==='cards')await loadCards();
  else if(tab.value==='follows')await loadFollows();
  else if(tab.value==='profile')await loadProfile();
  else if(tab.value==='runs')await loadRuns();
  else if(tab.value==='settings')await loadSettings();
}
async function refresh(){await Promise.all([loadOverview(),loadTab()]);}
async function selectTab(key){
  if(key===tab.value)return;
  if(tab.value==='settings'&&dirty.value){
    try{await ElMessageBox.confirm('设置有修改还没保存，离开会丢掉这些修改。','未保存的设置',{type:'warning',confirmButtonText:'放弃修改',cancelButtonText:'留下来'});}
    catch{return;}
  }
  tab.value=key;
}
watch(tab,loadTab);
watch([lane,unreadOnly],()=>loadCards());
watch(activeRun,(run,previous)=>{
  clearInterval(pollTimer);
  if(run)pollTimer=setInterval(loadOverview,6000);
  else if(previous){loadTab();}
});

async function stopRun(run){try{await Api.recStopRun(run.id);await loadOverview();if(tab.value==='runs')await loadRuns();}catch(e){ElMessage.error(apiError(e));}}
function openRun(run){
  if(runLinkable(run))emit('open-conversation',run.conversationUuid);
  else ElMessage.info(run?.conversationUuid?'这次运行的会话已被删除':'这次运行还没有创建会话，稍后再看');
}

function runLinkable(run){return Boolean(run?.conversationUuid)&&run.conversationExists!==false;}
async function ask(card,question){
  busyCard.value=card.id;
  try{
    const prepared=await Api.recContinue(card.id,{question,requestId:crypto.randomUUID()});
    markLocallyRead([card.id]);void loadOverview();
    const text=composerText(referenceToken(prepared.reference),prepared.question);
    emit('open-recommendation-draft',{folderId:prepared.folderId||'',model:prepared.model||'',text});
    return true;
  }catch(e){ElMessage.error(apiError(e));}
  finally{busyCard.value='';}
}
// Only the reference travels; sending it from any conversation records the card feedback.
async function copyReference(card){
  try{
    await copyTextToClipboard(referenceToken({kind:'rec',id:card.id,label:card.title}));
    ElMessage.success('已复制推荐引用，粘贴到任意会话的输入框即可');
  }catch{ElMessage.error('复制失败，请在会话输入框里用 @ 引用这张推荐');}
}
async function feedback(card,action,detail=''){
  busyCard.value=card.id;
  try{
    const result=await Api.recFeedback(card.id,{action,detail,requestId:crypto.randomUUID()});
    markLocallyRead([card.id]);
    const index=cards.value.findIndex(item=>item.id===card.id);
    if(index>=0)cards.value[index]={...cards.value[index],...result.card,feedback:[...(cards.value[index].feedback||[]),{action,detail,atMs:Date.now()}]};
    void loadOverview();
    if(action==='follow')ElMessage.success('已关注这件事，之后每日整理会跟进它的后续');
    else if(action==='note')ElMessage.success('已记下，下次整理会按你的说明调整');
    return true;
  }catch(e){ElMessage.error(apiError(e));}
  finally{busyCard.value='';}
}
function openSource(card,url){void Api.recOpenSource(card.id,url).then(()=>{markLocallyRead([card.id]);void loadOverview();}).catch(()=>{});}

// ---------------------------------------------------------------- follows
// A follow is a MATTER the user cares about; repo/package/page checks are optional signals on it.
const followSignalMeta=computed(()=>FOLLOW_KIND_META[followForm.kind]);
const followGroups=computed(()=>groupFollows(follows.value));
async function removeFollow(item){
  try{await ElMessageBox.confirm(`不再关注“${item.topic||item.target}”？`,'取消关注',{type:'warning',confirmButtonText:'取消关注',cancelButtonText:'保留'});}catch{return;}
  try{
    for(const one of [item,...(item.signals||[])])await Api.recRemoveFollow(one.id);
    await loadFollows();
  }catch(e){ElMessage.error(apiError(e));}
}
async function addFollow(){
  const topic=followForm.topic.trim();
  if(!topic)return;
  addingFollow.value=true;
  try{
    await Api.recAddFollow({kind:'topic',target:topic,topic,note:followForm.note.trim()});
    const target=followForm.target.trim();
    if(followForm.signalOpen&&target)await Api.recAddFollow({kind:followForm.kind,target,topic});
    Object.assign(followForm,{topic:'',note:'',target:'',signalOpen:false});
    await loadFollows();ElMessage.success('已关注，之后每日整理会跟进它的后续');
  }catch(e){ElMessage.error(e?.response?.data?.message||apiError(e));}
  finally{addingFollow.value=false;}
}
async function editFollowNote(item){
  let value='';
  try{({value}=await ElMessageBox.prompt('想看这件事的哪方面？例如“有没有人复现”“国内能不能用”“价格变化”。留空表示都看。','关注重点',{inputValue:item.note||'',confirmButtonText:'保存',cancelButtonText:'取消',inputPlaceholder:'想看哪方面（可选）'}));}catch{return;}
  try{await Api.recAddFollow({kind:'topic',target:item.target,topic:item.topic,note:String(value||'').trim()});await loadFollows();}
  catch(e){ElMessage.error(e?.response?.data?.message||apiError(e));}
}
const followStats=computed(()=>({total:followGroups.value.length,changed:follows.value.filter(f=>f.pendingChange).length}));

// ---------------------------------------------------------------- profile
const profileCounts=computed(()=>Object.fromEntries(PROFILE_KINDS.map(kind=>[kind.key,profile.value.filter(item=>item.kind===kind.key).length])));
const profileGroups=computed(()=>{
  const query=profileQuery.value.trim().toLowerCase();
  return PROFILE_KINDS.filter(kind=>!profileFilter.value||kind.key===profileFilter.value)
    .map(kind=>({...kind,items:profile.value.filter(item=>item.kind===kind.key&&(!query||`${item.topic} ${item.detail}`.toLowerCase().includes(query)))}))
    .filter(group=>group.items.length);
});
const editedCount=computed(()=>profile.value.filter(item=>item.userEdited).length);
function statusLabel(key){return PROFILE_STATUS.find(s=>s.key===key)?.label||'';}
function signalMeta(key){return SIGNALS.find(s=>s.key===key)||SIGNALS[2];}
function toggleExpanded(id){const next=new Set(expanded.value);next.has(id)?next.delete(id):next.add(id);expanded.value=next;}
function editProfile(item=null,kind=''){
  profileDialog.item=item;
  profileDialog.form=item?{kind:item.kind,topic:item.topic,status:item.status,signal:item.signal,detail:item.detail,category:item.category}
    :{kind:kind||profileFilter.value||'interest',topic:'',status:'',signal:'explicit',detail:'',category:''};
  profileDialog.open=true;
}
async function saveProfile(){
  profileDialog.saving=true;
  try{
    const form={...profileDialog.form};
    if(form.kind!=='ongoing')form.status='';
    const body={...form,...(profileDialog.item?{expectedRevision:profileDialog.item.revision}:{})};
    await Api.recSaveProfile(profileDialog.item?.id||'',body);profileDialog.open=false;await loadProfile();
  }catch(e){ElMessage.error(e?.response?.data?.message||apiError(e));}
  finally{profileDialog.saving=false;}
}
async function quickStatus(item,status){
  try{
    await Api.recSaveProfile(item.id,{kind:item.kind,topic:item.topic,status,signal:item.signal,detail:item.detail,category:item.category,expectedRevision:item.revision});
    await loadProfile();
  }catch(e){ElMessage.error(e?.response?.data?.message||apiError(e));}
}
async function removeProfile(item){
  try{await ElMessageBox.confirm(`移除“${item.topic}”？推荐系统之后不会再按它推荐，也不会原样加回。`,'移除画像条目',{type:'warning',confirmButtonText:'移除',cancelButtonText:'取消'});}catch{return;}
  try{await Api.recRemoveProfile(item.id);await loadProfile();}catch(e){ElMessage.error(apiError(e));}
}

// ---------------------------------------------------------------- settings
const protocols=computed(()=>settingsData.value?.protocols||{});
const refreshingNetworkTools=ref(false),networkToolsError=ref('');
const networkOptions=computed(()=>settingsData.value?.networkToolOptions||[]);
function toolOptions(role){return networkToolOptions(networkOptions.value,settingsForm.networkTools?.[role]||[]);}
function toolWarning(role){return networkToolWarning(role.label,settingsForm.networkTools?.[role.key]||[],networkOptions.value);}
const networkLimited=computed(()=>NETWORK_TOOL_ROLES.some(role=>toolWarning(role)));
async function refreshNetworkTools(){
  refreshingNetworkTools.value=true;networkToolsError.value='';
  try{
    const data=await Api.recSettings();
    // Availability refresh must never discard unsaved selections or other form edits.
    settingsData.value={...settingsData.value,networkToolOptions:data.networkToolOptions||[]};
  }catch(e){networkToolsError.value=apiError(e);}
  finally{refreshingNetworkTools.value=false;}
}
const firstModel=computed(()=>Object.values(protocols.value).flat().find(model=>model.key===settingsForm.models?.[0]));
const dirty=computed(()=>Boolean(settingsData.value)&&settingsDirty(settingsData.value.settings,settingsForm,protocols.value));
function modelOptions(index){return chainOptions(protocols.value,settingsForm.models||[],index);}
function addFallback(){settingsForm.models=[...normalizeChain(protocols.value,settingsForm.models),''];}
function removeModel(index){settingsForm.models=settingsForm.models.filter((_,i)=>i!==index);if(!settingsForm.models.length)settingsForm.models=[''];}
function changeFirst(value){settingsForm.models=normalizeChain(protocols.value,[value,...settingsForm.models.slice(1)]);settingsForm.thinkingLevel='';}
const histogram=computed(()=>{const values=settingsData.value?.activity?.hourHistogram||[];const max=Math.max(1,...values);return values.map(value=>Math.round(value/max*100));});
function settingsBody(source){
  const body={...JSON.parse(JSON.stringify(source)),models:normalizeChain(protocols.value,source.models||[])};
  for(const key of ['dailyBudgetUsd','dailyBudgetTokens'])if(body[key]===''||body[key]===0||body[key]===undefined)body[key]=null;
  return body;
}
async function saveSettings(){
  savingSettings.value=true;
  try{
    applySettings(await Api.recSaveSettings({settings:settingsBody(settingsForm),expectedRevision:settingsData.value.revision}));
    ElMessage.success('设置已保存');
    void loadOverview();
  }catch(e){ElMessage.error(e?.response?.data?.message||apiError(e));}
  finally{savingSettings.value=false;}
}
// The master switch saves at once, on top of the last SAVED settings, so other
// unsaved edits stay as edits and the page never looks enabled when it is not.
async function toggleEnabled(value){
  const saved=settingsData.value.settings;
  if(value&&!normalizeChain(protocols.value,saved.models||[]).length){
    ElMessage.warning('先在下面选择模型并保存，再开启推荐');
    return;
  }
  togglingEnabled.value=true;
  try{
    const pending=JSON.parse(JSON.stringify(settingsForm));
    const data=await Api.recSaveSettings({settings:settingsBody({...saved,enabled:value}),expectedRevision:settingsData.value.revision});
    settingsData.value=data;
    Object.assign(settingsForm,{...pending,enabled:data.settings.enabled});
    ElMessage.success(value?'推荐已开启':'推荐已关闭');
    void loadOverview();
  }catch(e){ElMessage.error(e?.response?.data?.message||apiError(e));}
  finally{togglingEnabled.value=false;}
}
function discardSettings(){applySettings(settingsData.value);}
// Prompt editor: an empty value means "use the built-in default", so the editor shows the
// default text until the user changes it, and stores '' again when it matches the default.
const PROMPT_KEYS=[{key:'system',field:'promptSystem',label:'系统提示词',hint:'推荐模型的身份、判断标准和写卡片的要求。每日整理和跟随对话都用它。'},
  {key:'daily',field:'promptDaily',label:'每日整理任务',hint:'每天整理时发给模型的步骤：读对话、更新画像、处理关注、研究、写卡片、OpenBear 建议。'},
  {key:'follow',field:'promptFollow',label:'跟随对话任务',hint:'跟随正在聊的会话时发给模型的步骤。'}];
const promptTab=ref('system');
const promptMeta=computed(()=>PROMPT_KEYS.find(item=>item.key===promptTab.value));
const promptDefault=computed(()=>settingsData.value?.defaultPrompts?.[promptTab.value]||'');
const promptText=computed({
  get:()=>settingsForm[promptMeta.value.field]||promptDefault.value,
  set:value=>{settingsForm[promptMeta.value.field]=String(value||'').trim()===promptDefault.value.trim()?'':value;},
});
function promptCustomized(item){return Boolean(String(settingsForm[item.field]||'').trim());}
async function restorePrompt(){
  try{await ElMessageBox.confirm(`把“${promptMeta.value.label}”恢复为内置默认？你的修改会丢弃（保存后生效）。`,'恢复默认',{type:'warning',confirmButtonText:'恢复默认',cancelButtonText:'取消'});}catch{return;}
  settingsForm[promptMeta.value.field]='';
}
const folderOptions=computed(()=>(settingsData.value?.folders||[]).filter(folder=>folder.id!==settingsData.value?.folderId));
function beforeUnload(event){if(dirty.value){event.preventDefault();event.returnValue='';}}

onMounted(async()=>{window.addEventListener('beforeunload',beforeUnload);await refresh();});
onBeforeUnmount(()=>{disposed=true;clearInterval(pollTimer);window.removeEventListener('beforeunload',beforeUnload);});
</script>

<template>
  <div class="rec-root">
    <AdminPageHeader title="推荐" inline-actions subtitle="从你的对话出发，带回值得看的东西" description="点“接着聊”只把问题填进输入框，不会自动发送。" @mobile-header-ready="emit('mobile-header-ready',$event)">
      <template #mobile-navigation><slot name="mobile-navigation"/></template>
      <template #actions>
        <el-button :disabled="loadingCards" @click="refresh"><el-icon><Refresh/></el-icon>刷新</el-button>
        <el-button v-if="tab==='cards'" :disabled="markingAll||!newCount" @click="markAllRead"><el-icon><Check/></el-icon>全部已读</el-button>
      </template>
    </AdminPageHeader>
    <RecMobileSheet v-if="isPhone&&mobileCard" :card="mobileCard" v-model:mode="mobileSheet.mode" :busy="busyCard===mobileCard.id" @close="mobileSheet.id=''"
      @read="card=>finishMobileAction(markRead,card)" @ask="(card,question)=>finishMobileAction(ask,card,question)" @copy="copyReference"
      @feedback="(card,action,detail)=>finishMobileAction(feedback,card,action,detail)" @open-source="openSource" @open-origin="openMobileOrigin"/>
    <nav class="rec-tabs" role="tablist" aria-label="推荐页面">
      <button v-for="item in TABS" :key="item.key" type="button" role="tab" :aria-selected="tab===item.key" @click="selectTab(item.key)">
        {{ item.label }}<span v-if="item.key==='cards'&&newCount" class="rec-tab-count">{{ newCount }}</span>
        <span v-if="item.key==='settings'&&dirty" class="rec-tab-dot" aria-label="有未保存的修改"></span>
      </button>
    </nav>
    <div class="rec-scroll" :class="{'is-card-feed':tab==='cards'}">
      <div class="rec-inner">
        <p v-if="error" class="rec-alert is-error" role="alert">{{ error }}</p>
        <div v-if="overview&&tab!=='cards'" class="rec-status" :class="{'is-off':!overview.enabled,'is-running':activeRun}">
          <span class="rec-status-dot" aria-hidden="true"></span>
          <span v-if="activeRun">正在{{ RUN_KINDS[activeRun.kind] }} · {{ timeText(activeRun.startedAtMs) }} 开始</span>
          <span v-else-if="!overview.enabled">推荐未开启</span>
          <span v-else-if="lastRun">上次{{ RUN_KINDS[lastRun.kind] }}：{{ RUN_STATUS[lastRun.status] || lastRun.status }} · {{ timeText(lastRun.finishedAtMs||lastRun.startedAtMs) }}</span>
          <span v-else>已开启，等待第一次整理</span>
          <span class="rec-status-sep">今日花费 <strong>{{ money(overview.budget.spentUsd) }}</strong><template v-if="overview.budget.budgetUsd"> / {{ money(overview.budget.budgetUsd) }}</template></span>
          <span v-if="overview.budget.exhausted" class="rec-status-warn">今日预算已用完</span>
          <div class="rec-status-actions">
            <el-button v-if="activeRun" size="small" :disabled="!activeRun.conversationUuid" @click="openRun(activeRun)"><el-icon><View/></el-icon>{{ activeRun.conversationUuid?'查看生成过程':'正在启动…' }}</el-button>
            <el-button v-if="activeRun" size="small" @click="stopRun(activeRun)">停止</el-button>
            <el-button v-else-if="runLinkable(lastRun)" size="small" @click="openRun(lastRun)"><el-icon><View/></el-icon>查看生成过程</el-button>
          </div>
        </div>
        <p v-if="overview?.configured&&overview.models&&!overview.models.usable.length" class="rec-alert is-error" role="alert">推荐用的模型都已不可用（{{ overview.models.unavailable.join('、') }}），推荐已暂停。到“设置”里重新选择模型。</p>
        <p v-else-if="overview?.models?.unavailable?.length" class="rec-alert">这些模型已不可用：{{ overview.models.unavailable.join('、') }}。推荐暂时改用 {{ overview.models.usable[0] }}；到“设置”里更新模型列表即可消除这条提示。</p>
        <p v-if="overview&&!overview.configured" class="rec-alert">还没有选择推荐用的模型。到“设置”里选择模型并开启推荐后，后台会先整理一次，之后每天自动整理，也会跟着你正在聊的话题去找资料。</p>

        <!-- ================================================= cards -->
        <section v-if="tab==='cards'" aria-label="推荐" class="rec-feed">
          <header class="rec-digest">
            <div class="rec-digest-date">{{ digestDate() }}</div>
            <h2 class="rec-digest-title">{{ greeting() }}<template v-if="newCount">，有 <em>{{ newCount }}</em> 件事想给你看</template><template v-else-if="cards.length">，新推荐都看过了</template><template v-else>，这里还没有推荐</template></h2>
            <p v-if="todayMix.length" class="rec-digest-mix"><b>今天带回来</b><span v-for="item in todayMix" :key="item.key" :data-category="item.key">{{ CATEGORY_LABELS[item.key] }} {{ item.count }}</span></p>
            <div v-if="overview" class="rec-digest-run">
              <span class="rec-status-dot" :class="{'is-off':!overview.enabled,'is-running':activeRun}" aria-hidden="true"></span>
              <span v-if="activeRun">正在{{ RUN_KINDS[activeRun.kind] }}…</span>
              <span v-else-if="!overview.enabled">推荐未开启</span>
              <span v-else-if="lastRun">上次{{ RUN_KINDS[lastRun.kind] }} {{ timeText(lastRun.finishedAtMs||lastRun.startedAtMs) }}</span>
              <span>今日花费 {{ money(overview.budget.spentUsd) }}</span>
              <button v-if="activeRun||runLinkable(lastRun)" type="button" :disabled="activeRun&&!activeRun.conversationUuid" @click="openRun(activeRun||lastRun)">查看过程</button>
              <button v-if="activeRun" type="button" @click="stopRun(activeRun)">停止</button>
            </div>
          </header>
          <div class="rec-card-toolbar">
            <div class="rec-filters is-flush">
              <button v-for="item in LANES" :key="item.key" type="button" class="rec-chip" :aria-pressed="lane===item.key" @click="lane=item.key">{{ item.label }}</button>
              <span class="rec-toolbar-sep" aria-hidden="true"></span>
              <button type="button" class="rec-chip rec-unread-filter" :aria-pressed="unreadOnly" @click="unreadOnly=!unreadOnly"><span class="rec-filter-check" aria-hidden="true"><el-icon v-if="unreadOnly"><Check/></el-icon></span>只看未读<span v-if="newCount" class="rec-filter-count">{{ newCount }}</span></button>
            </div>
          </div>
          <div class="rec-mobile-summary"><span>{{ newCount?'还有 '+newCount+' 条未读':cards.length?'新推荐都看过了':'这里还没有推荐' }}</span><time>{{ digestDate() }}</time></div>
          <div v-if="activeRun" class="rec-mobile-running"><span class="rec-status-dot is-running" aria-hidden="true"></span><span>正在{{ RUN_KINDS[activeRun.kind] }}…</span><button type="button" :disabled="!activeRun.conversationUuid" @click="openRun(activeRun)">查看过程</button><button type="button" @click="stopRun(activeRun)">停止</button></div>
          <div v-if="!groups.length&&!loadingCards" class="rec-empty"><div class="rec-empty-art" aria-hidden="true">✦</div><h3>{{ unreadOnly?'没有未读的推荐':'还没有推荐' }}</h3><p>{{ unreadOnly?'新推荐出现后会显示在这里。':overview?.enabled?'后台整理完成后，推荐会出现在这里。':'开启推荐后，后台会自动整理你的对话，结果出现在这里。' }}</p></div>
          <template v-for="group in groups" :key="group.key">
            <h3 class="rec-day"><span>{{ group.read?'已读':'未读' }} · {{ group.label }}</span><i aria-hidden="true"></i><small>{{ group.items.length }} 件</small></h3>
            <div class="rec-stories">
              <RecCard v-for="card in group.items" :key="card.id" :card="card" :busy="busyCard===card.id" :lead="card.id===leadId" :mobile="isPhone"
                @ask="ask" @copy="copyReference" @feedback="feedback" @read="markRead" @inspect="inspectMobile" @open-source="openSource" @open-origin="emit('open-conversation',$event)"/>
            </div>
          </template>
          <div v-if="cards.length<cardsTotal" class="rec-more"><el-button :loading="loadingCards" @click="loadCards({append:true})">加载更多推荐</el-button></div>
        </section>

        <!-- ================================================= follows -->
        <section v-else-if="tab==='follows'" aria-label="持续关注" class="rec-follows">
          <div class="rec-follow-composer">
            <h3>关注一件事</h3>
            <p class="rec-hint">关注的是事情本身，比如“主动式 Agent 的研究进展”“Pro 200 降额之后怎么用”。之后每日整理会去查它有没有新进展，有值得看的就推给你。在推荐卡片上点 ☆ 也会加到这里。</p>
            <div class="rec-follow-inputs">
              <el-input v-model="followForm.topic" placeholder="要关注的事" class="rec-follow-target-input" maxlength="200" @keyup.enter="addFollow"/>
              <el-input v-model="followForm.note" placeholder="想看哪方面（可选）" class="rec-follow-name-input" maxlength="500" @keyup.enter="addFollow"/>
              <el-button type="primary" :loading="addingFollow" :disabled="!followForm.topic.trim()" @click="addFollow"><el-icon><Plus/></el-icon>关注</el-button>
            </div>
            <button type="button" class="rec-text-btn is-small" @click="followForm.signalOpen=!followForm.signalOpen"><el-icon :class="{'is-up':followForm.signalOpen}"><ArrowDown/></el-icon>附加自动检测（可选）</button>
            <div v-if="followForm.signalOpen" class="rec-follow-signal">
              <div class="rec-follow-kinds" role="radiogroup" aria-label="检测类型">
                <button v-for="(meta,key) in FOLLOW_KIND_META" v-show="key!=='topic'" :key="key" type="button" role="radio" :aria-checked="followForm.kind===key" class="rec-follow-kind" @click="followForm.kind=key">
                  <span class="rec-follow-glyph" :data-kind="key">{{ meta.glyph }}</span>{{ meta.label }}
                </button>
              </div>
              <el-input v-model="followForm.target" :placeholder="followSignalMeta.placeholder"/>
              <p class="rec-follow-help">{{ followSignalMeta.help }} 它只是帮忙更早发现变化，不代替对这件事本身的跟进。</p>
            </div>
          </div>

          <div v-if="followGroups.length" class="rec-section-head">
            <h3>正在关注 <span class="rec-count">{{ followStats.total }}</span></h3>
            <span v-if="followStats.changed" class="rec-pill is-accent">{{ followStats.changed }} 项检测到变化</span>
          </div>
          <div v-if="!followGroups.length" class="rec-empty">
            <div class="rec-empty-art" aria-hidden="true">☆</div>
            <h3>还没有关注的事</h3>
            <p>在推荐卡片上点 ☆，或在上面写下一件想持续跟进的事。</p>
          </div>
          <div class="rec-matter-list">
            <article v-for="item in followGroups" :key="item.id" class="rec-matter" :class="{'has-change':item.pendingChange||item.signals.some(x=>x.pendingChange)}">
              <div class="rec-matter-head">
                <span class="rec-matter-star" aria-hidden="true">★</span>
                <div class="rec-matter-title">
                  <strong>{{ item.topic || item.target }}</strong>
                  <span>关注于 {{ timeText(item.createdAtMs) }}</span>
                </div>
                <div class="rec-tile-actions">
                  <button v-if="item.kind==='topic'" type="button" class="rec-icon-btn" title="修改关注重点" @click="editFollowNote(item)"><el-icon><EditPen/></el-icon></button>
                  <button type="button" class="rec-icon-btn is-danger" title="取消关注" @click="removeFollow(item)"><el-icon><Delete/></el-icon></button>
                </div>
              </div>
              <p v-if="item.note&&item.note.trim()" class="rec-matter-note"><b>想看</b>{{ item.note }}</p>
              <div class="rec-matter-update" :class="{'is-empty':!item.lastUpdate}">
                <b>{{ item.lastUpdate?'最新进展 · '+timeText(item.lastUpdateMs):'还没有进展' }}</b>
                <span>{{ item.lastUpdate || (item.kind==='topic'?'下一次每日整理会去查它的后续。':'由脚本定时检测变化。') }}</span>
              </div>
              <div v-for="signal in (item.kind==='topic'?item.signals:[item])" :key="signal.id" class="rec-signal-row" :class="{'has-change':signal.pendingChange,'has-error':signal.error}">
                <span class="rec-follow-glyph" :data-kind="signal.kind">{{ FOLLOW_KIND_META[signal.kind]?.glyph }}</span>
                <a v-if="followUrl(signal)" :href="followUrl(signal)" target="_blank" rel="noopener noreferrer">{{ signal.target }}</a><span v-else>{{ signal.target }}</span>
                <span class="rec-signal-state">{{ signal.error?'检测失败':signal.pendingChange?'有变化，下一期会看':signal.lastValue?'当前 '+(signal.kind==='github_commits'?signal.lastValue.slice(0,7):signal.kind==='webpage'?'已记录':signal.lastValue):'等待首次检测' }}</span>
              </div>
            </article>
          </div>
        </section>

        <!-- ================================================= profile -->
        <section v-else-if="tab==='profile'" aria-label="画像" class="rec-profile">
          <div class="rec-profile-intro">
            <div>
              <h3>推荐系统对你的认识</h3>
              <p>从你的对话里整理出来，决定推荐什么。你改过的条目以你的为准，模型不会改回去。</p>
            </div>
            <div class="rec-profile-stats">
              <span><strong>{{ profile.length }}</strong>条</span>
              <span v-if="editedCount"><strong>{{ editedCount }}</strong>条你改过</span>
            </div>
          </div>
          <div class="rec-profile-toolbar">
            <div class="rec-filters is-flush">
              <button type="button" class="rec-chip" :aria-pressed="!profileFilter" @click="profileFilter=''">全部 <span>{{ profile.length }}</span></button>
              <button v-for="kind in PROFILE_KINDS" :key="kind.key" type="button" class="rec-chip" :data-kind="kind.key" :aria-pressed="profileFilter===kind.key" @click="profileFilter=profileFilter===kind.key?'':kind.key">{{ kind.label }} <span>{{ profileCounts[kind.key] }}</span></button>
            </div>
            <div class="rec-profile-tools">
              <el-input v-model="profileQuery" clearable placeholder="搜索画像" class="rec-search"><template #prefix><el-icon><Search/></el-icon></template></el-input>
              <el-button type="primary" @click="editProfile()"><el-icon><Plus/></el-icon>添加</el-button>
            </div>
          </div>
          <div v-if="!profile.length" class="rec-empty"><div class="rec-empty-art" aria-hidden="true">◐</div><h3>画像还是空的</h3><p>第一次整理后会出现，你也可以先手动添加几条。</p></div>
          <div v-else-if="!profileGroups.length" class="rec-empty"><h3>没有匹配的条目</h3></div>
          <section v-for="group in profileGroups" :key="group.key" class="rec-profile-group" :data-kind="group.key">
            <header class="rec-profile-group-head">
              <span class="rec-kind-mark" :data-kind="group.key" aria-hidden="true"></span>
              <h4>{{ group.label }}</h4><span class="rec-count">{{ group.items.length }}</span>
              <span class="rec-profile-group-hint">{{ group.hint }}</span>
              <button type="button" class="rec-icon-btn" :title="'添加'+group.label" @click="editProfile(null,group.key)"><el-icon><Plus/></el-icon></button>
            </header>
            <div class="rec-profile-grid">
              <article v-for="item in group.items" :key="item.id" class="rec-profile-tile" :class="{'is-user':item.userEdited}">
                <div class="rec-profile-tile-head">
                  <h5>{{ item.topic }}</h5>
                  <div class="rec-tile-actions">
                    <button type="button" class="rec-icon-btn" title="修改" @click="editProfile(item)"><el-icon><EditPen/></el-icon></button>
                    <button type="button" class="rec-icon-btn is-danger" title="移除" @click="removeProfile(item)"><el-icon><Delete/></el-icon></button>
                  </div>
                </div>
                <p v-if="item.detail" class="rec-profile-detail" :class="{'is-open':expanded.has(item.id)}" @click="toggleExpanded(item.id)">{{ item.detail }}</p>
                <div class="rec-profile-foot">
                  <el-dropdown v-if="item.kind==='ongoing'" trigger="click" @command="quickStatus(item,$event)">
                    <button type="button" class="rec-status-chip" :data-status="item.status||'none'" title="点一下改状态">{{ statusLabel(item.status)||'未标状态' }}<span aria-hidden="true">▾</span></button>
                    <template #dropdown><el-dropdown-menu>
                      <el-dropdown-item v-for="s in PROFILE_STATUS.filter(s=>s.key)" :key="s.key" :command="s.key" :disabled="s.key===item.status">{{ s.label }}</el-dropdown-item>
                    </el-dropdown-menu></template>
                  </el-dropdown>
                  <span class="rec-signal" :data-signal="item.signal" :title="signalMeta(item.signal).hint"><i></i><i></i><i></i>{{ signalMeta(item.signal).label }}</span>
                  <span class="rec-profile-when">{{ item.lastMentionedMs?'最近提到 '+timeText(item.lastMentionedMs):'' }}</span>
                  <span v-if="item.userEdited" class="rec-pill is-blue">你改过</span>
                </div>
                <details v-if="item.evidence?.length" class="rec-evidence">
                  <summary>依据 {{ item.evidence.length }} 条</summary>
                  <ul><li v-for="(line,index) in item.evidence" :key="index">{{ line }}</li></ul>
                </details>
              </article>
            </div>
          </section>
          <el-dialog v-model="profileDialog.open" :title="profileDialog.item?'修改画像':'添加画像'" width="540px" append-to-body class="rec-dialog mobile-viewport-dialog">
            <div class="rec-dialog-body">
              <div class="rec-field"><label>类型</label>
                <div class="rec-segment"><button v-for="k in PROFILE_KINDS" :key="k.key" type="button" :aria-pressed="profileDialog.form.kind===k.key" @click="profileDialog.form.kind=k.key">{{ k.label }}</button></div>
              </div>
              <div class="rec-field"><label>主题</label><el-input v-model="profileDialog.form.topic" maxlength="200" placeholder="例如：本地部署大模型"/></div>
              <div v-if="profileDialog.form.kind==='ongoing'" class="rec-field"><label>进展</label>
                <div class="rec-segment"><button v-for="k in PROFILE_STATUS.filter(s=>s.key)" :key="k.key" type="button" :aria-pressed="profileDialog.form.status===k.key" @click="profileDialog.form.status=k.key">{{ k.label }}</button></div>
              </div>
              <div class="rec-field"><label>信号强弱</label>
                <div class="rec-segment"><button v-for="k in SIGNALS" :key="k.key" type="button" :aria-pressed="profileDialog.form.signal===k.key" @click="profileDialog.form.signal=k.key">{{ k.label }}</button></div>
              </div>
              <div class="rec-field"><label>说明</label><el-input v-model="profileDialog.form.detail" type="textarea" :autosize="{minRows:3,maxRows:8}" maxlength="2000" placeholder="为什么关注、进展到哪了。例如：已经换了风扇，问题解决了"/></div>
            </div>
            <template #footer><el-button @click="profileDialog.open=false">取消</el-button><el-button type="primary" :loading="profileDialog.saving" :disabled="!profileDialog.form.topic?.trim()" @click="saveProfile">保存</el-button></template>
          </el-dialog>
        </section>

        <!-- ================================================= runs -->
        <section v-else-if="tab==='runs'" aria-label="运行记录">
          <div class="rec-run-legend">
            <div v-for="(hint,key) in RUN_KIND_HINTS" :key="key"><b>{{ RUN_KINDS[key] }}</b>{{ hint }}</div>
          </div>
          <div class="rec-table-scroll">
            <table class="rec-table">
              <colgroup><col style="width:104px"><col style="width:120px"><col style="width:96px"><col><col style="width:48px"><col style="width:72px"><col style="width:68px"><col style="width:132px"></colgroup>
              <thead><tr><th>开始</th><th>类型</th><th>状态</th><th>模型</th><th class="num">卡片</th><th class="num">花费</th><th class="num">Tokens</th><th></th></tr></thead>
              <tbody>
                <tr v-for="run in runs" :key="run.id">
                  <td class="num">{{ timeText(run.startedAtMs) }}</td>
                  <td>{{ RUN_KINDS[run.kind] }}<div class="rec-cell-sub">{{ RUN_TRIGGERS[run.trigger] || run.trigger }}</div></td>
                  <td><span class="rec-status-pill" :data-status="run.status">{{ RUN_STATUS[run.status] || run.status }}</span><div v-if="run.error" class="rec-cell-sub is-error" :title="run.error">{{ run.error }}</div></td>
                  <td class="rec-model-cell" :title="run.models.join(' → ')"><span class="rec-model-name">{{ modelShort(run.models[0]) || '—' }}</span><span v-if="run.models.length>1" class="rec-model-more">+{{ run.models.length-1 }}</span></td>
                  <td class="num">{{ run.cards }}</td>
                  <td class="num">{{ money(run.costUsd) }}</td>
                  <td class="num">{{ compactTokens(run.tokens) }}</td>
                  <td class="rec-run-actions">
                    <el-button v-if="runLinkable(run)" size="small" @click="openRun(run)">过程</el-button>
                    <el-button v-if="run.sourceConversationUuid&&run.sourceExists!==false" size="small" title="被跟随的会话" @click="emit('open-conversation',run.sourceConversationUuid)">来源</el-button>
                    <el-button v-if="['starting','running'].includes(run.status)" size="small" @click="stopRun(run)">停止</el-button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="!runs.length" class="rec-empty"><h3>还没有运行记录</h3></div>
        </section>

        <!-- ================================================= settings -->
        <section v-else-if="tab==='settings'&&settingsData" aria-label="推荐设置" class="rec-settings">
          <div class="rec-panel rec-master" :class="{'is-on':settingsData.settings.enabled}">
            <div>
              <h3>{{ settingsData.settings.enabled?'推荐已开启':'推荐未开启' }}</h3>
              <p class="rec-hint">开启后后台先整理一次，之后每天在下面的时间自动整理；你正在聊的会话也会被跟随，结果只放在这个页面，不打扰当前对话。推荐只读你的对话，不会替你执行推荐里的事。这个开关点一下就生效。</p>
            </div>
            <el-switch :model-value="settingsData.settings.enabled" :loading="togglingEnabled" @update:model-value="toggleEnabled"/>
          </div>
          <div class="rec-panel">
            <h3>模型</h3>
            <p class="rec-hint">第一个模型不可用（额度、鉴权、下线或多次超时）时，从当前这一步起改用下一个。备用模型只能选同一协议的，避免加密思考等内部记录失效。</p>
            <div v-for="(model,index) in settingsForm.models" :key="index" class="rec-model-row">
              <span class="rec-model-index">{{ index===0?'首选':'备用 '+index }}</span>
              <el-select :model-value="model" filterable placeholder="选择模型" @update:model-value="index===0?changeFirst($event):(settingsForm.models[index]=$event)">
                <el-option v-for="option in modelOptions(index)" :key="option.key" :value="option.key" :label="option.label+(priceText(option)?' · '+priceText(option):'')+(index===0?' · '+option.protocol:'')"/>
              </el-select>
              <el-button v-if="index>0||settingsForm.models.length>1" size="small" @click="removeModel(index)">移除</el-button>
            </div>
            <el-button size="small" :disabled="!settingsForm.models[0]" @click="addFallback"><el-icon><Plus/></el-icon>添加备用模型</el-button>
            <div v-if="firstModel?.thinkingLevels?.length" class="rec-form rec-form-gap">
              <div class="rec-field"><label>思考强度</label><el-select v-model="settingsForm.thinkingLevel" clearable placeholder="模型默认"><el-option v-for="level in firstModel.thinkingLevels" :key="level" :value="level" :label="level"/></el-select></div>
            </div>
            <div class="rec-network-tools" aria-label="推荐联网工具">
              <div class="rec-network-head"><h4>联网工具</h4><el-button text :loading="refreshingNetworkTools" aria-label="刷新联网工具状态" @click="refreshNetworkTools"><el-icon><Refresh/></el-icon>刷新</el-button></div>
              <p class="rec-hint">按工具说明选择搜索、抓取用途；调用仍遵守原有权限与确认规则。保存后从下一次运行生效。</p>
              <div class="rec-form">
                <div v-for="role in NETWORK_TOOL_ROLES" :key="role.key" class="rec-field">
                  <label :for="'rec-network-'+role.key">{{ role.label }}</label>
                  <el-select :id="'rec-network-'+role.key" v-model="settingsForm.networkTools[role.key]" multiple filterable collapse-tags :max-collapse-tags="1" :collapse-tags-tooltip="!isPhone"
                    :aria-label="role.label" :placeholder="'选择'+role.label" :no-data-text="'暂无可选的主控工具'" popper-class="rec-network-options">
                    <el-option v-for="option in toolOptions(role.key)" :key="option.name" :value="option.name" :label="option.name+(option.available?'':'（暂不可用）')">
                      <span class="rec-network-option-name">{{ option.name }}<span v-if="!option.available"> · 暂不可用</span></span>
                      <span v-if="option.description" class="rec-network-option-description">{{ option.description }}</span>
                    </el-option>
                  </el-select>
                  <small v-if="toolWarning(role)" class="rec-network-warning" role="status">{{ toolWarning(role) }}</small>
                </div>
              </div>
              <p v-if="networkLimited" class="rec-hint rec-network-limited">联网发现可能受限，内部推荐仍可运行。工具失效时不会自动清空或替换选择。</p>
              <p v-if="networkToolsError" class="rec-network-warning" role="status">刷新失败：{{ networkToolsError }}</p>
            </div>
          </div>
          <div class="rec-panel">
            <h3>节奏</h3>
            <div class="rec-form">
              <div class="rec-field"><label>每日整理时间</label><el-input v-model="settingsForm.dailyTime" :placeholder="'自动：'+settingsData.derivedDailyTime" clearable/><small class="rec-hint">留空时按你最不活跃的时段自动选择（目前是 {{ settingsData.derivedDailyTime }}）。</small></div>
              <div class="rec-field"><label>首次读取最近</label><div class="rec-inline"><el-input-number v-model="settingsForm.initialDays" :min="1" :max="60" controls-position="right"/><span class="rec-unit">天的对话</span></div></div>
              <div class="rec-field is-wide"><label>你的活跃时段（最近 {{ settingsData.activity.days }} 天，按小时）</label><div class="rec-histogram"><span v-for="(value,hour) in histogram" :key="hour" :style="{height:value+'%'}" :title="hour+'点：'+settingsData.activity.hourHistogram[hour]+' 条'"/></div><div class="rec-histogram-axis"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div></div>
              <div class="rec-field"><label>跟随正在聊的会话</label><el-switch v-model="settingsForm.followEnabled"/><small class="rec-hint">你在某个会话里连续聊了几条后，后台针对这个话题去外面找资料，也会翻你自己的其他会话和记忆找相关的事。同一个会话的每次跟随都在同一个推荐会话里接着做。</small></div>
              <div class="rec-field"><label>新消息达到</label><div class="rec-inline"><el-input-number v-model="settingsForm.followMinNewUserMessages" :min="1" :max="50" controls-position="right"/><span class="rec-unit">条后触发</span></div></div>
              <div class="rec-field"><label>同一会话至少间隔</label><div class="rec-inline"><el-input-number v-model="settingsForm.followCooldownMinutes" :min="1" :max="1440" controls-position="right"/><span class="rec-unit">分钟</span></div></div>
              <div class="rec-field"><label>最多同时跟随</label><div class="rec-inline"><el-input-number v-model="settingsForm.followConcurrency" :min="1" :max="5" controls-position="right"/><span class="rec-unit">个会话</span></div><small class="rec-hint">多个会话同时满足条件时可以并行跟随；每个都会单独调用模型。</small></div>
              <div class="rec-field"><label>停顿多久后开始</label><div class="rec-inline"><el-input-number v-model="settingsForm.followIdleMinutes" :min="0" :max="120" controls-position="right"/><span class="rec-unit">分钟</span></div></div>
              <div class="rec-field"><label>持续关注检查间隔</label><div class="rec-inline"><el-input-number v-model="settingsForm.trackingIntervalMinutes" :min="10" :max="1440" controls-position="right"/><span class="rec-unit">分钟</span></div></div>
              <div class="rec-field"><label>单次运行最长</label><div class="rec-inline"><el-input-number v-model="settingsForm.runTimeoutMinutes" :min="5" :max="240" controls-position="right"/><span class="rec-unit">分钟</span></div></div>
            </div>
          </div>
          <div class="rec-panel">
            <h3>每日预算</h3>
            <p class="rec-hint">按每一步实际用的模型和价格累计，达到后当天不再调用模型。留空表示不限。</p>
            <div class="rec-form">
              <div class="rec-field"><label>金额</label><div class="rec-inline"><el-input-number v-model="settingsForm.dailyBudgetUsd" :min="0" :step="0.5" :precision="2" :value-on-clear="null" controls-position="right"/><span class="rec-unit">美元</span></div></div>
              <div class="rec-field"><label>Tokens</label><div class="rec-inline"><el-input-number v-model="settingsForm.dailyBudgetTokens" :min="0" :step="100000" :value-on-clear="null" controls-position="right"/></div></div>
            </div>
          </div>
          <div class="rec-panel rec-prompts">
            <h3>提示词</h3>
            <p class="rec-hint">推荐模型按这里的提示词工作。不改就一直用内置默认（会随版本更新）；改过的以你的为准，保存后从下一次运行起生效。工具和动作的名称请保持不变。</p>
            <div class="rec-prompt-tabs" role="tablist" aria-label="提示词">
              <button v-for="item in PROMPT_KEYS" :key="item.key" type="button" role="tab" :aria-selected="promptTab===item.key" @click="promptTab=item.key">
                {{ item.label }}<span v-if="promptCustomized(item)" class="rec-prompt-flag">已修改</span>
              </button>
            </div>
            <div class="rec-prompt-head">
              <span class="rec-hint">{{ promptMeta.hint }}</span>
              <button v-if="promptCustomized(promptMeta)" type="button" class="rec-text-btn" @click="restorePrompt">恢复默认</button>
            </div>
            <div class="rec-prompt-editor"><AdaptiveMdEditor :key="promptTab" v-model="promptText" language="markdown" completion-mode="none"/></div>
          </div>
          <div class="rec-panel">
            <h3>范围与通知</h3>
            <div class="rec-form">
              <div class="rec-field"><label>分析哪些目录</label><el-select v-model="settingsForm.scopeMode"><el-option v-for="m in SCOPE_MODES" :key="m.key" :value="m.key" :label="m.label"/></el-select></div>
              <div v-if="settingsForm.scopeMode!=='all'" class="rec-field"><label>目录</label><el-select v-model="settingsForm.scopeFolders" multiple filterable collapse-tags placeholder="选择目录"><el-option v-for="folder in folderOptions" :key="folder.id" :value="folder.id" :label="folder.path||folder.name"/></el-select></div>
              <div class="rec-field"><label>每日汇总通知</label><el-select v-model="settingsForm.notifyMode"><el-option v-for="m in NOTIFY_MODES" :key="m.key" :value="m.key" :label="m.label"/></el-select><small class="rec-hint">只在每日整理有推荐或失败时发一条；跟随对话的结果不单独通知。</small></div>
            </div>
          </div>
        </section>
      </div>
    </div>
    <transition name="rec-savebar">
      <div v-if="tab==='settings'&&settingsData&&dirty" class="rec-savebar" role="region" aria-label="未保存的设置">
        <span class="rec-savebar-dot" aria-hidden="true"></span>
        <span class="rec-savebar-text">设置有修改，还没保存</span>
        <el-button @click="discardSettings">还原</el-button>
        <el-button type="primary" :loading="savingSettings" @click="saveSettings">保存设置</el-button>
      </div>
    </transition>
  </div>
</template>
