const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
function setup(){
 const nodes=new Map(), pending=[];
 const context=vm.createContext({Date,Boolean,Number,console});
 context.$=selector=>{if(!nodes.has(selector))nodes.set(selector,{innerHTML:''});return nodes.get(selector)};
 context.api=path=>new Promise(resolve=>pending.push({path,resolve}));
 context.esc=String;
 context.renderDevices=()=>{context.$('#content').innerHTML='devices'};
 context.renderSettings=async()=>{};
 context.window={};
 vm.runInContext("let me={admin:false},page='devices',devices=[],refreshGeneration=0,statsUpdatedAt=0;",context);
 vm.runInContext(source.slice(source.indexOf('async function refresh(){'),source.indexOf('function renderDevices(){')),context);
 return {context,nodes,pending,run:code=>vm.runInContext(code,context)};
}
test('late devices response cannot overwrite users page',async()=>{
 const t=setup(),first=t.run('refresh()');t.run("page='users'");const second=t.run('refresh()');
 t.pending[1].resolve([]);await second;const expected=t.context.$('#content').innerHTML;
 t.pending[0].resolve([{id:1}]);await first;assert.equal(t.context.$('#content').innerHTML,expected);
});
test('older refresh cannot replace newer device data',async()=>{
 const t=setup(),first=t.run('refresh()'),second=t.run('refresh()');
 t.pending[1].resolve([{id:2}]);await second;t.pending[0].resolve([{id:1}]);await first;
 assert.equal(t.run('devices[0].id'),2);
});
test('logout invalidates pending page responses',async()=>{
 const t=setup(),first=t.run('refresh()');t.run('me=null;refreshGeneration++');
 t.pending[0].resolve([{id:1}]);await first;assert.equal(t.nodes.has('#content'),false);
});
test('late statistics cannot write after navigation',async()=>{
 const t=setup();t.run('me.admin=true');const first=t.run('refresh()');
 t.pending[0].resolve([]);await new Promise(resolve=>setImmediate(resolve));
 t.run("page='users'");const second=t.run('refresh()');t.pending.find(p=>p.path==='/admin/users').resolve([]);await second;
 t.pending.find(p=>p.path==='/admin/stats').resolve({free:1024});await first;
 assert.equal(t.nodes.has('#stats'),false);
});
