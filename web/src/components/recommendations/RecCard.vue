<script setup>
import {computed,ref} from 'vue';
import {Check,ChatLineRound,Star,StarFilled,TopRight,ArrowDown,EditPen,Document,Operation,CopyDocument} from '@element-plus/icons-vue';
import * as Icons from '@element-plus/icons-vue';
import {CATEGORY_LABELS,CATEGORY_META,askedQuestions,finalAction,clockText,sourceLabel,cardPublishers} from './recConfig.js';
const props=defineProps({card:{type:Object,required:true},busy:{type:Boolean,default:false},lead:{type:Boolean,default:false},mobile:{type:Boolean,default:false}});
const emit=defineEmits(['ask','copy','feedback','open-source','open-origin','read','inspect']);
const noteOpen=ref(false),note=ref(''),expanded=ref(false);
const isOpenBear=computed(()=>props.card.lane==='openbear');
const unread=computed(()=>!props.card.seenAtMs);
const done=computed(()=>finalAction(props.card));
const asked=computed(()=>askedQuestions(props.card));
const meta=computed(()=>CATEGORY_META[props.card.category]||CATEGORY_META.update);
const icon=computed(()=>Icons[meta.value.icon]||Icons.Compass);
const publishers=computed(()=>cardPublishers(props.card));
const following=computed(()=>{
  const items=props.card.feedback||[];
  const last=action=>Math.max(0,...items.filter(x=>x.action===action).map(x=>x.atMs||0));
  return props.card.state==='followed'||(last('follow')>0&&last('follow')>last('unfollow'));
});
const actions=computed(()=>isOpenBear.value
  ?[{key:'adopted',label:'已采纳'},{key:'dismiss',label:'不需要'}]
  :[{key:'known',label:'已经知道'},{key:'resolved',label:'已解决'},{key:'dismiss',label:'不感兴趣'}]);
// A deleted origin conversation is not linkable (older payloads without the flag still are).
const originLinkable=computed(()=>Boolean(props.card.originConversationUuid)&&props.card.originExists!==false);
const notes=computed(()=>(props.card.feedback||[]).filter(f=>f.action==='note'&&f.detail));
const muted=computed(()=>!unread.value);
// Long enough to be clamped at the collapsed size (lead shows more lines).
const long=computed(()=>String(props.card.body||'').length>(props.lead?170:84)||(props.card.sources||[]).length>0||(props.card.toCheck||[]).length>0);
const showFull=computed(()=>expanded.value||!long.value);
function submitNote(){const value=note.value.trim();if(!value)return;emit('feedback',props.card,'note',value);note.value='';noteOpen.value=false;}
function toggle(event){if(event?.target?.closest('a,button'))return;if(long.value)expanded.value=!expanded.value;}
</script>

