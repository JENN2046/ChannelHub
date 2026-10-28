'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const plugin = path.join(root, 'Plugin', 'ChannelHub');
const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true })
  .flatMap(entry => entry.isDirectory()
    ? walk(path.join(dir, entry.name))
    : [path.relative(plugin, path.join(dir, entry.name)).split(path.sep).join('/')]);

test('frozen 0.4.1 plugin structure is intact', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(plugin, 'plugin-manifest.json'), 'utf8'));
  assert.equal(manifest.name, 'ChannelHub');
  assert.equal(manifest.version, '0.4.1');
  assert.equal(manifest.pluginType, 'service');
  assert.equal(manifest.communication.protocol, 'direct');
  assert.equal(manifest.entryPoint.script, 'channel-hub.js');
  const files = walk(plugin);
  assert.equal(files.length, 46, 'frozen 0.4.1 must contain exactly 46 plugin files');
  for (const required of ['api/adapter-api.mjs', 'core/ChannelHubService.js',
      'core/StateStore.js', 'routes/admin/channelHub.js', 'ui/index.html',
      'host-runtime.js', 'config.env.example', 'SOURCE_PROVENANCE.json']) {
    assert.ok(files.includes(required), 'missing ' + required);
  }
});

test('source tree excludes runtime credentials and mutable data', () => {
  const files = walk(plugin);
  const forbidden = new Set(['config.env', '.env', 'state', 'logs', 'cache', 'node_modules']);
  for (const file of files) {
    assert.ok(!file.split('/').some(segment => forbidden.has(segment)),
      'runtime state or private config unexpectedly committed: ' + file);
    assert.doesNotMatch(file, /\.(?:pem|key|sqlite|sqlite3|db|log)$/,
      'sensitive artifact unexpectedly committed: ' + file);
  }
});

test('host integration remains an explicit dependency', () => {
  const source = fs.readFileSync(path.join(plugin, 'channel-hub.js'), 'utf8');
  assert.ok(source.includes("require('../../modules/agentManager')"));
  const example = fs.readFileSync(path.join(plugin, 'config.env.example'), 'utf8');
  assert.match(example, /ChannelHubEnabled=false/);
  assert.ok(fs.existsSync(path.join(root, 'LICENSE')));
});
