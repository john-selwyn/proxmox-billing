const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

// Execute the actual inline poller against a minimal browser and delayed replies.
const template = readFileSync(join(__dirname, 'templates/portal/provisioning_status.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));
function page(dataset, replies, storage = new Map()) {
  let now = 1000000;
  const intervals = new Map();
  let intervalId = 0;
  const timers = [];
  let reloads = 0;
  let requests = 0;
  const elements = {};
  for (const id of ['status', 'step', 'percent', 'bar', 'vmid', 'ip']) {
    elements['provisioning-' + id] = {textContent: '', style: {}, classList: {add() {}}};
  }
  for (const id of ['setup-vps-state', 'setup-access-state', 'access-wait-status',
                    'access-wait-message', 'access-wait-elapsed', 'access-next-check']) {
    elements[id] = {textContent: '', hidden: false, className: ''};
  }
  elements['provisioning-status'].dataset = {
    orderId: '20', statusUrl: '/orders/20/provisioning-status/', initialStatus: 'ACTIVE',
    operatingSystem: 'Windows 11', initialAccess: '', initialIp: '10.60.0.19', ...dataset
  };
  vm.runInNewContext(script, {
    document: {getElementById: id => elements[id]},
    window: {location: {reload() {reloads++;}}, addEventListener() {}},
    Date: {now: () => now},
    sessionStorage: {getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setInterval: fn => { intervals.set(++intervalId, fn); return intervalId; },
    clearInterval: id => intervals.delete(id),
    setTimeout: (fn, delay) => timers.push({fn, delay}),
    fetch: async () => {
      requests++;
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      return {ok: true, json: async () => reply};
    }
  });
  return {elements, timers, storage, reloads: () => reloads, requests: () => requests,
    tick: ms => {now += ms; intervals.forEach(fn => fn());}, intervals};
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


test('waiting copy distinguishes VM completion from access and counts status checks', async () => {
  const p = page({}, [{...active}]);
  await flush();
  assert.equal(p.elements['setup-vps-state'].textContent, 'VPS running');
  assert.equal(p.elements['setup-access-state'].textContent, 'Preparing Remote Desktop');
  assert.match(p.elements['access-wait-message'].textContent, /retries automatically/);
  assert.equal(p.elements['access-next-check'].textContent, 'Next status check in 3s');
  p.tick(1000);
  assert.equal(p.elements['access-next-check'].textContent, 'Next status check in 2s');
  assert.equal(p.elements['access-wait-elapsed'].textContent, 'Waiting 0m 01s');
});
test('long waits show order-specific help without claiming a completion deadline', async () => {
  const p = page({}, [{...active}]);
  await flush();
  p.tick(301000);
  assert.match(p.elements['access-wait-message'].textContent, /Automatic retries continue/);
  assert.match(p.elements['access-wait-message'].textContent, /order #20/);
  assert.equal(p.elements['access-wait-elapsed'].textContent, 'Waiting 5m 01s');
});
test('wait start survives page reload, then readiness clears it and the timer', async () => {
  const storage = new Map([['vps-access-wait-20', '880000']]);
  const p = page({}, [{...active, rdp_host: 'example.test', rdp_port: 22000}], storage);
  assert.equal(p.elements['access-wait-elapsed'].textContent, 'Waiting 2m 00s');
  await flush();
  assert.equal(p.elements['access-wait-status'].hidden, true);
  assert.equal(storage.has('vps-access-wait-20'), false);
  assert.equal(p.intervals.size, 0);
});
