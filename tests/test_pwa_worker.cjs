const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('app/static/pwa/service-worker.js', 'utf8');
function worker(fetch) {
  const handlers = {};
  vm.runInNewContext(code, {URL, Response, fetch, self: {
    location: {origin: 'https://vpn.example'},
    addEventListener: (name, fn) => { handlers[name] = fn; },
    skipWaiting: () => Promise.resolve(), clients: {claim: () => Promise.resolve()}
  }});
  return handlers;
}
function request(path, method = 'GET', mode = 'navigate') {
  return {url: 'https://vpn.example'+path, method, mode};
}
async function navigate(handlers, req) {
  let result;
  handlers.fetch({request: req, respondWith: value => {result = value;}});
  return result;
}
test('always fetch fresh user page and preserve server errors', async () => {
  const response = new Response('disabled', {status: 404});
  const handlers = worker(async (req, options) => {
    assert.equal(options.cache, 'no-store');
    return response;
  });
  assert.equal(await navigate(handlers, request('/client/test')), response);
});
test('offline fallback contains no cached profile or token', async () => {
  const handlers = worker(async () => {throw new Error('offline');});
  const response = await navigate(handlers, request('/client/personal-secret'));
  assert.equal(response.status, 503);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  const html = await response.text();
  assert.match(html, /Нет соединения/);
  assert.ok(!html.includes('personal-secret'));
});
test('leave admin, external pages, POST and config downloads untouched', async () => {
  const handlers = worker(() => {throw new Error('must not fetch');});
  for (const req of [request('/admin'), request('/client/test/ip-change','POST'),
    request('/amnezia-key/test','GET','cors'), request('/client/test/manifest.webmanifest','GET','cors'),
    {...request('/client/test'), url:'https://another.example/client/test'}]) {
    assert.equal(await navigate(handlers, req), undefined);
  }
});

test('install prompt is offered once and hidden after successful install', async () => {
  const listeners = {};
  const button = {hidden: true, addEventListener: (name, fn) => {listeners['button:'+name] = fn;}};
  const section = {hidden: false};
  const help = {};
  let registration;
  const window = {isSecureContext: true, matchMedia: () => ({matches:false}),
    addEventListener: (name, fn) => {listeners[name] = fn;}};
  const navigator = {serviceWorker: {register: (url, options) => {
    registration = {url, options}; return Promise.resolve();
  }}};
  vm.runInNewContext(fs.readFileSync('app/static/pwa/install.js','utf8'), {
    window, navigator, document: {querySelector: selector => ({
      '[data-pwa-install]':button, '[data-pwa-help]':help, '[data-pwa-section]':section
    })[selector]}
  });
  assert.equal(registration.url, '/client/service-worker.js');
  assert.equal(registration.options.scope, '/client/');
  let prompts = 0;
  listeners.beforeinstallprompt({preventDefault:()=>{}, prompt:async()=>{prompts++;}, userChoice:Promise.resolve({outcome:'accepted'})});
  assert.equal(button.hidden, false);
  await listeners['button:click']();
  await listeners['button:click']();
  assert.equal(prompts, 1);
  listeners.appinstalled();
  assert.equal(button.hidden, true);
  assert.equal(section.hidden, true);
});