<template>
  <article v-if="mobile" class="rec-mobile-card" :class="{'is-unread':unread,'is-muted':muted}" :data-tone="meta.tone" :data-card-id="card.id" :data-category="card.category">
    <div class="rec-mobile-meta">
      <span class="rec-mobile-dot" :aria-label="unread?'未读':'已读'"></span>
      <span class="rec-mobile-category">{{ CATEGORY_LABELS[card.category] || card.category }}</span>
      <span v-if="lead&&unread" class="rec-mobile-pick">首推</span>
      <span class="rec-mobile-meta-right"><template v-if="card.sources?.length"><span>{{ card.sources.length }} 个来源</span><span aria-hidden="true">·</span></template><time>{{ clockText(card.createdAtMs) }}</time></span>
    </div>
    <button type="button" class="rec-mobile-copy" :aria-label="'查看全文：'+card.title" @click="emit('inspect',card,'detail')">
      <h3>{{ card.title }}</h3><p>{{ card.body }}</p>
    </button>
    <footer class="rec-mobile-actions">
      <button type="button" :disabled="busy||!unread" :aria-label="unread?'标为已读':'已读'" @click="emit('read',card)"><el-icon><Check/></el-icon>已读</button>
      <button type="button" aria-label="查看全文与来源" @click="emit('inspect',card,'detail')"><el-icon><Document/></el-icon>全文</button>
      <button type="button" class="rec-mobile-chat" @click="emit('inspect',card,'chat')"><el-icon><ChatLineRound/></el-icon>聊聊<span v-if="card.questions?.length" class="rec-mobile-question-count">{{ card.questions.length }}</span></button>
      <button type="button" @click="emit('inspect',card,'process')"><el-icon><Operation/></el-icon>处理</button>
      <button type="button" title="复制推荐引用" aria-label="复制推荐引用" @click="emit('copy',card)"><el-icon><CopyDocument/></el-icon>复制</button>
    </footer>
  </article>
  <article v-else class="rec-story" :class="['tone-'+meta.tone,{'is-lead':lead,'is-unread':unread,'is-muted':muted,'is-open':showFull}]" :data-card-id="card.id" :data-category="card.category">
    <div class="rec-story-tile" aria-hidden="true"><el-icon><component :is="icon"/></el-icon></div>
    <div class="rec-story-main">
      <div class="rec-story-kicker">
        <span v-if="unread" class="rec-unread-dot" aria-label="未读"></span>
        <span v-if="lead" class="rec-lead-label">今日首推</span>
        <span class="rec-cat">{{ CATEGORY_LABELS[card.category] || card.category }}</span>
        <span v-if="publishers.length" class="rec-kicker-item">{{ publishers[0] }}<template v-if="publishers.length>1"> 等 {{ publishers.length }} 处</template></span>
        <span class="rec-kicker-item">{{ clockText(card.createdAtMs) }}</span>
        <button v-if="originLinkable" type="button" class="rec-kicker-link" :title="'打开会话：'+(card.originTitle||'')" @click="emit('open-origin',card.originConversationUuid)">来自「<span class="rec-origin-title">{{ card.originTitle || '一个会话' }}</span>」<el-icon><TopRight/></el-icon></button>
        <button v-if="unread" type="button" class="rec-read" :disabled="busy" title="标为已读" aria-label="标为已读" @click="emit('read',card)"><el-icon><Check/></el-icon>已读</button>
      </div>

      <h3 class="rec-story-title" :class="{'is-clickable':long}" @click="toggle">{{ card.title }}</h3>
      <p v-if="card.reason" class="rec-story-why" :class="{'is-clickable':long}" @click="toggle"><span aria-hidden="true">✦</span><span class="rec-story-reason">{{ card.reason }}</span></p>
      <p class="rec-story-body" :class="{'is-clamped':!showFull,'is-clickable':long}" @click="toggle">{{ card.body }}</p>
      <div v-if="showFull&&card.toCheck?.length" class="rec-story-check"><span>待核对</span><ul><li v-for="item in card.toCheck" :key="item">{{ item }}</li></ul></div>
      <button v-if="originLinkable" type="button" class="rec-mobile-origin" @click="emit('open-origin',card.originConversationUuid)">来自「<span>{{ card.originTitle || '一个会话' }}</span>」<el-icon><TopRight/></el-icon></button>
      <button v-if="long" type="button" class="rec-more-link" @click="expanded=!expanded">{{ expanded?'收起':'展开全文与来源' }}<el-icon :class="{'is-up':expanded}"><ArrowDown/></el-icon></button>

      <div v-if="showFull&&card.sources?.length" class="rec-story-sources">
        <a v-for="source in card.sources" :key="source.url" :href="source.url" target="_blank" rel="noopener noreferrer" :title="source.url" @click="emit('open-source',card,source.url)">
          <span class="rec-source-mono" aria-hidden="true">{{ (sourceLabel({url:source.url,title:''})||'?').slice(0,1).toUpperCase() }}</span><span class="rec-source-text">{{ sourceLabel(source) }}</span><el-icon class="rec-source-out"><TopRight/></el-icon>
        </a>
      </div>

      <div class="rec-story-asks" aria-label="接着聊">
        <span class="rec-asks-label"><el-icon><ChatLineRound/></el-icon>聊聊</span>
        <div class="rec-asks-list">
          <button v-for="question in card.questions" :key="question" type="button" class="rec-ask" :class="{'is-asked':asked.has(question)}" :disabled="busy" title="填进输入框（不会自动发送）" @click="emit('ask',card,question)">{{ question }}</button>
          <button type="button" class="rec-ask is-free" :disabled="busy" title="只带上这张卡片的资料，问题自己写" @click="emit('ask',card,'')">自己问…</button>
        </div>
      </div>
      <ul v-if="notes.length" class="rec-feedback-list"><li v-for="item in notes" :key="item.atMs">你的补充：{{ item.detail }}</li></ul>
      <footer class="rec-story-foot">
        <div class="rec-foot-actions">
          <button v-if="!isOpenBear" type="button" class="rec-foot-btn" :class="{'is-on':following}" :disabled="busy" :title="following?'正在关注这件事，点击取消':'之后每日整理会跟进这件事的后续'" @click="emit('feedback',card,following?'unfollow':'follow')"><el-icon><component :is="following?StarFilled:Star"/></el-icon>{{ following?'关注中':'持续关注' }}</button>
          <button v-for="item in actions" :key="item.key" type="button" class="rec-foot-btn" :class="{'is-on':done===item.key}" :disabled="busy" @click="emit('feedback',card,item.key)"><el-icon v-if="done===item.key"><Check/></el-icon>{{ item.label }}</button>
          <button type="button" class="rec-foot-btn" :class="{'is-on':noteOpen}" :disabled="busy" :aria-expanded="noteOpen" :aria-controls="'rec-note-'+card.id" @click="noteOpen=!noteOpen"><el-icon><EditPen/></el-icon>补充一句</button>
        </div>
        <button type="button" class="rec-foot-btn rec-foot-copy" title="复制这张推荐的引用，粘贴到任意已有会话的输入框" aria-label="复制推荐引用" @click="emit('copy',card)"><el-icon><CopyDocument/></el-icon>复制</button>
      </footer>
      <div v-if="noteOpen" :id="'rec-note-'+card.id" class="rec-note">
        <el-input v-model="note" type="textarea" :rows="3" maxlength="500" aria-label="补充一句" placeholder="例如：想看，但我更想知道……／不想看，因为……"/>
        <div class="rec-note-actions">
          <el-button size="small" :disabled="busy" @click="noteOpen=false">取消</el-button>
          <el-button size="small" type="primary" :disabled="!note.trim()||busy" @click="submitNote">提交</el-button>
        </div>
      </div>
    </div>
  </article>
</template>
