const {test}=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../tuneio/product_ui.js');

test('deep links and legacy aliases resolve to a single real view',()=>{
  for(const [hash,view] of [['#cache','cache'],['#policies','cache'],['#playground','playground'],['#tester','playground'],['#guide','guide'],['#unknown','overview']])assert.equal(ui.route(hash),view);
});
test('no traffic means unavailable latency and rates, rather than fabricated measurements',()=>{
  const s=ui.summary({count:0,requests_per_second:0,mean_ms:null,p95_ms:null,cache_hits:0});
  assert.equal(s.total,'0');assert.equal(s.p95,'— ms');assert.equal(s.errors,'—');assert.equal(s.offload,'—');
  assert.equal(ui.summary({count:4,requests_per_second:1,p95_ms:2,error_rate:.25,cache_hits:2}).offload,'50.0%');
});
test('history removes queries and never keeps bodies or explicit credentials',()=>{
  const history=ui.safeHistory('GET','/items?token=secret',{status:200,duration_ms:1,cache:'HIT',response_text:'secret',headers:{authorization:'secret'}},2);
  assert.equal(history.operation,'GET /items');assert.ok(!JSON.stringify(history).includes('secret'));
});
test('chart gaps stay missing and zero latency remains a real measured value',()=>{
  const chart=ui.chartModel([{count:0,p95_ms:null},{count:2,p95_ms:0},{count:1,p95_ms:10}]);
  assert.equal(chart[0].y,null);assert.equal(chart[1].y,155);assert.equal(chart[2].y,20);assert.equal(chart[1].height,135);
});
test('theme prefers explicit storage then system, handles unrecognized storage',()=>{
  assert.equal(ui.theme('dark',true),'dark');assert.equal(ui.theme('light',false),'light');assert.equal(ui.theme(null,true),'light');assert.equal(ui.theme('invalid',false),'dark');
});

const {surface}=require('./console_dom.cjs');
test('click/hash/back-forward updates visibility and the active navigation together',()=>{
  const s=surface('#policies');
  for(const name of ['cache','playground','guide','overview','playground','cache']){
    s.context.location.hash='#'+name;s.listeners.hashchange();
    assert.deepEqual(s.views.filter(x=>!x.hidden).map(x=>x.dataset.view),[name]);
    assert.deepEqual(s.links.filter(x=>x.attributes['aria-current']==='page').map(x=>x.dataset.nav),[name]);
  }
  assert.equal(s.get('view-title').textContent,'Cache & insights');
});
test('theme toggles persist only preference; loading does not generate data-plane traffic',()=>{
  const s=surface('#playground','light');assert.equal(s.context.document.documentElement.dataset.theme,'light');
  s.get('theme').onclick();assert.equal(s.storage.get('tuneio-theme'),'dark');
  assert.equal(s.calls.length,1);assert.match(s.calls[0].url,/dashboard\/data/);assert.equal(s.calls[0].options.credentials,'omit');
});
test('keyboard skip link focuses the current view without resetting its route',()=>{
  const s=surface('#cache');let prevented=false;
  s.get('skip-to-main').onclick({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(s.get('main').focused,true);assert.equal(s.context.location.hash,'#cache');
});
test('scenario preview gates writes and any edit invalidates the approval',async()=>{
  const s=surface();
  s.context.fetch=async(url,options)=>({ok:true,json:async()=>({name:'Review',requests:2,writes:1,cycles:1,delay_ms:0,steps:[{method:'POST',path_template:'/items',repeat:1}]})});
  s.get('scenario-source').value='{"name":"Review","steps":[]}';
  await s.get('preview-scenario').onclick();
  assert.equal(s.get('run-scenario').disabled,true);
  s.get('allow-writes').checked=true;s.get('allow-writes').onchange();assert.equal(s.get('run-scenario').disabled,false);
  s.get('scenario-source').oninput();assert.equal(s.get('run-scenario').disabled,true);assert.equal(s.get('allow-writes').checked,false);
});
test('untrusted response content is rendered as text, and capture defaults to off',async()=>{
  const s=surface();s.get('method').value='GET';s.get('path').value='/items?key=secret';s.get('headers').value='{}';
  let payload;
  s.context.fetch=async(url,options)=>{payload=JSON.parse(options.body);return {ok:true,json:async()=>({status:200,duration_ms:1,cache:'MISS',response_text:'<script>alert(1)</script>',headers:{}})};};
  await s.get('request-form').onsubmit({preventDefault(){}});assert.equal(payload.include_response,false);assert.match(s.get('response-text').textContent,/not captured/);
  s.get('inspect').checked=true;await s.get('request-form').onsubmit({preventDefault(){}});
  assert.match(s.get('response-text').textContent,/<script>/);assert.equal(s.get('response-text').children.length,0);
  assert.ok(!JSON.stringify(s.get('request-history').children).includes('secret'));
});
