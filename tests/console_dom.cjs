// Minimal DOM harness for shipped handler and Python-to-JavaScript contract checks.
const fs=require('node:fs');
const vm=require('node:vm');
function surface(hash='#overview',saved=null,snapshot=null){
  class Element{
    constructor(){this.value='';this.dataset={};this.children=[];this.attributes={};this.hidden=false;this.checked=false;this.disabled=false;this.textContent='';this.files=[];this.classList={toggle(){},add(){},remove(){}};}
    append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}
    setAttribute(k,v){this.attributes[k]=v;}removeAttribute(k){delete this.attributes[k];}click(){}focus(){this.focused=true;}
  }
  const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const names=['overview','playground','cache','guide'];
  const views=names.map(name=>Object.assign(new Element(),{dataset:{view:name}}));
  const links=names.map(name=>Object.assign(new Element(),{dataset:{nav:name}}));
  const document={getElementById:get,querySelectorAll:selector=>selector==='[data-view]'?views:links,createElement:()=>new Element(),createElementNS:()=>new Element(),documentElement:new Element(),body:new Element(),hidden:false};
  const listeners={},storage=new Map(saved?[['tuneio-theme',saved]]:[]),calls=[];
  const context={document,location:{hash},addEventListener:(name,fn)=>listeners[name]=fn,localStorage:{getItem:key=>storage.get(key),setItem:(k,v)=>storage.set(k,v)},matchMedia:()=>({matches:false}),setInterval(){},setTimeout(){return 1;},clearTimeout(){},AbortController,performance,crypto:require('node:crypto').webcrypto,console,Blob,URL,
    fetch:(url,options)=>{calls.push({url,options});return snapshot?Promise.resolve({ok:true,json:async()=>snapshot}):new Promise(()=>{});}};
  get('window').value='60';
  vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../tuneio/product_ui.js'),'utf8'),context);
  return {context,get,views,links,listeners,storage,calls};
}
module.exports={surface};
