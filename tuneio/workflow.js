/* Sequential API scenarios shared by the browser and Node's built-in tests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.tuneioWorkflow = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function delay(ms, signal) {
    return new Promise(resolve => {
      if (!ms || signal?.aborted) return resolve();
      const finish = () => { clearTimeout(timer); signal?.removeEventListener('abort', finish); resolve(); };
      const timer = setTimeout(finish, ms);
      signal?.addEventListener('abort', finish, {once: true});
    });
  }
  function plan(options, context) {
    const steps = [];
    const add = (label, method, path, body, expected = 200, check = null, capture = null) =>
      steps.push({label, method, path, body, expected, check, capture});
    const repeated = (label, path) => {
      for (let i = 0; i < options.reads; i++) add(`${label} ${i + 1}/${options.reads}`, 'GET', path);
    };
    if (options.preset === 'reads') {
      for (const path of ['/api/users?limit=5', '/api/products?limit=5', '/api/orders?limit=5', '/api/reports/sales'])
        repeated('Read and observe cache', path);
      return steps;
    }
    const idCheck = data => Number.isSafeInteger(data?.id) && data.id > 0;
    add('Create user', 'POST', '/api/users', {name: 'Workflow user', email: context.email}, 201, idCheck, 'user');
    add('Create product', 'POST', '/api/products', {name: 'Workflow product', price_cents: 1500}, 201, idCheck, 'product');
    add('List users', 'GET', '/api/users?limit=5', null, 200, Array.isArray);
    add('Read created user', 'GET', () => `/api/users/${context.user.id}`, null, 200, d => d.id === context.user.id);
    add('Update user', 'PATCH', () => `/api/users/${context.user.id}`, {name: 'Updated workflow user'}, 200, d => d.name === 'Updated workflow user');
    add('Verify user update', 'GET', () => `/api/users/${context.user.id}`, null, 200, d => d.name === 'Updated workflow user');
    add('List products', 'GET', '/api/products?limit=5', null, 200, Array.isArray);
    add('Read created product', 'GET', () => `/api/products/${context.product.id}`, null, 200, d => d.id === context.product.id);
    add('Set product price', 'PATCH', () => `/api/products/${context.product.id}`, {price_cents: 2000}, 200, d => d.price_cents === 2000);
    add('Create order from returned IDs', 'POST', '/api/orders', () => ({user_id: context.user.id, product_id: context.product.id, quantity: 2}), 201, d => idCheck(d) && d.unit_price_cents === 2000, 'order');
    add('List orders', 'GET', '/api/orders?limit=5', null, 200, Array.isArray);
    add('Read created order', 'GET', () => `/api/orders/${context.order.id}`, null, 200, d => d.user_id === context.user.id && d.product_id === context.product.id);
    repeated('Observe product caching', () => `/api/products/${context.product.id}`);
    repeated('Observe report caching', '/api/reports/sales');
    add('Change product after order creation', 'PATCH', () => `/api/products/${context.product.id}`, {name: 'Tuned workflow product', price_cents: 2500}, 200, d => d.price_cents === 2500);
    add('Verify original order price is preserved', 'GET', () => `/api/orders/${context.order.id}`, null, 200, d => d.unit_price_cents === 2000);
    add('Verify fresh sales report', 'GET', '/api/reports/sales', null, 200, data => {
      const row = Array.isArray(data) && data.find(d => d.product_id === context.product.id);
      return row && row.name === 'Tuned workflow product' && row.revenue_cents === 4000 && row.units === 2;
    });
    add('Pay order', 'PATCH', () => `/api/orders/${context.order.id}/status`, {status: 'paid'}, 200, d => d.status === 'paid');
    add('Verify paid order', 'GET', () => `/api/orders/${context.order.id}`, null, 200, d => d.status === 'paid');
    add('Ship order', 'PATCH', () => `/api/orders/${context.order.id}/status`, {status: 'shipped'}, 200, d => d.status === 'shipped');
    add('Verify shipped order', 'GET', () => `/api/orders/${context.order.id}`, null, 200, d => d.status === 'shipped');
    if (options.validation) {
      add('Reject invalid price (expected)', 'POST', '/api/products', {name: 'Invalid', price_cents: 0}, 422);
      add('Reject duplicate email (expected)', 'POST', '/api/users', {name: 'Duplicate', email: context.email}, 409);
      add('Reject shipped-to-cancelled transition (expected)', 'PATCH', () => `/api/orders/${context.order.id}/status`, {status: 'cancelled'}, 409);
    }
    return steps;
  }
  async function run(settings, request, onStep = () => {}, signal) {
    const options = {preset: 'full', cycles: 1, reads: 6, delayMs: 100, validation: true, enableAdaptive: false, ...settings};
    if (!['full', 'reads'].includes(options.preset) || !Number.isInteger(options.cycles) || options.cycles < 1 || options.cycles > 5 ||
        !Number.isInteger(options.reads) || options.reads < 1 || options.reads > 30 ||
        !Number.isInteger(options.delayMs) || options.delayMs < 0 || options.delayMs > 2000)
      throw Error('Use 1-5 cycles, 1-30 repeated reads, and a delay of 0-2000 ms.');
    const report = {startedAt: new Date().toISOString(), options, contexts: [], results: [], state: 'running'};
    const started = performance.now();
    // A fresh token per run and cycle avoids collisions across tabs and reruns.
    const token = settings.runId || (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`);
    let failed = false;
    async function execute(step, cycle) {
      if (signal?.aborted) return false;
      const path = typeof step.path === 'function' ? step.path() : step.path;
      const body = typeof step.body === 'function' ? step.body() : step.body;
      const result = {index: report.results.length + 1, cycle, label: step.label, method: step.method, path, body: body || null, expected: step.expected, state: 'running'};
      report.results.push(result);
      onStep({...result});
      const begin = performance.now();
      try {
        const response = await request(step.method, path, body);
        Object.assign(result, {status: response.status, data: response.data, cache: response.cache || '-', cacheReason: response.cacheReason || null, durationMs: performance.now() - begin});
        result.passed = response.status === step.expected && (!step.check || Boolean(step.check(response.data)));
        if (!result.passed) result.error = response.status !== step.expected ? `Expected HTTP ${step.expected}; received ${response.status}` : 'Response correctness check failed';
      } catch (error) {
        Object.assign(result, {passed: false, error: error.message, durationMs: performance.now() - begin});
      }
      result.state = result.passed ? 'passed' : 'failed';
      onStep({...result});
      if (!result.passed) { failed = true; return false; }
      if (step.capture) step.context[step.capture] = result.data;
      await delay(options.delayMs, signal);
      return true;
    }
    if (options.enableAdaptive) await execute({label: 'Enable adaptive caching (global mode)', method: 'PUT', path: '/cache/mode', body: {mode: 'adaptive'}, expected: 200}, 0);
    for (let cycle = 1; cycle <= options.cycles && !failed && !signal?.aborted; cycle++) {
      const context = {cycle, email: `workflow-${token}-${cycle}@example.com`};
      report.contexts.push(context);
      for (const step of plan(options, context)) {
        step.context = context;
        if (!await execute(step, cycle)) break;
      }
    }
    report.state = failed ? 'failed' : signal?.aborted ? 'stopped' : 'completed';
    report.durationMs = performance.now() - started;
    report.finishedAt = new Date().toISOString();
    return report;
  }
  return {run};
});
