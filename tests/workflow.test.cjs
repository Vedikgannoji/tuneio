const {test} = require('node:test');
const assert = require('node:assert/strict');
const {run} = require('../tuneio/workflow.js');

function server() {
  let user, product, order;
  const calls = [];
  async function request(method, path, body) {
    calls.push({method, path, body});
    let status = 200, data;
    if (path === '/cache/mode') data = {mode: body.mode};
    else if (method === 'POST' && path === '/api/users') {
      if (user && user.email === body.email) {status = 409; data = {detail: 'Duplicate'};}
      else {user = {...body, id: 47}; data = {...user}; status = 201;}
    } else if (method === 'POST' && path === '/api/products') {
      if (body.price_cents <= 0) {status = 422; data = {detail: 'Invalid price'};}
      else {product = {...body, id: 92}; data = {...product}; status = 201;}
    } else if (method === 'POST' && path === '/api/orders') {
      assert.equal(body.user_id, user.id); assert.equal(body.product_id, product.id);
      order = {...body, id: 123, status: 'pending', unit_price_cents: product.price_cents}; data = {...order}; status = 201;
    } else if (method === 'PATCH' && path === '/api/users/47') {Object.assign(user, body); data = {...user};}
    else if (method === 'PATCH' && path === '/api/products/92') {Object.assign(product, body); data = {...product};}
    else if (method === 'PATCH' && path === '/api/orders/123/status') {
      if (order.status === 'shipped' && body.status === 'cancelled') {status = 409; data = {detail: 'Invalid transition'};}
      else {Object.assign(order, body); data = {...order};}
    } else if (path.startsWith('/api/reports/sales')) data = product && order ? [{product_id: product.id, name: product.name, units: 2, revenue_cents: 4000}] : [];
    else if (path === '/api/users/47') data = {...user};
    else if (path === '/api/products/92') data = {...product};
    else if (path === '/api/orders/123') data = {...order};
    else if (method === 'GET' && /\/api\/(users|products|orders)\?/.test(path)) data = [];
    else throw Error('Unexpected request: ' + method + ' ' + path);
    return {status, data, cache: method === 'GET' ? 'MISS' : '-'};
  }
  return {calls, request};
}

test('Full workflow chains returned IDs, covers 13 operations, accepts expected failures', async () => {
  const fake = server();
  const report = await run({reads: 2, delayMs: 0, enableAdaptive: true}, fake.request);
  assert.equal(report.state, 'completed');
  assert.equal(report.results.length, 27); // 19 + 4 reads + 3 validation + setup
  assert(report.results.every(r => r.passed));
  assert.equal(report.contexts[0].order.id, 123);
  const operations = new Set(fake.calls.filter(c => c.path.startsWith('/api/')).map(c => c.method + ' ' + c.path.split('?')[0].replace(/\/\d+/g, '/{id}')));
  assert.equal(operations.size, 13);
  assert.deepEqual(fake.calls.filter(c => c.path.endsWith('/status')).map(c => c.body.status), ['paid', 'shipped', 'cancelled']);
  assert.equal(report.results.filter(r => r.expected >= 400).length, 3);
});

test('Read-only preset makes only GETs and respects cycle/read counts', async () => {
  const fake = server();
  const report = await run({preset: 'reads', reads: 3, cycles: 2, delayMs: 0}, fake.request);
  assert.equal(report.state, 'completed'); assert.equal(fake.calls.length, 24);
  assert(fake.calls.every(c => c.method === 'GET'));
});

test('Unexpected status stops dependencies instead of creating an invalid order', async () => {
  const fake = server();
  const report = await run({delayMs: 0}, (method, path, body) => path === '/api/products' ? {status: 503, data: {detail: 'Unavailable'}} : fake.request(method, path, body));
  assert.equal(report.state, 'failed'); assert.equal(report.results.length, 2);
  assert.match(report.results[1].error, /Expected HTTP 201/);
  assert(!fake.calls.some(c => c.path === '/api/orders'));
});

test('Wrong response data fails even when status is successful', async () => {
  const report = await run({delayMs: 0}, async () => ({status: 201, data: {id: null}}));
  assert.equal(report.state, 'failed'); assert.match(report.results[0].error, /correctness/);
});

test('Stop completes the current request but schedules no further writes', async () => {
  const fake = server(), controller = new AbortController();
  const report = await run({delayMs: 1000}, async (...args) => {const response = await fake.request(...args); controller.abort(); return response;}, () => {}, controller.signal);
  assert.equal(report.state, 'stopped'); assert.equal(fake.calls.length, 1);
  assert.equal(report.contexts[0].user.id, 47); assert(report.results[0].passed);
});

test('Network failure produces an inspectable failed result', async () => {
  const report = await run({delayMs: 0}, async () => {throw Error('Offline');});
  assert.equal(report.state, 'failed'); assert.equal(report.results[0].error, 'Offline');
});

test('Unique emails across runs/cycles and bounded input validation', async () => {
  const fake = server();
  const first = await run({cycles: 2, reads: 1, validation: false, delayMs: 0}, fake.request);
  const second = await run({reads: 1, validation: false, delayMs: 0}, server().request);
  assert.equal(first.state, 'completed'); assert.equal(second.state, 'completed');
  assert.equal(new Set([...first.contexts, ...second.contexts].map(c => c.email)).size, 3);
  let calls = 0;
  await assert.rejects(run({cycles: 100}, async () => {calls++;}), /1-5/);
  assert.equal(calls, 0);
});

test('Stop during pacing interrupts the delay promptly', async () => {
  const controller = new AbortController();
  const fake = server();
  const task = run({delayMs: 2000}, fake.request, result => {
    if (result.state === 'passed') setTimeout(() => controller.abort(), 5);
  }, controller.signal);
  const report = await task;
  assert.equal(report.state, 'stopped'); assert.equal(report.results.length, 1);
});

test('Dashboard public fetch omits ambient cookies while preserving request options', async () => {
  const fs = require('node:fs'), vm = require('node:vm');
  const html = fs.readFileSync(require('node:path').join(__dirname, '../tuneio/dashboard.html'), 'utf8');
  const helper = html.slice(html.indexOf('function publicFetch('), html.indexOf('function node('));
  const calls = [];
  const context = vm.createContext({fetch: (url, options) => {calls.push({url, options}); return Promise.resolve('ok');}});
  vm.runInContext(helper, context);
  await vm.runInContext("publicFetch('/api/products', {method:'GET', credentials:'include'})", context);
  await vm.runInContext("publicFetch('/api/orders', {method:'POST', body:'{}'})", context);
  assert(calls.every(c => c.options.credentials === 'omit'));
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.body, '{}');
  // All dashboard fetch paths (manual, workflow and monitoring) use the helper.
  assert.equal((html.match(/\bfetch\(/g) || []).length, 1);
});
