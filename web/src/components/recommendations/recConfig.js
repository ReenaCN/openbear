// Recommendation page vocabularies and pure helpers (unit-tested, no DOM).
export const TABS = [
  {key: 'cards', label: '推荐'},
  {key: 'follows', label: '持续关注'},
  {key: 'profile', label: '画像'},
  {key: 'runs', label: '运行记录'},
  {key: 'settings', label: '设置'},
];
export const LANES = [
  {key: '', label: '全部'},
  {key: 'external', label: '外部发现'},
  {key: 'openbear', label: 'OpenBear 建议'},
];
export const CATEGORY_LABELS = {
  update: '新动态', project: '项目跟进', adjacent: '相邻发现', improvement: '更好的做法', resume: '接回搁置的事',
  curiosity: '好奇心延伸', lifestyle: '生活与消费', news: '新闻', openbear: 'OpenBear 建议',
};
// Feed presentation per category: an icon tile and an accent, so a day's picks read as a varied digest.
export const CATEGORY_META = {
  update: {icon: 'Bell', tone: 'blue'},
  project: {icon: 'FolderOpened', tone: 'blue'},
  adjacent: {icon: 'Compass', tone: 'violet'},
  improvement: {icon: 'TrendCharts', tone: 'green'},
  resume: {icon: 'VideoPlay', tone: 'orange'},
  curiosity: {icon: 'MagicStick', tone: 'violet'},
  lifestyle: {icon: 'Goods', tone: 'amber'},
  news: {icon: 'Notification', tone: 'red'},
  openbear: {icon: 'Opportunity', tone: 'green'},
};
export const STATE_LABELS = {
  new: '新', seen: '已看', engaged: '点过', sent: '已发出', continued: '继续聊过', followed: '持续关注', known: '已经知道',
  resolved: '已解决', dismissed: '不感兴趣', adopted: '已采纳', parked: '已搁置',
};
export const PROFILE_KINDS = [
  {key: 'interest', label: '关注点', hint: '在关注的产品、项目、领域，以及为什么关注'},
  {key: 'ongoing', label: '进行中的事', hint: '在做、想做或做到一半的事'},
  {key: 'preference', label: '偏好与口味', hint: '喜欢和不喜欢的方案、风格'},
  {key: 'background', label: '背景', hint: '工作、技能、设备和环境'},
  {key: 'habit', label: '工作习惯', hint: '某类事情固定放在哪、用什么模型'},
];
export const PROFILE_STATUS = [
  {key: '', label: '—'}, {key: 'active', label: '进行中'}, {key: 'paused', label: '搁置'}, {key: 'done', label: '已完成'},
  {key: 'dropped', label: '放弃'}, {key: 'unknown', label: '不清楚'},
];
export const SIGNALS = [
  {key: 'explicit', label: '亲口说的', hint: '你亲口说过的兴趣或计划，最强'},
  {key: 'repeated', label: '反复做的', hint: '你反复主动去做、去问的事'},
  {key: 'weak', label: '弱线索', hint: '一次性的提问或好奇'},
];
export const FOLLOW_KINDS = {
  topic: '话题', github_release: 'GitHub 版本', github_commits: 'GitHub 提交', npm: 'npm 包', webpage: '网页变化',
};
export const FOLLOW_KIND_META = {
  github_release: {label: 'GitHub 版本', glyph: 'Rel', placeholder: 'owner/repo，例如 vuejs/core', help: '脚本定时检查最新发布的版本，不调用模型；有新版本会出现在下一期推荐里。'},
  github_commits: {label: 'GitHub 提交', glyph: 'Git', placeholder: 'owner/repo', help: '脚本定时检查默认分支的最新提交，适合还没有正式发版的项目。'},
  npm: {label: 'npm 包', glyph: 'npm', placeholder: '包名，例如 vite', help: '脚本定时检查 npm 上的最新版本。'},
  webpage: {label: '网页', glyph: 'Web', placeholder: 'https://…', help: '脚本定时抓取网页，内容变了就交给下一期推荐判断值不值得告诉你。'},
  topic: {label: '话题', glyph: '#', placeholder: '例如：GraphRAG 新版本', help: '没有固定网址的事，每日整理时由模型联网研究有没有后续动态。'},
};
export const RUN_KINDS = {daily: '每日整理', follow: '跟随对话'};
export const RUN_KIND_HINTS = {
  daily: '每天读一次你最近的对话，更新画像，再去外面找值得看的东西。',
  follow: '你在某个会话里连续聊了几条、停下来后，后台针对这个话题找外部资料和你自己的相关内容；同一会话的跟随都在同一个推荐会话里接着做，结果只放进推荐页。',
};
export const RUN_TRIGGERS = {manual: '手动', scheduled: '定时', conversation: '跟随会话'};
export const RUN_STATUS = {
  starting: '启动中', running: '运行中', completed: '完成', failed: '失败', timed_out: '超时', cancelled: '已停止',
  interrupted: '中断', budget_exhausted: '预算用完', skipped: '跳过',
};
export const NOTIFY_MODES = [
  {key: 'inherit', label: '跟随任务通知设置'}, {key: 'silent', label: '不通知'}, {key: 'telegram', label: '只发 TG'},
  {key: 'push', label: '只发浏览器'}, {key: 'both', label: 'TG 和浏览器'},
];
export const SCOPE_MODES = [
  {key: 'all', label: '全部目录'}, {key: 'include', label: '只分析选中的目录'}, {key: 'exclude', label: '排除选中的目录'},
];
// Terminal per-card feedback (mutually exclusive presentation).
export const FINAL_ACTIONS = ['known', 'resolved', 'dismiss', 'adopted'];

