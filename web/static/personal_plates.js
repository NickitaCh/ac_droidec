(() => {
    const form = document.querySelector('[data-character-conditions]');
    if (!form) return;
    const rows = form.querySelector('[data-condition-rows]');
    const add = form.querySelector('[data-add-condition]');
    const template = document.getElementById('pp-condition-template');
    function update() {
        add.disabled = rows.children.length >= 30;
        rows.querySelectorAll('[data-remove-condition]').forEach(button => {
            button.disabled = rows.children.length === 1;
        });
    }
    add.addEventListener('click', () => {
        if (rows.children.length >= 30) return;
        rows.append(template.content.cloneNode(true));
        update();
        rows.lastElementChild.querySelector('select').focus();
    });
    rows.addEventListener('click', event => {
        const button = event.target.closest('[data-remove-condition]');
        if (!button || rows.children.length <= 1) return;
        button.closest('.pp-batch-row').remove();
        update();
    });
    update();
})();
