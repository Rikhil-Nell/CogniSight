/* Execute the shipped polling script against a minimal DOM boundary, without
 * claiming this replaces phone/browser accessibility or TLS acceptance tests. */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../src/cognisight/caregiver/static/dashboard.js'),'utf8');
const indoorSource = fs.readFileSync(path.join(__dirname,'../src/cognisight/caregiver/static/indoor_map.js'),'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() { this.textContent=''; this.children=[]; this.hidden=true; }
  replaceChildren(...children) { this.children=children; }
  appendChild(child) { this.children.push(child); }
  append(...children) { this.children.push(...children); }
}
function sample() {
  return {schema_version:1,has_mock_data:false,health:{camera:'ready'},indoor:{status:'unavailable'},
    recognition:{status:'live',data_source:'live',age_seconds:0,max_age_seconds:1,observed_at:null,
      people:[{name:'Synthetic profile',relationship:'test fixture',note:'<script>untrusted text</script>'}]},
    outdoor:{status:'live',data_source:'live',age_seconds:0,max_age_seconds:10,observed_at:null,
      latitude:0,longitude:0,last_known:null}};
}
async function boot(replies) {
  const elements=new Map(), timers=new Map(), intervals=[], listeners={};
  let now=0, nextId=0;
  const document={hidden:false,
    getElementById(id) { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); },
    createElement() { return new Element(); },
    addEventListener(name,callback) { listeners[name]=callback; }};
  vm.runInNewContext(source,{document,performance:{now:()=>now},AbortController,Date,
    setTimeout(callback,delay) { const id=++nextId; timers.set(id,{callback,delay}); return id; },
    clearTimeout(id) { timers.delete(id); },setInterval(callback) { intervals.push(callback); },
    fetch: async () => {
      const reply=replies.shift();
      if(reply instanceof Error) throw reply;
      return {status:reply.status??200,ok:!reply.status || reply.status===200,redirected:false,
        headers:{get:()=> 'application/json'},json:async()=>reply.data};
    }});
  await flush();
  return {elements,document,listeners,
    tick(seconds) { now+=seconds*1000; intervals.forEach(fn=>fn()); },
    async poll() { const item=[...timers].find(([,v])=>[500,5000].includes(v.delay)); assert.ok(item); timers.delete(item[0]); await item[1].callback(); await flush(); },
    nextPollDelay:()=>[...timers.values()].find(v=>[500,5000].includes(v.delay))?.delay,
    pendingPolls:()=>[...timers.values()].filter(v=>[500,5000].includes(v.delay)).length};
}

test('source freshness expires independently of successful browser polling',async()=>{
  const app=await boot([{data:sample()}]);
  assert.equal(app.elements.get('people').children.length,1);
  assert.match(app.elements.get('coordinates').textContent,/Latitude 0.000000/);
  const note=app.elements.get('people').children[0].children[2];
  assert.equal(note.textContent,'<script>untrusted text</script>');
  app.tick(2);
  assert.equal(app.elements.get('people').children.length,0);
  assert.match(app.elements.get('recognition-status').textContent,/stale/i);
  app.tick(9);
  assert.match(app.elements.get('outdoor-status').textContent,/stale/i);
  assert.doesNotMatch(app.elements.get('coordinates').textContent,/Latitude/);
});

test('expired authentication clears sensitive data and stops polling',async()=>{
  const app=await boot([{data:sample()},{status:401}]);
  await app.poll();
  assert.equal(app.elements.get('people').children.length,0);
  assert.equal(app.elements.get('health').children.length,0);
  assert.match(app.elements.get('connection').textContent,/Session ended/);
  assert.equal(app.pendingPolls(),0);
});

test('network failure clears location and visible mock labels, then retries',async()=>{
  const data=sample(); data.has_mock_data=true; data.recognition.status='mock';
  const app=await boot([{data},new Error('network unavailable')]);
  assert.equal(app.elements.get('demo').hidden,false);
  await app.poll();
  assert.equal(app.elements.get('demo').hidden,true);
  assert.equal(app.elements.get('people').children.length,0);
  assert.match(app.elements.get('connection').textContent,/Disconnected/);
  assert.equal(app.pendingPolls(),1);
});


