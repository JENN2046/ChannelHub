'use strict';

// Development-only host. This file is copied into a disposable pinned VCP host
// and executed by devctl inside its network/filesystem sandbox, never by server.js.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const express = require('express');

const HOST_KEY = 'channelhub-pilot-synthetic-host';
const BRIDGE_KEY = 'channelhub-pilot-synthetic-bridge';
const AGENT = 'pilot-synthetic';
const MODEL = 'pilot-synthetic-model';
const ANSWER = 'ChannelHub isolated synthetic answer';
const ADMIN = { 'x-synthetic-admin': 'yes' };
const hostProfile = process.env.CH_DEV_HOST_PROFILE;
if (!['reference', 'tencent-source', 'tencent-runtime'].includes(hostProfile)) {
    throw new Error('FIXTURE_HOST_PROFILE_REQUIRED');
}
const authMode = hostProfile === 'reference' ? 'native' : 'synthetic';
const smoke = process.argv.slice(2);
if (smoke.length > 1 || (smoke.length === 1 && smoke[0] !== '--smoke')) {
    throw new Error('FIXTURE_ARGUMENT_NOT_ALLOWED');
}
for (const [name, expected] of Object.entries({
    CH_DEV_SOCKET: '/runstate/backend.sock',
    CH_DEV_STATE: '/runstate/data',
    CH_DEV_READY: '/runstate/ready.json'
})) {
    if (process.env[name] !== expected) throw new Error('FIXTURE_SANDBOX_PATH_REQUIRED:' + name);
}
if (process.cwd() !== '/work/host' || path.resolve(__dirname, '..') !== '/work/host') {
    throw new Error('FIXTURE_DISPOSABLE_HOST_REQUIRED');
}

let manager, plugin, internalServer, adminServer;
let closing = null;
let mockCalls = 0;
let signalsReady = false;
let pendingSignal = false;
const readyPath = process.env.CH_DEV_READY;
const socketPath = process.env.CH_DEV_SOCKET;

function listen(server, ...args) {
    return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(...args, () => {
            server.off('error', reject);
            resolve(server.address());
        });
    });
}

function close(server) {
    if (!server?.listening) return Promise.resolve();
    return new Promise((resolve, reject) => {
        server.close(error => error ? reject(error) : resolve());
        server.closeIdleConnections();
    });
}

async function shutdown() {
    if (closing) return closing;
    closing = (async () => {
        fs.rmSync(readyPath, { force: true });
        plugin?.beginDrain();
        // End browser admission first. Existing plugin operations are drained
        // by native shutdown before closing its internal execution listener.
        const adminClosed = close(adminServer);
        await manager?.shutdownAllPlugins();
        await manager?.toolApprovalManager?.watcher?.close();
        manager?.toolApprovalManager?.shutdown();
        await adminClosed;
        await close(internalServer);
        fs.rmSync(socketPath, { force: true });
    })();
    return closing;
}

function onSignal() {
    if (!signalsReady) { pendingSignal = true; return; }
    shutdown().then(() => { process.exitCode = 0; }).catch(() => {
        console.error('FIXTURE_SHUTDOWN_FAILED'); process.exitCode = 1;
    });
}
process.on('SIGTERM', onSignal);
process.on('SIGINT', onSignal);