export function dayKey(ms, tz = 'Asia/Shanghai') {
  return new Intl.DateTimeFormat('zh-CN', {timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date(ms));
}
export function dayLabel(ms, now = Date.now(), tz = 'Asia/Shanghai') {
  const key = dayKey(ms, tz);
  if (key === dayKey(now, tz)) return '今天';
  if (key === dayKey(now - 86_400_000, tz)) return '昨天';
  return new Intl.DateTimeFormat('zh-CN', {timeZone: tz, month: 'long', day: 'numeric', weekday: 'short'}).format(new Date(ms));
}
export function groupByDay(items, now = Date.now(), tz = 'Asia/Shanghai') {
  const groups = [];
  for (const item of items) {
    const key = dayKey(item.createdAtMs, tz);
    let group = groups[groups.length - 1];
    if (!group || group.key !== key) groups.push(group = {key, label: dayLabel(item.createdAtMs, now, tz), items: []});
    group.items.push(item);
  }
  return groups;
}
// Reading status is the outer group: even yesterday's unread precedes today's read.
export function groupFeed(items, now = Date.now(), tz = 'Asia/Shanghai') {
  const sorted = [...items].sort((a, b) => Number(b.createdAtMs) - Number(a.createdAtMs)
    || Number(b.priority || 0) - Number(a.priority || 0) || String(b.id).localeCompare(String(a.id)));
  return [false, true].flatMap(read => groupByDay(sorted.filter(card => Boolean(card.seenAtMs) === read), now, tz)
    .map(group => ({...group, key: `${read ? 'read' : 'unread'}-${group.key}`, read})));
}
export function timeText(ms, tz = 'Asia/Shanghai') {
  if (!ms) return '—';
  return new Intl.DateTimeFormat('zh-CN', {timeZone: tz, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(ms));
}
export function clockText(ms, tz = 'Asia/Shanghai') {
  if (!ms) return '';
  return new Intl.DateTimeFormat('zh-CN', {timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(ms));
}
export function sourceHost(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
}
// A readable label for a source: its title, or host + short path when the title is just the URL.
export function sourceLabel(source) {
  const url = String(source?.url || '');
  const title = String(source?.title || '').trim();
  if (title && title !== url) return title;
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/$/, '');
    return parsed.hostname.replace(/^www\./, '') + (path ? (path.length > 28 ? path.slice(0, 27) + '…' : path) : '');
  } catch { return url; }
}
export function cardPublishers(card) {
  return [...new Set((card?.sources || []).map(source => sourceHost(source.url)).filter(Boolean))];
}
// The lead story: the highest-priority card of the newest day (first one on ties).
export function leadCardId(groups) {
  const items = groups?.[0]?.items || [];
  if (!items.length) return '';
  return items.reduce((best, card) => (Number(card.priority || 0) > Number(best.priority || 0) ? card : best), items[0]).id;
}
export function categoryCounts(cards) {
  const counts = {};
  for (const card of cards || []) counts[card.category] = (counts[card.category] || 0) + 1;
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([key, count]) => ({key, count}));
}
export function digestDate(ms = Date.now(), tz = 'Asia/Shanghai') {
  return new Intl.DateTimeFormat('zh-CN', {timeZone: tz, month: 'long', day: 'numeric', weekday: 'long'}).format(new Date(ms));
}
export function greeting(ms = Date.now(), tz = 'Asia/Shanghai') {
  const hour = Number(new Intl.DateTimeFormat('en-GB', {timeZone: tz, hour: '2-digit', hour12: false}).format(new Date(ms)));
  return hour < 6 ? '夜深了' : hour < 11 ? '早上好' : hour < 14 ? '中午好' : hour < 18 ? '下午好' : '晚上好';
}
// Follows are grouped by the MATTER; repo/package/page checks attach to it as signals.
export function groupFollows(items) {
  const matters = [], loose = [];
  for (const item of items || []) (item.kind === 'topic' ? matters : loose).push({...item, signals: []});
  const signals = [];
  for (const item of loose) {
    const owner = matters.find(m => (item.cardId && m.cardId === item.cardId) || m.topic === item.topic);
    if (owner) owner.signals.push(item); else signals.push(item);
  }
  return [...matters, ...signals];
}
export function money(value) {
  const number = Number(value || 0);
  return number < 0.01 && number > 0 ? `$${number.toFixed(4)}` : `$${number.toFixed(2)}`;
}
export function compactTokens(value) {
  const number = Number(value || 0);
  if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
  if (number >= 1e3) return `${(number / 1e3).toFixed(1)}k`;
  return String(number);
}
export function finalAction(card) {
  const done = (card.feedback || []).filter(item => FINAL_ACTIONS.includes(item.action));
  return done.length ? done[done.length - 1].action : '';
}
export function askedQuestions(card) {
  return new Set((card.feedback || []).filter(item => item.action === 'ask').map(item => item.detail));
}
// Model chain must stay inside the first model's protocol.
export function protocolOf(protocols, key) {
  for (const [protocol, models] of Object.entries(protocols || {})) if (models.some(model => model.key === key)) return protocol;
  return '';
}
export function chainOptions(protocols, chain, index) {
  if (index === 0) return Object.entries(protocols || {}).flatMap(([protocol, models]) => models.map(model => ({...model, protocol})));
  const protocol = (chain || []).map(key => protocolOf(protocols, key)).find(Boolean) || '';
  return (protocols?.[protocol] || []).filter(model => !chain.includes(model.key) || chain[index] === model.key).map(model => ({...model, protocol}));
}
export function normalizeChain(protocols, chain) {
  const clean = [...new Set((chain || []).filter(Boolean))];
  const protocol = clean.map(key => protocolOf(protocols, key)).find(Boolean) || '';
  // Saving unrelated settings must not discard unavailable choices or healthy fallbacks.
  return clean.filter(key => !protocolOf(protocols, key) || protocolOf(protocols, key) === protocol);
}
export function priceText(model) {
  const cost = model?.cost || {};
  if (cost.input == null && cost.output == null) return '';
  return `$${cost.input ?? '?'}/$${cost.output ?? '?'} 每百万`;
}
export function modelShort(key) {
  const value = String(key || '');
  return value.includes('/') ? value.slice(value.indexOf('/') + 1) : value;
}
export function followUrl(item) {
  const target = String(item?.target || '');
  if (item?.kind === 'github_release') return `https://github.com/${target}/releases`;
  if (item?.kind === 'github_commits') return `https://github.com/${target}/commits`;
  if (item?.kind === 'npm') return `https://www.npmjs.com/package/${target}`;
  if (item?.kind === 'webpage' && /^https:\/\//.test(target)) return target;
  return '';
}
export const NETWORK_TOOL_ROLES = [
  {key: 'search', label: '搜索工具'}, {key: 'fetch', label: '抓取工具'},
];
// Roles are the user's choice; registry names/descriptions are never classified by heuristics.
export function networkToolOptions(options, selected = []) {
  const items = (options || []).filter(item => item.available || selected.includes(item.name));
  const known = new Set(items.map(item => item.name));
  return [...items, ...selected.filter(name => !known.has(name)).map(name => ({name, description: '', available: false}))];
}
export function networkToolWarning(label, selected = [], options = []) {
  if (!selected.length) return `尚未选择${label}。`;
  const available = new Set(options.filter(item => item.available).map(item => item.name));
  const missing = selected.filter(name => !available.has(name)).length;
  if (!missing) return '';
  return `${missing === selected.length ? label + '当前均不可用' : label + '有部分暂不可用'}（${missing} 项），选择已保留。`;
}
// Compare what would be SAVED: an empty trailing model slot or a cleared budget is not a change.
export function settingsDirty(saved, form, protocols) {
  const norm = value => {
    const out = JSON.parse(JSON.stringify(value || {}));
    out.models = normalizeChain(protocols, out.models || []);
    for (const key of ['dailyBudgetUsd', 'dailyBudgetTokens']) if (out[key] === '' || out[key] === 0 || out[key] === undefined) out[key] = null;
    delete out.enabled; // saved immediately by its own switch
    return JSON.stringify(Object.keys(out).sort().map(key => [key, out[key]]));
  };
  return norm(saved) !== norm(form);
}
// The composer receives the card as a reference followed by the chosen question.
export function composerText(referenceToken, question) {
  return `${referenceToken} ${question || ''}`.trimEnd();
}
