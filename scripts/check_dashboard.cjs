// Run: node scripts/check_dashboard.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/static/dashboard.js', 'utf8');
const start = source.indexOf('const initTableSort =');
const end = source.indexOf('initTableSort(document);', start);
function checkSort(type, values, expected, headerIndex = 0) {
    let click;
    const header = {dataset: {sort: type}, cellIndex: headerIndex,
        addEventListener: (_, fn) => click = fn, removeAttribute() {},
        querySelector() {}, appendChild() {}};
    const rows = values.map(value => ({cells: Array.from({length: headerIndex + 1}, (_, i) =>
        ({dataset: {value: i === headerIndex ? value : 'name'}, textContent: 'name'}))}));
    const tbody = {rows, appendChild(row) { rows.splice(rows.indexOf(row), 1); rows.push(row); }};
    const table = {querySelectorAll: () => [header], tBodies: [tbody]};
    vm.runInNewContext(source.slice(start, end) + '\ninitTableSort(root);', {
        root: {querySelectorAll: () => [table]}, document: {createElement: () => ({})},
    });
    click();
    assert.deepEqual(rows.map(r => r.cells[headerIndex].dataset.value), expected);
    click();
    assert.deepEqual(rows.map(r => r.cells[headerIndex].dataset.value), [...expected].reverse());
}
checkSort('number', ['10', '2', '3', '4'], ['2', '3', '4', '10']);
checkSort('num', ['32 447', '5,1', '10'], ['5,1', '10', '32 447']);
checkSort('num', ['10', '2'], ['2', '10'], 1);
console.log('Dashboard numeric sorting: PASS');
const searchStart = source.indexOf('const initSearchWidget =');
const searchEnd = source.indexOf('// Легаси:', searchStart);
function checkPicker(required, text, selected, shouldBlock) {
    const listeners = {}, formListeners = {};
    const input = {value: text, addEventListener: (event, fn) => listeners[event] = fn,
        setCustomValidity(value) {this.validityMessage = value;}, reportValidity() {}};
    const hidden = {value: selected};
    const results = {classList: {remove() {}}};
    const form = {addEventListener: (event, fn) => formListeners[event] = fn};
    const wrap = {querySelector: selector => selector.endsWith('-input') ? input :
        selector.endsWith('-value') ? hidden : results, closest: () => form};
    vm.runInNewContext(source.slice(searchStart, searchEnd) + '\ninitSearchWidget(wrap, {required});', {
        wrap, required, clearTimeout() {}, setTimeout() {},
    });
    const event = {blocked:false, preventDefault() {this.blocked = true;}};
    formListeners.submit(event);
    assert.equal(event.blocked, shouldBlock);
    input.value = '';
    listeners.input();
    assert.equal(input.validityMessage, '');
}
checkPicker(false, '', '', false);
checkPicker(false, 'Рей', 'REY', false);
checkPicker(false, 'не выбран из подсказок', '', true);
checkPicker(true, '', '', true);
assert.match(fs.readFileSync('web/templates/mod_compare.html', 'utf8'), /data-unit-search data-required="false"/);
console.log('Dashboard optional character pickers: PASS');
