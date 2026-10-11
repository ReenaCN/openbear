<script setup>
import {computed,ref,watch} from 'vue';
import {Check,ChatLineRound,Close,TopRight,ArrowRight,EditPen,Star,StarFilled,Remove,MagicStick,Link,CopyDocument} from '@element-plus/icons-vue';
import {CATEGORY_LABELS,askedQuestions,finalAction,timeText,sourceLabel} from './recConfig.js';

const props=defineProps({card:{type:Object,required:true},mode:{type:String,default:'detail'},busy:{type:Boolean,default:false}});
const emit=defineEmits(['close','update:mode','read','ask','copy','feedback','open-source','open-origin']);
const note=ref(''),content=ref(null),dragOffset=ref(0),dragging=ref(false),closing=ref(false);
let swipe=null;
// Keep the release position for the drawer's leave-from frame; let its native
// leave-to transform carry the panel off screen instead of unmounting mid-drag.
const swipeStyle=computed(()=>dragOffset.value?{
  '--rec-sheet-drag-y':`${dragOffset.value}px`,
  transform:closing.value?undefined:`translateY(${dragOffset.value}px)`,
}:undefined);
const unread=computed(()=>!props.card.seenAtMs);
const asked=computed(()=>askedQuestions(props.card));
const done=computed(()=>finalAction(props.card));
const notes=computed(()=>(props.card.feedback||[]).filter(item=>item.action==='note'&&item.detail));
const originLinkable=computed(()=>Boolean(props.card.originConversationUuid)&&props.card.originExists!==false);
const isOpenBear=computed(()=>props.card.lane==='openbear');
const following=computed(()=>{
  const items=props.card.feedback||[];
  const last=action=>Math.max(0,...items.filter(item=>item.action===action).map(item=>item.atMs||0));
  return props.card.state==='followed'||(last('follow')>0&&last('follow')>last('unfollow'));
});
const actions=computed(()=>isOpenBear.value
  ?[{key:'adopted',label:'已采纳'},{key:'dismiss',label:'不需要'}]
  :[{key:'known',label:'已经知道'},{key:'resolved',label:'已解决'},{key:'dismiss',label:'不感兴趣'}]);
const title=computed(()=>({detail:'全文与来源',chat:'聊聊这条',process:'处理这条推荐'})[props.mode]||'全文与来源');
watch(()=>props.card.id,()=>{note.value='';cancelSwipe();});
watch(()=>props.mode,()=>{cancelSwipe();if(content.value)content.value.scrollTop=0;});
function closeSheet(){swipe=null;dragging.value=false;closing.value=true;}
function cancelSwipe(){if(closing.value)return;swipe=null;dragging.value=false;dragOffset.value=0;}
function startSwipe(event){
  if(closing.value)return;
  cancelSwipe();
  if(event.touches.length!==1)return;
  const target=event.target;
  // Leave controls, text editing and ordinary content scrolling to the browser.
  if(target.closest('button,a,input,textarea,select,[contenteditable]:not([contenteditable="false"]),.el-drawer__footer'))return;
  const inContent=Boolean(target.closest('.rec-sheet-content'));
  if(inContent&&content.value?.scrollTop>0)return;
  const touch=event.touches[0];
  swipe={id:touch.identifier,x:touch.clientX,y:touch.clientY,inContent};
}
function moveSwipe(event){
  if(!swipe)return;
  if(event.touches.length!==1){cancelSwipe();return;}
  const touch=event.touches[0];
  if(touch.identifier!==swipe.id){cancelSwipe();return;}
  const dx=touch.clientX-swipe.x,dy=touch.clientY-swipe.y;
  if(!dragging.value){
    if(Math.max(Math.abs(dx),Math.abs(dy))<8)return;
    // Lock the first intentional direction; a reading scroll must never turn into a dismissal.
    if(dy<=0||dy<=Math.abs(dx)||(swipe.inContent&&content.value?.scrollTop>0)){cancelSwipe();return;}
  }
  if(!event.cancelable){cancelSwipe();return;}
  event.preventDefault();
  dragging.value=true;
  dragOffset.value=Math.max(0,dy);
}
function endSwipe(){
  const dismiss=dragging.value&&dragOffset.value>=88;
  if(dismiss)closeSheet();
  else cancelSwipe();
}
// The page closes only after a successful API response. A failed save leaves the
// draft intact, and moving the card to the read group cannot destroy this panel.
function submitNote(){const value=note.value.trim();if(value&&!props.busy)emit('feedback',props.card,'note',value);}
</script>

