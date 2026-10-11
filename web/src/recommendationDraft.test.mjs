import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref, nextTick} from 'vue';
import {parse, compileScript, compileTemplate} from '@vue/compiler-sfc';
import {createAttachmentDraftStorage} from './views/consoleView/attachmentDraftStorage.js';
import {createMemoryAttachmentDraftDriver} from './views/consoleView/attachmentDraftMemoryDriver.mjs';
import {isLocalConversationRow as isLocalConversation} from './conversationOrdering.js';
import {draftEnvironment, mountDraftConsole, settleDraftConsole, draftNames} from './views/consoleView/consoleDraftLifecycleHarness.mjs';

const source = fs.readFileSync(new URL('./App.vue', import.meta.url), 'utf8');
const draftCode = source.slice(source.indexOf('const pendingRecommendationDraft ='), source.indexOf('const pageHeaderReady ='));
const focusCode = source.slice(source.indexOf('function focusLocalConversation('), source.indexOf('async function startConsoleNewSession('));
const key = 'openbear.console.drafts.v1';
const local = 'local:new';
const request = {text: '推荐引用\n接着聊', folderId: 'recommended-folder', model: 'recommended-model'};
const file = id => ({id, file: new File([id], `${id}.txt`, {type: 'text/plain'})});
const deferred = () => {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};};

async function harness({text = '', files = [], row = false, driver = createMemoryAttachmentDraftDriver(), confirm = async () => {}, navigate = true} = {}) {
  const store = createAttachmentDraftStorage({driver});
  await store.save(local, files);
  await store.save('other-conversation', [file('other')]);
  const saved = new Map([[key, JSON.stringify({[local]: text, 'other-conversation': 'other text'})]]);
  const confirmations = [], errors = [], warnings = [], events = [], routes = [], refreshed = [];
  const ctx = vm.createContext({ref, nextTick, isLocalConversation, LOCAL_CONVERSATION_UUID: local,
    active: ref('recommendations'), navigationSequence: 1,
    conversations: ref(row ? [{local: true, conversationUuid: local, folderId: 'old-folder'}] : []),
    activeConversationUuid: ref(''), draftFolderId: ref('old-folder'), conversationTreeRef: ref(null),
    window: {localStorage: {getItem: k => saved.get(k), setItem: (k, v) => saved.set(k, v)}, dispatchEvent: e => events.push(e)},
    CustomEvent: class {constructor(type, {detail}) {this.type = type; this.detail = detail;}},
    createAttachmentDraftStorage: options => createAttachmentDraftStorage({...options, driver}),
    ElMessageBox: {confirm: async (...args) => {confirmations.push(args); return confirm();}},
    ElMessage: {error: value => errors.push(value), warning: value => warnings.push(value)},
    navigateFromCron: fn => navigate ? fn() : false,
    closeSidebar() {}, syncRoute() {routes.push(ctx.active.value);},
    refreshConsoleAfterPropSync() {refreshed.push(true);},
    localConversation: folderId => ({local: true, conversationUuid: local, folderId, parentId: folderId}),
    setDraftFolderId: value => {ctx.draftFolderId.value = value;},
    setConversationsIfChanged: value => {ctx.conversations.value = value;},
    Api: new Proxy({}, {get() {return () => assert.fail('draft preparation must not send or create a conversation');}}),
  });
  vm.runInContext(draftCode + '\n' + focusCode, ctx);
  return {ctx, saved, store, driver, confirmations, errors, warnings, events, routes, refreshed,
    run: () => ctx.openRecommendationDraft(request),
    text: () => JSON.parse(saved.get(key))[local],
    pending: () => vm.runInContext('pendingRecommendationDraft.value', ctx),
  };
}

