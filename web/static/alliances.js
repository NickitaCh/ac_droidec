(function () {
    'use strict';
    function normalize(value) { return value.toLocaleLowerCase('ru').replace(/ё/g, 'е').trim(); }
    document.querySelectorAll('[data-all-members]').forEach(function (control) {
        var recipients = control.closest('form').querySelector('[data-recipients]');
        function updateRecipients() {
            recipients.disabled = control.checked;
            recipients.hidden = control.checked;
        }
        control.addEventListener('change', updateRecipients);
        updateRecipients();
    });
    var search = document.querySelector('[data-sharing-search]');
    if (search) {
        var kind = document.querySelector('[data-sharing-kind]');
        var rows = Array.from(document.querySelectorAll('[data-sharing-row]'));
        function filterShares() {
            var query = normalize(search.value);
            rows.forEach(function (row) {
                row.hidden = (kind.value && row.dataset.kind !== kind.value) ||
                    !normalize(row.querySelector('.alliance-share-overview').textContent).includes(query);
            });
            document.querySelector('[data-sharing-empty]').hidden = rows.some(function (row) { return !row.hidden; });
        }
        search.addEventListener('input', filterShares);
        kind.addEventListener('change', filterShares);
    }
    var dialog = document.querySelector('[data-share-dialog]');
    if (!dialog) return;
    var boxes = Array.from(dialog.querySelectorAll('[name=resources]'));
    var all = dialog.querySelector('[data-share-all]');
    var groups = Array.from(dialog.querySelectorAll('[data-share-category]'));
    var resourceSearch = dialog.querySelector('[data-resource-search]');
    function visible(items) { return items.filter(function (x) { return !x.closest('[data-resource-row]').hidden; }); }
    function reflect(control, items) {
        var selected = items.filter(function (x) { return x.checked; }).length;
        control.checked = items.length > 0 && selected === items.length;
        control.indeterminate = selected > 0 && selected < items.length;
        control.disabled = !items.length;
    }
    function update() {
        reflect(all, visible(boxes));
        groups.forEach(function (g) { reflect(g.querySelector('[data-share-group]'), visible(Array.from(g.querySelectorAll('[name=resources]')))); });
        var count = boxes.filter(function (x) { return x.checked; }).length;
        dialog.querySelector('[data-share-count]').textContent = 'Выбрано: ' + count;
        dialog.querySelector('[data-share-submit]').disabled = !count;
    }
    resourceSearch.addEventListener('input', function () {
        var query = normalize(resourceSearch.value);
        boxes.forEach(function (box) {
            var row = box.closest('[data-resource-row]');
            row.hidden = !normalize(row.textContent).includes(query);
        });
        groups.forEach(function (g) { g.hidden = !visible(Array.from(g.querySelectorAll('[name=resources]'))).length; });
        dialog.querySelector('[data-resource-empty]').hidden = visible(boxes).length > 0;
        update();
    });
    all.addEventListener('change', function () { visible(boxes).forEach(function (x) { x.checked = all.checked; }); update(); });
    groups.forEach(function (g) {
        var control = g.querySelector('[data-share-group]');
        control.addEventListener('change', function () { visible(Array.from(g.querySelectorAll('[name=resources]'))).forEach(function (x) { x.checked = control.checked; }); update(); });
    });
    boxes.forEach(function (x) { x.addEventListener('change', update); });
    document.querySelector('[data-share-open]').addEventListener('click', function () { dialog.showModal(); });
    dialog.querySelector('[data-share-close]').addEventListener('click', function () { dialog.close(); });
    update();
})();