async function main() {
    if (fs.existsSync(socketPath)) throw new Error('FIXTURE_SOCKET_ALREADY_EXISTS');
    if (fs.existsSync(readyPath)) throw new Error('FIXTURE_READY_ALREADY_EXISTS');
    fs.mkdirSync(process.env.CH_DEV_STATE, { recursive: true, mode: 0o700 });
    const pluginDirs = fs.readdirSync(path.join(process.cwd(), 'Plugin'), { withFileTypes: true })
        .filter(entry => entry.isDirectory()).map(entry => entry.name).sort();
    assert.deepEqual(pluginDirs, ['ChannelHub'], 'FIXTURE_ONLY_CHANNELHUB_ALLOWED');

    // Patch the native singleton in memory, without loading agent maps, private
    // prompts, file watchers or any real host runtime configuration.
    const agents = require('../modules/agentManager');
    agents.agentMap = new Map([[AGENT, 'synthetic-only']]);
    agents.isAgent = name => name === AGENT;
    agents.getAgentPrompt = async name => {
        assert.equal(name, AGENT, 'FIXTURE_AGENT_REQUIRED');
        return 'SYNTHETIC_AGENT';
    };

    const app = express();
    app.disable('x-powered-by');
    app.use(express.json({ limit: '256kb' }));
    app.use('/admin_api', (req, res, next) =>
        req.headers['x-synthetic-admin'] === 'yes' ? next() : res.status(401).end());
    if (hostProfile === 'reference') {
        app.use(require('../modules/apiBearerAuth')(HOST_KEY));
    } else {
        // Both pinned Tencent hosts predate apiBearerAuth.js. This is a
        // synthetic fixture gate only: it does not patch, replace, or establish
        // acceptance of that host's real server.js authentication boundary.
        app.use((req, res, next) => {
            if (req.path.startsWith('/admin_api') || req.path.startsWith('/plugin-callback')) return next();
            if (req.headers.authorization === `Bearer ${HOST_KEY}`) return next();
            res.status(401).json({ error: 'FIXTURE_SYNTHETIC_BEARER_REQUIRED' });
        });
    }
    app.post('/v1/chat/completions', (req, res) => {
        const body = req.body;
        if (body?.model !== MODEL || body?.messages?.[0]?.content !== 'SYNTHETIC_AGENT'
            || body?.stream !== false || body?._channelHubRequest !== true) {
            return res.status(400).json({ error: 'FIXTURE_SYNTHETIC_REQUEST_REQUIRED' });
        }
        mockCalls++;
        res.json({ choices: [{ message: { content: ANSWER } }] });
    });
    const admin = express.Router();
    app.use('/admin_api', admin);
    internalServer = http.createServer(app);
    internalServer.requestTimeout = 10000;
    const address = await listen(internalServer, 0, '127.0.0.1');
    const url = `http://127.0.0.1:${address.port}`;

    Object.assign(process.env, {
        ChannelHubEnabled: 'true', ChannelHubDataDir: process.env.CH_DEV_STATE,
        ChannelHubModel: MODEL, ChannelHubTimeoutMs: '2000',
        PORT: String(address.port), Key: HOST_KEY, DebugMode: 'false'
    });
    manager = require('../Plugin');
    manager.setProjectBasePath(process.cwd());
    await manager.loadPlugins();
    assert.equal(manager.plugins.size, 1, 'FIXTURE_SINGLE_PLUGIN_REQUIRED');
    plugin = manager.getServiceModule('ChannelHub');
    assert.equal(plugin?.snapshot().ready, true, 'FIXTURE_PLUGIN_NOT_READY');
    await manager.initializeServices(app, admin, process.cwd());

    // The browser-facing socket deliberately has no native execution or B2
    // callback routes, even if a future relay bug forwards an arbitrary URL.
    adminServer = http.createServer((req, res) => {
        if (req.method === 'POST' && req.url === '/__dev/shutdown') {
            if (req.headers['x-synthetic-admin'] !== 'yes') {
                res.writeHead(401); res.end(); return;
            }
            res.writeHead(202, { 'Content-Type': 'application/json' });
            res.end('{"accepted":true}');
            setImmediate(onSignal);
            return;
        }
        const route = (req.url || '').split('?')[0];
        if (!/^\/admin_api\/channelHub(?:\/[A-Za-z0-9_./-]*)?$/.test(route)
            || route.includes('/../') || route.endsWith('/..')
            || !['GET', 'POST', 'PUT', 'DELETE'].includes(req.method)) {
            res.writeHead(404); res.end(); return;
        }
        app(req, res);
    });
    adminServer.requestTimeout = 10000;
    await listen(adminServer, socketPath);
    fs.chmodSync(socketPath, 0o600);

    const request = (endpoint, body, headers = {}) => fetch(url + endpoint, {
        method: body === undefined ? 'GET' : 'POST',
        redirect: 'error', signal: AbortSignal.timeout(10000),
        headers: { 'Content-Type': 'application/json', ...headers },
        body: body === undefined ? undefined : JSON.stringify(body)
    });
    assert.equal((await request('/admin_api/channelHub/health')).status, 401, 'FIXTURE_ADMIN_AUTH');
    assert.equal((await request('/v1/chat/completions')).status, 401, 'FIXTURE_API_BEARER_AUTH');
    assert.equal((await request('/admin_api/channelHub/health', undefined, ADMIN)).status, 200, 'FIXTURE_HEALTH');
    for (const asset of ['/ui', '/ui/app.js', '/ui/style.css']) {
        const response = await request('/admin_api/channelHub' + asset, undefined, ADMIN);
        assert.equal(response.status, 200, 'FIXTURE_UI_ASSET');
        assert.ok((await response.text()).length > 0, 'FIXTURE_UI_EMPTY');
    }
    const catalog = await (await request('/admin_api/channelHub/agents', undefined, ADMIN)).json();
    assert.ok(catalog.data.agents.some(agent => agent.id === AGENT), 'FIXTURE_AGENT_CATALOG');

    const suffix = crypto.randomUUID();
    const adapterId = 'pilot-' + suffix;
    const created = await request('/admin_api/channelHub/adapters', {
        adapterId, channel: 'dingtalk', name: 'Synthetic development adapter', status: 'active',
        config: { bridgeKey: BRIDGE_KEY, agentId: AGENT }
    }, ADMIN);
    assert.equal(created.status, 201, 'FIXTURE_ADAPTER_CREATE');
    const envelope = {
        version: '2.0', eventId: 'event-' + suffix, adapterId, channel: 'dingtalk',
        eventType: 'message.created', occurredAt: Date.now(), target: { agentId: AGENT },
        client: { messageId: 'message-' + suffix, conversationId: 'pilot-synthetic-chat' },
        sender: { userId: 'pilot-synthetic-user' },
        payload: { messages: [{ role: 'user', content: 'Synthetic smoke request' }] }
    };
    const headers = { 'x-channel-adapter-id': adapterId, 'x-channel-bridge-key': BRIDGE_KEY };
    const endpoint = '/plugin-callback/ChannelHub/events';
    assert.equal((await request(endpoint, envelope)).status, 404, 'FIXTURE_CALLBACK_HEADERS');
    assert.equal((await request(endpoint, envelope, { ...headers, 'x-channel-bridge-key': 'wrong' })).status, 401, 'FIXTURE_CALLBACK_AUTH');
    const answer = await request(endpoint, envelope, headers);
    assert.equal(answer.status, 200, 'FIXTURE_CALLBACK_FAILED');
    const result = await answer.json();
    assert.equal(result.status, 'success', 'FIXTURE_REPLY_STATUS');
    assert.equal(result.jobId, null, 'FIXTURE_INLINE_REQUIRED');
    assert.equal(result.reply.messages[0].content[0].text, ANSWER, 'FIXTURE_MOCK_REPLY');
    assert.equal(mockCalls, 1, 'FIXTURE_MOCK_CALL_COUNT');
    const replay = await (await request(endpoint, envelope, headers)).json();
    assert.equal(replay.status, 'duplicate', 'FIXTURE_DEDUP');
    assert.equal(mockCalls, 1, 'FIXTURE_DEDUP_MOCK_CALL_COUNT');
    assert.equal((await request(endpoint, { ...envelope, eventId: 'forged-' + suffix, adapterId: 'different' }, headers)).status, 400, 'FIXTURE_IDENTITY_MISMATCH');
    const outbox = await (await request('/admin_api/channelHub/outbox', undefined, ADMIN)).json();
    assert.deepEqual(outbox.data, [], 'FIXTURE_NO_OUTBOX_DELIVERY');

    const checks = {
        nativePluginManager: true, adminAuth: true,
        bearerGate: { mode: authMode, unauthenticatedRejected: true }, health: true, ui: true,
        agentCatalog: true, callbackAuth: true, inlineMock: true,
        deduplication: true, forgedIdentityRejected: true, noOutbox: true,
        mockCalls
    };
    signalsReady = true;
    if (pendingSignal) { await shutdown(); return; }
    if (smoke[0] === '--smoke') {
        plugin.beginDrain();
        assert.equal((await request(endpoint, { ...envelope, eventId: 'drain-' + suffix }, headers)).status, 503, 'FIXTURE_DRAIN');
        await shutdown();
        assert.equal(plugin.snapshot().ready, false, 'FIXTURE_SHUTDOWN');
        const verification = {
            fixture: 'channelhub-dev', hostProfile, authMode, mockVerified: true, checks,
            drain: true, shutdown: true
        };
        fs.writeFileSync(path.join(path.dirname(readyPath), 'verification.json'),
            JSON.stringify(verification) + '\n', { mode: 0o600, flag: 'wx' });
        console.log(JSON.stringify(verification));
        return;
    }
    // Publication is last: devctl must never expose an incompletely checked host.
    const temporary = readyPath + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify({
        fixture: 'channelhub-dev', hostProfile, authMode, mode: 'synthetic-only', mockVerified: true,
        uiRoute: '/admin_api/channelHub/ui', checks
    }) + '\n', { mode: 0o600, flag: 'wx' });
    fs.renameSync(temporary, readyPath);
}

main().catch(async error => {
    // Fixture inputs are synthetic, but do not turn diagnostics into a payload
    // dump. Native tests provide the detailed behavior regression evidence.
    console.error('FIXTURE_START_FAILED:' + (error.code || error.name) + ':' + String(error.message).split('\n')[0]);
    try { await shutdown(); } catch { console.error('FIXTURE_SHUTDOWN_FAILED'); }
    process.exitCode = 1;
});
