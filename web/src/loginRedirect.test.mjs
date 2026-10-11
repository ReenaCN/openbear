import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse} from '@vue/compiler-sfc';
import {notificationTarget, loginUrlForLocation, loginSuccessTarget} from './loginRedirect.js';
import {createLoginFlow} from './views/loginFlow.js';

const origin = 'https://bear.test';
const location = path => new URL(path, origin);
const targets = ['/chat?id=a%26b%2F%E4%BC%9A%E8%AF%9D', '/settings?section=system-settings&domain=notifications', '/recommendations', '/recommendations?source=push'];

for (const target of targets) {
  test(`notification destination survives the real API 401 and LoginView success chain: ${target}`, async () => {
    let interceptor;
    const redirects = [];
    const win = {location: Object.assign(location(target), {replace: url => redirects.push(url)})};
    // Execute the actual API interceptor, not a duplicate of its redirect logic.
    const source = readFileSync(new URL('./api.js', import.meta.url), 'utf8');
    vm.runInNewContext(source.replace(/^import .*;\n/gm, '').replace(/^export /gm, ''), {
      axios: {create: () => ({interceptors: {response: {use(ok, error) {interceptor = error;}}}})},
      window: win, loginUrlForLocation,
    });
    const error = {response: {status: 401}};
    await assert.rejects(interceptor(error), actual => actual === error);
    assert.equal(redirects.length, 1);
    assert.equal(location(redirects[0]).pathname, '/login');
    assert.equal(location(redirects[0]).searchParams.get('next'), target);
    win.location = Object.assign(location(redirects[0]), {replace: url => redirects.push(url)});
    // A login-page 401 must not replace or erase its existing target.
    await assert.rejects(interceptor(error));
    assert.equal(redirects.length, 1);

    const callbacks = [];
    const calls = [];
    const {descriptor} = parse(readFileSync(new URL('./views/LoginView.vue', import.meta.url), 'utf8'));
    const context = vm.createContext({...Vue, window: win, loginSuccessTarget, onBeforeUnmount() {},
      ElMessage: {success() {}, error(message) {assert.fail(message);}},
      Api: {
        async loginStart() {calls.push('start'); return {requestUuid: 'request', expiresIn: 300};},
        async loginStatus() {calls.push('status'); return {status: 'approved'};},
        async consumeLogin() {calls.push('consume');},
        async authSession() {calls.push('session'); return {ok: true};},
      },
      createLoginFlow: options => createLoginFlow({...options,
        schedule: callback => {callbacks.push(callback); return callbacks.length;}, unschedule() {},
        lifecycle: {isVisible: () => true, isOnline: () => true, subscribe: () => () => {}},
      }),
    });
    vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*;\n/gm, ''), context);
    await vm.runInContext('state.secret = "test-only"; submit()', context);
    assert.equal(redirects.length, 1); // No premature redirect before session verification.
    await callbacks.shift()();
    assert.deepEqual(calls, ['start', 'status', 'consume', 'session']);
    assert.equal(redirects.at(-1), target);
    vm.runInContext('flow.dispose()', context);
  });
}

test('login targets reject external, ambiguous and non-whitelisted destinations', () => {
  for (const value of ['https://evil.test/chat', '//evil.test/chat', '/\\evil.test/chat', 'javascript:alert(1)', '/api/auth/session', '/login?next=/chat', '/memory', '/%2f%2fevil.test/chat', '%2Fchat', '/chat\n?x=1', '/chat\t', 'https://bear.test/chat', 'chat?id=x']) {
    assert.equal(loginSuccessTarget(location(`/login?next=${encodeURIComponent(value)}`)), '/', value);
  }
  for (const value of ['https://evil.test/recommendations', '//evil.test/recommendations', '/recommendations/', '/recommendations-extra', '/api/recommendations/cards', '/%72ecommendations']) {
    assert.equal(loginSuccessTarget(location(`/login?next=${encodeURIComponent(value)}`)), '/', value);
  }
  assert.equal(loginSuccessTarget(location('/login')), '/');
  assert.equal(loginUrlForLocation(location('/memory?next=https://evil.test')), '/login');
  assert.equal(notificationTarget('https://evil.test/chat', origin), '');
  assert.equal(notificationTarget('https://user:pass@bear.test/chat', origin), '');
  assert.equal(notificationTarget('https://bear.test/chat?id=ok', origin), '/chat?id=ok');
  assert.equal(notificationTarget('/chat?id=https%3A%2F%2Fevil.test', origin), '/chat?id=https%3A%2F%2Fevil.test');
});
