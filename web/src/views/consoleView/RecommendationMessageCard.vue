<script setup>
import {computed} from 'vue';
import {MagicStick, ArrowRight} from '@element-plus/icons-vue';
import {time} from '../../components/cron/cronConfig.js';
const props = defineProps({message: {type: Object, required: true}});
const card = computed(() => props.message.recommendationCard || {});
const kind = computed(() => card.value.kind || (String(props.message.content || '').includes('跟随对话') ? 'follow' : 'daily'));
const title = computed(() => kind.value === 'follow' ? '跟随对话' : '每日整理');
const trigger = computed(() => ({manual: '手动开始', scheduled: '定时触发', conversation: '你正在聊的会话有新内容'})[card.value.trigger] || '系统触发');
const summary = computed(() => kind.value === 'follow'
  ? '读取正在进行的会话的新消息，判断外部有没有值得参考的东西；结果只放进推荐页。'
  : '读取最近的对话，更新画像，研究值得关注的方向并保存推荐卡片。');
const startedAt = computed(() => card.value.startedAtMs || Number(props.message.createdAt || 0) * 1000 || null);
const runId = computed(() => card.value.runId || props.message.recommendationRunId || String(props.message.opId || '').replace(/^msg:/, ''));
function openPage() { window.dispatchEvent(new CustomEvent('openbear:open-recommendations')); }
</script>

<template>
  <button type="button" class="event-envelope rec-envelope" :aria-label="`推荐模块：${title}`" title="打开推荐页" @click="openPage">
    <span class="event-envelope-icon" aria-hidden="true"><el-icon><MagicStick/></el-icon></span>
    <span class="event-envelope-main">
      <span class="event-envelope-label">推荐模块 <span>· {{ trigger }}</span></span>
      <strong>{{ title }}</strong>
      <span class="event-envelope-summary">{{ summary }}</span>
      <span class="event-envelope-meta"><time>{{ time(startedAt) }}</time><span v-if="runId" class="event-envelope-id">#{{ runId.slice(0, 8) }}</span></span>
    </span>
    <el-icon class="event-envelope-arrow"><ArrowRight/></el-icon>
  </button>
</template>

<style scoped src="./eventMessageCard.css"></style>