<template>
  <el-drawer :model-value="!closing" direction="btt" size="auto" append-to-body :with-header="false" :title="title" class="rec-mobile-drawer" modal-class="rec-mobile-sheet-overlay" :class="{'is-swiping':dragging}" :style="swipeStyle" @touchstart="startSwipe" @touchmove="moveSwipe" @touchend="endSwipe" @touchcancel="cancelSwipe" @close="closeSheet" @closed="emit('close')">
    <div class="rec-sheet-handle" aria-hidden="true"></div>
    <header class="rec-sheet-header"><h2>{{ title }}</h2><button type="button" aria-label="关闭" @click="closeSheet"><el-icon><Close/></el-icon></button></header>
    <div ref="content" class="rec-sheet-content">
      <template v-if="mode==='detail'">
        <div class="rec-sheet-meta"><span>{{ CATEGORY_LABELS[card.category] || card.category }}</span><span aria-hidden="true">·</span><time>{{ timeText(card.createdAtMs) }}</time></div>
        <h3 class="rec-sheet-title">{{ card.title }}</h3>
        <section v-if="card.reason" class="rec-sheet-reason"><h4><el-icon><MagicStick/></el-icon>为什么推荐给你</h4><p>{{ card.reason }}</p></section>
        <p class="rec-sheet-body">{{ card.body }}</p>
        <section v-if="card.toCheck?.length" class="rec-sheet-check"><h4>待核对</h4><ul><li v-for="item in card.toCheck" :key="item">{{ item }}</li></ul></section>
        <template v-if="card.sources?.length">
          <h4 class="rec-sheet-section-title">来源 · {{ card.sources.length }}</h4>
          <div class="rec-sheet-sources"><a v-for="source in card.sources" :key="source.url" :href="source.url" target="_blank" rel="noopener noreferrer" :title="source.url" @click="emit('open-source',card,source.url)"><el-icon><Link/></el-icon><span>{{ sourceLabel(source) }}</span><el-icon><TopRight/></el-icon></a></div>
        </template>
        <button v-if="originLinkable" type="button" class="rec-sheet-origin" @click="emit('open-origin',card.originConversationUuid)">来自「{{ card.originTitle || '一个会话' }}」<el-icon><TopRight/></el-icon></button>
        <ul v-if="notes.length" class="rec-sheet-notes"><li v-for="item in notes" :key="item.atMs">你的补充：{{ item.detail }}</li></ul>
      </template>
      <template v-else-if="mode==='chat'">
        <p class="rec-sheet-context">{{ card.title }}</p>
        <p class="rec-sheet-hint">选一个问题填入输入框，不会自动发送。</p>
        <div class="rec-sheet-questions">
          <button v-for="question in card.questions" :key="question" type="button" :class="{'is-asked':asked.has(question)}" :disabled="busy" @click="emit('ask',card,question)"><span>{{ question }}</span><el-icon><ArrowRight/></el-icon></button>
          <button type="button" class="is-free" :disabled="busy" @click="emit('ask',card,'')"><el-icon><EditPen/></el-icon><span>自己问一句…</span><el-icon><ArrowRight/></el-icon></button>
        </div>
      </template>
      <template v-else>
        <p class="rec-sheet-context">{{ card.title }}</p>
        <div class="rec-sheet-feedback">
          <button v-for="action in actions" :key="action.key" type="button" :disabled="busy" :class="{'is-on':done===action.key}" @click="emit('feedback',card,action.key)"><el-icon><Check v-if="done===action.key||action.key!=='dismiss'"/><Remove v-else/></el-icon>{{ action.label }}</button>
          <button v-if="!isOpenBear" type="button" :disabled="busy" :class="{'is-on':following}" @click="emit('feedback',card,following?'unfollow':'follow')"><el-icon><StarFilled v-if="following"/><Star v-else/></el-icon>{{ following?'取消关注':'持续关注' }}</button>
        </div>
        <div class="rec-sheet-note">
          <label :for="'rec-mobile-note-'+card.id"><el-icon><EditPen/></el-icon>补充一句</label>
          <el-input :id="'rec-mobile-note-'+card.id" v-model="note" type="textarea" :rows="3" maxlength="500" :disabled="busy" placeholder="想看哪方面，或为什么不想看…"/>
          <p>只记录你的反馈，不会进入聊天或发送问题。</p>
        </div>
        <ul v-if="notes.length" class="rec-sheet-notes"><li v-for="item in notes" :key="item.atMs">你的补充：{{ item.detail }}</li></ul>
      </template>
    </div>
    <template #footer>
      <template v-if="mode==='detail'"><button type="button" :disabled="busy||!unread" @click="emit('read',card)"><el-icon><Check/></el-icon>{{ unread?'标为已读':'已读' }}</button><button type="button" class="is-primary" @click="emit('update:mode','chat')"><el-icon><ChatLineRound/></el-icon>聊聊这条</button></template>
      <template v-else-if="mode==='chat'"><button type="button" @click="closeSheet">返回推荐</button><button type="button" @click="emit('copy',card)"><el-icon><CopyDocument/></el-icon>复制到已有会话</button></template>
      <template v-else><button type="button" @click="closeSheet">取消</button><button type="button" class="is-primary" :disabled="busy||!note.trim()" @click="submitNote">{{ busy?'保存中…':'保存补充' }}</button></template>
    </template>
  </el-drawer>
</template>
