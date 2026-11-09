(function () {
    'use strict';
    var dialog = document.querySelector('[data-share-dialog]');
    if (!dialog) return;
    var boxes = Array.from(dialog.querySelectorAll('[name=resources]'));
    var all = dialog.querySelector('[data-share-all]');
    var groups = Array.from(dialog.querySelectorAll('[data-share-category]'));
    function reflect(control, items) {
        var selected = items.filter(function (x) { return x.checked; }).length;
        control.checked = items.length > 0 && selected === items.length;
        control.indeterminate = selected > 0 && selected < items.length;
        control.disabled = !items.length;
    }
    function update() {
        reflect(all, boxes);
        groups.forEach(function (g) { reflect(g.querySelector('[data-share-group]'), Array.from(g.querySelectorAll('[name=resources]'))); });
        var count = boxes.filter(function (x) { return x.checked; }).length;
        dialog.querySelector('[data-share-count]').textContent = 'Выбрано: ' + count;
        dialog.querySelector('button:not([type])').disabled = !count;
    }
    all.addEventListener('change', function () { boxes.forEach(function (x) { x.checked = all.checked; }); update(); });
    groups.forEach(function (g) {
        var control = g.querySelector('[data-share-group]');
        control.addEventListener('change', function () { g.querySelectorAll('[name=resources]').forEach(function (x) { x.checked = control.checked; }); update(); });
    });
    boxes.forEach(function (x) { x.addEventListener('change', update); });
    document.querySelector('[data-share-open]').addEventListener('click', function () { dialog.showModal(); });
    dialog.querySelector('[data-share-close]').addEventListener('click', function () { dialog.close(); });
    update();
})();
