const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

// Execute the actual inline poller against a minimal browser and delayed replies.
const template = readFileSync(join(__dirname, 'templates/billing/provisioning_status.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));
function page(dataset, replies) {
  const timers = [];
  let reloads = 0;
  let requests = 0;
  const elements = {};
  for (const id of ['status', 'step', 'percent', 'bar', 'vmid', 'ip']) {
    elements['provisioning-' + id] = {textContent: '', style: {}, classList: {add() {}}};
  }
  elements['provisioning-status'].dataset = {
    orderId: '20', statusUrl: '/orders/20/provisioning-status/', initialStatus: 'ACTIVE',
    operatingSystem: 'Windows 11', initialAccess: '', initialIp: '10.60.0.19', ...dataset
  };
  vm.runInNewContext(script, {
    document: {getElementById: id => elements[id]},
    window: {location: {reload() {reloads++;}}},
    setTimeout: (fn, delay) => timers.push({fn, delay}),
    fetch: async () => {
      requests++;
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      return {ok: true, json: async () => reply};
    }
  });
  return {elements, timers, reloads: () => reloads, requests: () => requests};
}
const active = {status: 'ACTIVE', progress: 100, vmid: 1020, ip_address: '10.60.0.19',
  ssh_host: '', ssh_port: 22, rdp_host: '', rdp_port: 3389};

test('activation reloads to render power controls even while RDP is preparing', async () => {
  const p = page({initialStatus: 'PROVISIONING'}, [{...active}]);
  await flush();
  assert.equal(p.timers[0].delay, 700);
  p.timers.shift().fn();
  assert.equal(p.reloads(), 1);
  assert.equal(p.timers.length, 0);
});
test('active Windows page keeps polling until delayed RDP is ready', async () => {
  const p = page({}, [{...active}, {...active, rdp_host: 'example.test', rdp_port: 22000}]);
  await flush();
  assert.match(p.elements['provisioning-step'].textContent, /Preparing remote access/);
  assert.equal(p.timers[0].delay, 3000);
  await p.timers.shift().fn();
  assert.match(p.elements['provisioning-step'].textContent, /Connection details are available/);
  assert.equal(p.timers[0].delay, 700);
  p.timers.shift().fn();
  assert.equal(p.reloads(), 1);
});
test('Linux waits for SSH rather than accepting an RDP endpoint', async () => {
  const p = page({operatingSystem: 'Debian 13'}, [
    {...active, rdp_host: 'example.test'}, {...active, ssh_host: 'example.test', ssh_port: 22015}
  ]);
  await flush();
  assert.equal(p.timers[0].delay, 3000);
  await p.timers.shift().fn();
  assert.equal(p.timers[0].delay, 700);
});
test('a fully rendered active Windows page does not poll or reload endlessly', async () => {
  const p = page({initialAccess: 'ready'}, []);
  await flush();
  assert.equal(p.requests(), 0);
  assert.equal(p.timers.length, 0);
});
test('failure stops polling and preserves the order reference', async () => {
  const p = page({initialStatus: 'PROVISIONING'}, [{status: 'FAILED', progress: 98}]);
  await flush();
  assert.equal(p.timers.length, 0);
  assert.match(p.elements['provisioning-step'].textContent, /order #20/);
});
test('a temporary fetch error retries rather than freezing the page', async () => {
  const p = page({}, [new Error('temporary failure')]);
  await flush();
  assert.equal(p.timers[0].delay, 3000);
  assert.equal(p.reloads(), 0);
});
