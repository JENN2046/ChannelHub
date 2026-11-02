'use strict';

// Run only in the disposable pinned-host sandbox. These tests exercise the
// stable plugin's native host client; they do not import a reference host core
// into the target profile or exercise any real provider/authentication setup.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { spawnSync } = require('node:child_process');
const { HostRuntime } = require('../Plugin/ChannelHub/host-runtime');
const plugin = require('../Plugin/ChannelHub/channel-hub');

const agents = {
    isAgent: name => name === 'synthetic',
    getAgentPrompt: async () => 'SYNTHETIC_AGENT'
};
const envelope = { payload: { messages: [{ role: 'user', content: 'synthetic request' }] } };
const route = { agentId: 'synthetic' };

async function host(t, handler, timeoutMs = 2000) {
    const server = http.createServer(handler);
    await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', resolve);
    });
    const runtime = new HostRuntime({ agentManager: agents, model: 'synthetic-model', timeoutMs });
    runtime.setEndpoint({
        url: `http://127.0.0.1:${server.address().port}/v1/chat/completions`,
        key: 'synthetic-host'
    });
    t.after(async () => {
        await runtime.close();
        await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
    });
    return runtime;
}

test('disabled plugin allocates no storage and does not import active runtime', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'channelhub-compat-disabled-'));
    try {
        const entry = path.resolve(__dirname, '../Plugin/ChannelHub/channel-hub');
        const code = `
            const Module = require('node:module'), original = Module._load;
            Module._load = function (id, ...args) {
                if (id.includes('ChannelHubService') || id === 'express') throw Error('DISABLED_ACTIVE_IMPORT');
                return original.call(this, id, ...args);
            };
            require(${JSON.stringify(entry)}).initialize({
                ChannelHubDataDir: ${JSON.stringify(path.join(root, 'data'))}
            }).catch(() => { process.exitCode = 1; });
        `;
        const result = spawnSync(process.execPath, ['-e', code], { encoding: 'utf8', timeout: 10000 });
        assert.equal(result.status, 0, 'DISABLED_PLUGIN_MUST_EXIT_CLEANLY');
        assert.deepEqual(fs.readdirSync(root), []);
    } finally {
        fs.rmSync(root, { recursive: true });
    }
});

test('enabled plugin refuses missing data path or host authority before allocation', async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'channelhub-compat-config-'));
    try {
        await assert.rejects(plugin.initialize({ ChannelHubEnabled: true }), /ABSOLUTE_DATA_DIR_REQUIRED/);
        await assert.rejects(plugin.initialize({
            ChannelHubEnabled: true, ChannelHubDataDir: path.join(root, 'data'),
            ChannelHubModel: 'synthetic-model'
        }), /HOST_PORT_OR_KEY_UNAVAILABLE/);
        assert.equal(plugin.snapshot().ready, false);
        assert.deepEqual(fs.readdirSync(root), []);
    } finally {
        await plugin.shutdown();
        fs.rmSync(root, { recursive: true });
    }
});

test('native host HTTP 503 rejects without a fabricated reply', async t => {
    let calls = 0;
    const runtime = await host(t, async (req, res) => {
        for await (const _chunk of req) { /* consume synthetic body */ }
        calls++;
        assert.equal(req.url, '/v1/chat/completions');
        assert.equal(req.headers.authorization, 'Bearer synthetic-host');
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end('{"error":"synthetic-unavailable"}');
    });
    await assert.rejects(runtime.invoke(envelope, route), /CHANNELHUB_HOST_HTTP_503/);
    assert.equal(calls, 1);
    assert.equal(runtime.pending.size, 0);
    assert.equal(runtime.controllers.size, 0);
});

test('native host timeout rejects and clears pending request ownership', async t => {
    let calls = 0;
    const runtime = await host(t, async req => {
        for await (const _chunk of req) { /* leave response pending deliberately */ }
        calls++;
    }, 1000);
    await assert.rejects(runtime.invoke(envelope, route), error => error.name === 'AbortError');
    assert.equal(calls, 1);
    assert.equal(runtime.pending.size, 0);
    assert.equal(runtime.controllers.size, 0);
});

test('drain aborts an in-flight native request and refuses new execution', async t => {
    let entered;
    const received = new Promise(resolve => { entered = resolve; });
    const runtime = await host(t, async req => {
        for await (const _chunk of req) { /* response intentionally held */ }
        entered();
    });
    const operation = runtime.invoke(envelope, route);
    const rejection = assert.rejects(operation, error => error.name === 'AbortError');
    await received;
    await runtime.close();
    await rejection;
    await assert.rejects(runtime.invoke(envelope, route), /CHANNELHUB_RUNTIME_NOT_READY/);
    assert.equal(runtime.pending.size, 0);
    assert.equal(runtime.controllers.size, 0);
});