test('healthy refreshes are faster than recognition expiry',async()=>{
  const app=await boot([{data:sample()},{data:sample()}]);
  assert.ok(app.nextPollDelay()<1000, 'Next refresh must occur before the one-second observation expiry');
  app.tick(.6);
  await app.poll();
  app.tick(.6);
  assert.equal(app.elements.get('people').children.length,1);
  assert.doesNotMatch(app.elements.get('recognition-status').textContent,/stale/i);
});

class MapElement {
  constructor() {
    this.children=[];
    this.listeners={};
    this.attributes={};
    this.textContent='';
    this.value='';
    this.disabled=false;
    this.className='';
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children=children; }
  setAttribute(name,value) { this.attributes[name]=String(value); }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(name,callback) {
    (this.listeners[name] ||= []).push(callback);
  }
  async dispatch(name) {
    const event={target:this,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};
    for (const callback of this.listeners[name] || []) await callback(event);
    return event;
  }
  querySelectorAll(selector) {
    const className=selector.slice(1);
    const found=[];
    const visit=item=>{
      for (const child of item.children) {
        if (child.className.split(/\s+/).includes(className)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
}

async function bootMap(loadSucceeds=true) {
  const elements=new Map(), listeners={};
  const document={
    getElementById(id) {
      if(!elements.has(id)) elements.set(id,new MapElement());
      return elements.get(id);
    },
    createElement() { return new MapElement(); }
  };
  document.getElementById('map-csrf-token').value='synthetic-csrf-token';
  let uuid=0;
  const requests=[];
  vm.runInNewContext(indoorSource,{
    document,
    window:{addEventListener(name,callback){listeners[name]=callback;}},
    crypto:{randomUUID(){uuid+=1;return `00000000-0000-4000-8000-${String(uuid).padStart(12,'0')}`;}},
    fetch:async(url,options={})=>{
      requests.push({url,options});
      if(options.method==='PUT') {
        const map=JSON.parse(options.body);
        return {ok:true,status:200,json:async()=>({saved:true,map})};
      }
      if(!loadSucceeds) return {ok:false,status:503,json:async()=>({error:'map_unavailable'})};
      return {ok:true,status:200,json:async()=>({map:null})};
    }
  });
  await flush();
  return {elements,requests};
}

test('indoor map editor creates rooms, links, landmark metadata and saves with CSRF',async()=>{
  const app=await bootMap();
  const byId=id=>app.elements.get(id);
  assert.match(byId('map-status').textContent,/Name the home map/);
  const name=byId('map-name');
  name.value='Synthetic home';
  await name.dispatch('input');
  await byId('add-room').dispatch('click');
  let roomInputs=byId('rooms-list').querySelectorAll('.map-control');
  roomInputs[0].value='Hall';
  await roomInputs[0].dispatch('input');
  await byId('add-room').dispatch('click');
  roomInputs=byId('rooms-list').querySelectorAll('.map-control');
  roomInputs[1].value='Kitchen';
  await roomInputs[1].dispatch('input');
  await byId('add-connection').dispatch('click');
  const connectionSelects=byId('connections-list').querySelectorAll('.map-control');
  assert.equal(connectionSelects[0].children.length,2);
  await byId('add-landmark').dispatch('click');
  const landmarkInputs=byId('landmarks-list').querySelectorAll('.map-control');
  landmarkInputs[0].value='LM-01';
  await landmarkInputs[0].dispatch('input');
  landmarkInputs[1].value='Hall marker';
  await landmarkInputs[1].dispatch('input');
  await byId('save-map').dispatch('click');
  const save=app.requests.find(request=>request.options.method==='PUT');
  assert.ok(save);
  assert.equal(save.options.headers['X-CSRFToken'],'synthetic-csrf-token');
  const saved=JSON.parse(save.options.body);
  assert.equal(saved.rooms.length,2);
  assert.equal(saved.connections[0].kind,'doorway');
  assert.equal(saved.landmarks[0].id,'LM-01');
  assert.match(byId('map-status').textContent,/Map saved on this Pi/);
  assert.equal(byId('export-map').getAttribute('aria-disabled'),'false');
});

test('failed map load does not enable a blank map for overwriting',async()=>{
  const app=await bootMap(false);
  assert.match(app.elements.get('map-status').textContent,/could not be loaded/);
  assert.equal(app.elements.get('add-room').disabled,true);
  await app.elements.get('add-room').dispatch('click');
  assert.equal(app.requests.filter(request=>request.options.method==='PUT').length,0);
});
