// Pure serialization tests: no browser, network, or AI is used.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync(process.argv[2], 'utf8');
const payload = source.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/)[1];
const script = source.match(/<\/script><script>([\s\S]*?)<\/script>/)[1];
const original = JSON.parse(payload);
const fields = original.rows.map((row, index) => [
  {dataset:{field:'case'},type:'text',value:String(index+1)},
  {dataset:{field:'title'},type:'text',value:'Дело «Ёлка».'},
  {dataset:{field:'start'},type:'text',value:'01.08.1903'},
  {dataset:{field:'end'},type:'text',value:'01.08.1903'},
  {dataset:{field:'quantity'},type:'text',value:index===0?'2':''},
  {dataset:{field:'note'},type:'text',value:'Проверенная пометка'},
  {dataset:{field:'pages'},type:'text',value:String(row.page)},
  {dataset:{field:'same_as'},type:'text',value:''},
  {dataset:{field:'withdrawal'},type:'select',value:index===0?'cancelled':'active'},
  {dataset:{field:'checked'},type:'checkbox',checked:true},
  {dataset:{field:'unresolved'},type:'checkbox',checked:false},
  {dataset:{field:'reversed'},type:'checkbox',checked:false},
]);
const cards = fields.map((inputs, index) => ({dataset:{index:String(index)},querySelectorAll:()=>inputs}));
let exported;
const save = {};
const document = {
  querySelector: selector => selector === '#data' ? {textContent:payload} : save,
  querySelectorAll: () => cards,
  createElement: () => ({click(){}}),
  addEventListener(){},
};
const context = vm.createContext({document, JSON, Number, String, alert(){throw Error('invalid input')},
  Blob:class {constructor(parts){this.parts=parts}},
  URL:{createObjectURL(blob){exported=JSON.parse(blob.parts.join(''));return 'test';},revokeObjectURL(){}}});
new vm.Script(script).runInContext(context);
save.onclick();
assert.equal(exported.reviewed,true);
assert.equal(exported.rows.length,original.rows.length);
assert.equal(exported.rows[0].quantity,2);
assert.equal(exported.rows[0].withdrawal,'cancelled');
assert.deepEqual(exported.rows[0].pages,[original.rows[0].page]);
fields[0].find(x=>x.dataset.field==='checked').checked=false;
save.onclick();assert.equal(exported.reviewed,false);
fields[0].find(x=>x.dataset.field==='checked').checked=true;
fields[0].find(x=>x.dataset.field==='withdrawal').value='withdrawn';
save.onclick();assert.equal(exported.excluded.length,1);assert.equal(exported.excluded[0].reviewed,true);
fields[0].find(x=>x.dataset.field==='quantity').value='abc';
assert.throws(()=>save.onclick(), /invalid input/);
console.log('Review JSON: checked rows, cancelled/confirmed withdrawal, quantities and pages passed.');