for (const row of [false, true]) {
  test(`persisted text is protected with sidebar row=${row}: cancel is side-effect free`, async () => {
    const h = await harness({row, text: 'unsent original', confirm: async () => {throw 'cancel';}});
    await h.run();
    assert.equal(h.confirmations.length, 1);
    assert.equal(h.text(), 'unsent original');
    assert.equal(h.ctx.draftFolderId.value, 'old-folder');
    assert.equal(h.pending(), null);
    assert.deepEqual(h.events, []); assert.deepEqual(h.routes, []);
  });
  test(`confirmed text/attachments replacement with sidebar row=${row} starts clean and preserves other drafts`, async () => {
    const h = await harness({row, text: 'unsent original', files: [file('old')]});
    await h.run();
    assert.equal(h.confirmations.length, 1);
    assert.match(h.confirmations[0][0], /文字和待发送附件会一并清除/);
    assert.equal(h.text(), request.text);
    assert.deepEqual((await h.store.load(local)).items, []);
    assert.equal((await h.store.load('other-conversation')).items[0].id, 'other');
    assert.equal(JSON.parse(h.saved.get(key))['other-conversation'], 'other text');
    assert.equal(h.ctx.activeConversationUuid.value, local);
    assert.equal(h.ctx.conversations.value[0].folderId, request.folderId);
    assert.equal(h.pending().model, request.model);
    assert.equal(h.events.length, 1);
    assert.deepEqual(h.routes, ['console']);
    assert.deepEqual(h.errors, []);
  });
}
for (const accepted of [false, true]) {
  test(`attachment-only persisted draft requires confirmation, accepted=${accepted}`, async () => {
    const h = await harness({files: [file('attachment-only')], confirm: async () => {if (!accepted) throw 'cancel';}});
    await h.run();
    assert.equal(h.confirmations.length, 1);
    assert.equal((await h.store.load(local)).items.length, accepted ? 0 : 1);
    assert.equal(h.text(), accepted ? request.text : '');
    assert.equal(h.routes.length, accepted ? 1 : 0);
  });
}
test('no draft opens without confirmation and only prefills the composer', async () => {
  const h = await harness(); await h.run();
  assert.equal(h.confirmations.length, 0);
  assert.equal(h.text(), request.text);
  assert.equal(h.pending().text, request.text);
  assert.deepEqual(h.routes, ['console']);
});
test('skipped attachment metadata is protected and cleared on confirmed replacement', async () => {
  const h = await harness();
  h.driver.records.set(local, {key: local, items: [], skipped: [{id: 'large', fileName: 'large.zip'}]});
  await h.run();
  assert.equal(h.confirmations.length, 1);
  assert.deepEqual((await h.store.load(local)).skipped, []);
});
for (const failing of ['get', 'delete']) {
  test(`unreadable/uncleanable attachments (${failing}) abort instead of attaching stale files`, async () => {
    const h = await harness({text: 'original', files: [file('old')]});
    h.driver[failing] = async () => {throw new Error('storage failure');};
    await h.run();
    assert.equal(h.text(), 'original'); assert.equal(h.pending(), null);
    assert.equal(h.errors.length, 1); assert.deepEqual(h.routes, []);
    assert.equal(h.driver.records.get(local).items[0].id, 'old');
  });
}
test('no IndexedDB still protects text and permits a confirmed text-only replacement', async () => {
  const h = await harness({text: 'original', driver: null}); await h.run();
  assert.equal(h.confirmations.length, 1); assert.equal(h.text(), request.text);
  assert.deepEqual(h.errors, []);
});
test('unreadable text storage cannot be mistaken for no draft', async () => {
  const h = await harness({text: 'original'});
  h.ctx.window.localStorage.getItem = () => {throw new Error('denied');};
  await h.run();
  assert.equal(h.text(), 'original'); assert.equal(h.errors.length, 1); assert.deepEqual(h.routes, []);
});
test('cancelling navigation does not clear confirmed files or text', async () => {
  const h = await harness({text: 'original', files: [file('old')], navigate: false});
  await h.run();
  assert.equal(h.text(), 'original'); assert.equal((await h.store.load(local)).items.length, 1);
  assert.deepEqual(h.events, []);
});
test('leaving the page while confirmation is pending invalidates the request; double clicks do not replace twice', async () => {
  const gate = deferred(), entered = deferred();
  const h = await harness({text: 'original', files: [file('old')], confirm: async () => {entered.resolve(); await gate.promise;}});
  const pending = h.run(); await entered.promise;
  await h.run(); assert.equal(h.confirmations.length, 1);
  h.ctx.active.value = 'settings'; h.ctx.navigationSequence++;
  gate.resolve(); await pending;
  assert.equal(h.text(), 'original'); assert.equal((await h.store.load(local)).items.length, 1);
  assert.deepEqual(h.routes, []);
});
for (const changed of ['text', 'files']) {
  test(`a ${changed} change during confirmation is not implicitly authorized`, async () => {
    const gate = deferred(), entered = deferred();
    const h = await harness({text: 'original', files: [file('old')], confirm: async () => {entered.resolve(); await gate.promise;}});
    const pending = h.run(); await entered.promise;
    if (changed === 'text') h.saved.set(key, JSON.stringify({[local]: 'new unconfirmed text'}));
    else await h.store.save(local, [file('old'), file('unconfirmed')]);
    gate.resolve(); await pending;
    assert.equal(h.warnings.length, 1); assert.deepEqual(h.routes, []);
    assert.equal(h.text(), changed === 'text' ? 'new unconfirmed text' : 'original');
    assert.equal((await h.store.load(local)).items.length, changed === 'files' ? 2 : 1);
  });
}
test('files added during cleanup are neither deleted nor carried into the recommendation', async () => {
  const h = await harness({text: 'original', files: [file('old')]});
  const get = h.driver.get; let reads = 0;
  h.driver.get = async k => {
    if (k === local && ++reads === 3) h.driver.records.get(local).items.push({...file('new-unconfirmed'), fileName: 'new.txt'});
    return get(k);
  };
  await h.run();
  assert.equal(h.warnings.length, 1); assert.deepEqual(h.routes, []);
  assert.equal(h.text(), 'original');
  assert.deepEqual((await h.store.load(local)).items.map(item => item.id), ['new-unconfirmed']);
});
test('attachment cleanup finishes before the new composer can mount and restore its files', async () => {
  const h = await harness({files: [file('old')]});
  const gate = deferred(), entered = deferred(), remove = h.driver.delete;
  h.driver.delete = async k => {entered.resolve(); await gate.promise; return remove(k);};
  const pending = h.run(); await entered.promise;
  assert.equal(h.pending(), null); assert.deepEqual(h.routes, []);
  gate.resolve(); await pending;
  assert.equal(h.driver.records.has(local), false);
  assert.deepEqual(h.routes, ['console']);
});
for (const accepted of [false, true]) {
  test(`real Console lifecycle after App recommendation decision restores only authorized text/files, accepted=${accepted}`, async () => {
    const h = await harness({text: 'original', files: [file('old')], confirm: async () => {if (!accepted) throw 'cancel';}});
    await h.run();
    const env = draftEnvironment();
    env.driver = h.driver;
    env.window.localStorage = h.ctx.window.localStorage;
    const sends = [];
    env.api.createConversation = env.api.sendMessage = async () => {sends.push('unexpected send'); throw new Error('must not send');};
    env.api.rathOptions = async () => ({models: [{key: 'test-model'}, {key: request.model}], primaryModel: 'test-model'});
    let applied = 0;
    const composer = await mountDraftConsole(env, local, {
      recommendationDraft: h.pending(), folderId: accepted ? request.folderId : 'old-folder',
      onRecommendationDraftApplied: () => applied++,
    });
    try {
      await settleDraftConsole();
      assert.equal(composer.state.draft, accepted ? request.text : 'original');
      assert.deepEqual(draftNames(composer), accepted ? [] : ['old.txt']);
      assert.equal(applied, accepted ? 1 : 0);
      if (accepted) assert.equal(composer.state.localModel, request.model);
      assert.deepEqual(sends, []);
      assert.deepEqual(env.errors, []);
    } finally {composer.unmount(); env.cleanup();}
  });
}
test('App script and real template still compile without design or layout changes', () => {
  const descriptor = parse(source, {filename: 'App.vue'}).descriptor;
  const script = compileScript(descriptor, {id: 'app'});
  const template = compileTemplate({id: 'app', filename: 'App.vue', source: descriptor.template.content,
    compilerOptions: {bindingMetadata: script.bindings}});
  assert.deepEqual(template.errors, []);
});
