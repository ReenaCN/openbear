import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app.web_admin import _LOGIN_NONCE_COOKIE
from app.web_console.auth_api import _notification_return_target
from tests.test_login_update_regressions import login_env as login_env


@pytest.mark.parametrize("target", [
    "/chat?id=a%26b%2F%E4%BC%9A%E8%AF%9D",
    "/settings?section=system-settings&domain=notifications",
    "/recommendations",
    "/recommendations?source=push&label=%E6%8E%A8%E8%8D%90",
])
async def test_notification_page_target_survives_unauthenticated_login_chain(login_env, target):
    env = login_env
    page = await env.client.get(target, allow_redirects=False)
    assert page.status == 302
    login = page.headers["Location"]
    assert urlsplit(login).path == "/login"
    return_target = parse_qs(urlsplit(login).query)["next"][0]
    # aiohttp/YARL can canonicalize a query slash from %2F to / in the request;
    # the conversation ID and all query values must survive unchanged.
    assert urlsplit(return_target).path == urlsplit(target).path
    assert parse_qs(urlsplit(return_target).query) == parse_qs(urlsplit(target).query)
    assert (await env.client.get(login)).status == 200

    # The API remains a 401, not an HTML redirect or implicit subscription bind.
    api = await env.client.get("/api/auth/session")
    assert api.status == 401
    assert (await api.json())["error"] == "unauthorized"
    start = await env.client.post("/api/auth/login/start", json={"secret": await env.server.get_secret_key()})
    request_id = (await start.json())["requestUuid"]
    nonce = start.cookies[_LOGIN_NONCE_COOKIE].value
    await env.server.decide_login_request(request_id, approved=True, decided_by=123)
    consume = await env.client.post(f"/api/auth/login/consume/{request_id}", cookies={_LOGIN_NONCE_COOKIE: nonce})
    assert consume.status == 200
    cookie = consume.cookies["openbear_web_session"].value
    session = await env.client.get("/api/auth/session", cookies={"openbear_web_session": cookie})
    assert session.status == 200
    # LoginView separately verifies this API before navigating to the saved target.
    restored = await env.client.get(return_target, cookies={"openbear_web_session": cookie}, allow_redirects=False)
    assert restored.status == 200
    assert restored.url.path == urlsplit(target).path
    assert parse_qs(restored.url.raw_query_string) == parse_qs(urlsplit(target).query)


@pytest.mark.parametrize("target", ["/chat?id=a%26b", "/recommendations?source=push"])
async def test_notification_return_target_survives_configured_https_login_hop(login_env, target):
    env = login_env
    env.server.config.web.custom_url = "https://console.example:8443"
    response = await env.client.get(target, allow_redirects=False)
    assert response.status == 302
    response = await env.client.get(response.headers["Location"], headers={"Host": "untrusted.invalid"}, allow_redirects=False)
    assert response.status == 302
    destination = urlsplit(response.headers["Location"])
    assert f"{destination.scheme}://{destination.netloc}{destination.path}" == "https://console.example:8443/login"
    assert parse_qs(destination.query)["next"] == [target]
    assert not env.bot.sent


@pytest.mark.parametrize("target", [
    "https://evil.test/chat", "//evil.test/chat", "/\\evil.test/chat", "/chat\n?x=1",
    "/chat\t", "/api/auth/session", "/login?next=/chat", "/memory", "/%2f%2fevil.test/chat",
    "%2Fchat", "https://console.example/chat", "chat?id=x", "/chat/../settings",
    "https://evil.test/recommendations", "//evil.test/recommendations",
    "/\\evil.test/recommendations", "/recommendations/", "/recommendations-extra",
    "/api/recommendations/cards", "/%72ecommendations", "/recommendations\n?source=push",
])
async def test_https_login_rejects_non_whitelisted_return_targets(login_env, target):
    env = login_env
    env.server.config.web.custom_url = "https://console.example"
    response = await env.client.get("/login?" + urlencode({"next": target}), allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "https://console.example/login"
    assert _notification_return_target(target) == ""


@pytest.mark.skipif(not shutil.which("node"), reason="cross-language worker/login regression requires Node")
async def test_real_recommendation_payload_worker_and_server_login_round_trip(login_env):
    from app.web_push import BrowserPush

    root = Path(__file__).resolve().parents[1]
    payload = BrowserPush.payload("recommendation", "process-conversation", "recommendation-update")
    # Run the actual worker handlers with browser notification/client seams only.
    worker = subprocess.run(["node", "--input-type=module", "-e", """
        import fs from 'node:fs'; import vm from 'node:vm';
        const handlers = {}, notices = [], opened = [];
        const self = {location: {origin: 'https://bear.test'},
          addEventListener: (type, handler) => handlers[type] = handler,
          registration: {showNotification: async (title, options) => notices.push(options)},
          clients: {matchAll: async () => [], openWindow: async url => opened.push(url)}};
        vm.runInNewContext(fs.readFileSync('web/public/openbear-push-sw.js', 'utf8'), {self, URL});
        let pending;
        handlers.push({data: {json: () => JSON.parse(fs.readFileSync(0, 'utf8'))}, waitUntil: p => pending = p});
        await pending;
        handlers.notificationclick({notification: {close() {}, data: notices[0].data}, waitUntil: p => pending = p});
        await pending; console.log(JSON.stringify(opened));
    """], cwd=root, input=json.dumps(payload), text=True, capture_output=True, check=True, timeout=15)
    clicked = json.loads(worker.stdout)
    assert clicked == ["https://bear.test/recommendations"]
    page = await login_env.client.get(urlsplit(clicked[0]).path, allow_redirects=False)
    assert page.status == 302
    login_url = page.headers["Location"]
    # The browser's success handler consumes exactly the server-generated next.
    success = subprocess.run(["node", "--input-type=module", "-e", """
        import fs from 'node:fs'; import {loginSuccessTarget} from './web/src/loginRedirect.js';
        console.log(loginSuccessTarget(new URL(fs.readFileSync(0, 'utf8'), 'https://bear.test')));
    """], cwd=root, input=login_url, text=True, capture_output=True, check=True, timeout=15)
    assert success.stdout.strip() == "/recommendations"


async def test_non_notification_pages_keep_existing_login_destination(login_env):
    response = await login_env.client.get("/memory?next=https://evil.test/chat", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/login"
