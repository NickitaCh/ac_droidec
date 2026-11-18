(() => {
    const normalize = value => value.toLocaleLowerCase('ru').replaceAll('ё', 'е').trim();
    document.querySelectorAll('[data-preset-picker]').forEach(picker => {
        const selection = picker.querySelector('[data-preset-selection]');
        const rows = [...picker.querySelectorAll('[data-preset-row]')];
        const search = picker.querySelector('[data-preset-search]');
        const slots = picker.querySelector('[data-preset-slots]');
        const checkedOnly = picker.querySelector('[data-preset-checked]');
        const submit = picker.closest('form').querySelector('[data-preset-submit]');
        const select = picker.querySelector('[data-preset-select]');
        const clear = picker.querySelector('[data-preset-clear]');
        const count = picker.querySelector('[data-preset-count]');
        const empty = picker.querySelector('[data-preset-empty]');
        const update = () => {
            const partial = picker.querySelector('input[name=scope]:checked').value === 'selected';
            const query = normalize(search?.value ?? '');
            const filter = slots?.value;
            let checked = 0;
            let visible = 0;
            let uncheckedVisible = 0;
            rows.forEach(row => {
                const input = row.querySelector('input');
                const used = Number(row.dataset.slots);
                const match = used === 0 ? 'empty' : used === 6 ? 'full' : 'partial';
                row.hidden = !normalize(row.dataset.name).includes(query) ||
                    Boolean(filter && filter !== match) || Boolean(checkedOnly?.checked && !input.checked);
                if (input.checked) checked++;
                if (!row.hidden) {
                    visible++;
                    if (!input.checked) uncheckedVisible++;
                }
            });
            selection.hidden = !partial;
            if (submit) submit.disabled = partial && !checked;
            if (count) {
                count.textContent = `Выбрано ${checked} из ${rows.length}`;
                empty.hidden = visible > 0;
                select.disabled = !uncheckedVisible;
                clear.disabled = !checked;
            }
        };
        picker.querySelectorAll('[data-preset-tools], [data-preset-batch]').forEach(el => { el.hidden = false; });
        picker.addEventListener('input', update);
        picker.addEventListener('change', update);
        select?.addEventListener('click', () => {
            rows.filter(row => !row.hidden).forEach(row => { row.querySelector('input').checked = true; });
            update();
        });
        clear?.addEventListener('click', () => {
            rows.forEach(row => { row.querySelector('input').checked = false; });
            update();
        });
        update();
    });
    const picker = document.getElementById('mo-character-select');
    const cards = [...document.querySelectorAll('[data-mo-character]')];
    if (picker && cards.length) {
        const prev = document.querySelector('[data-mo-prev]');
        const next = document.querySelector('[data-mo-next]');
        const search = document.getElementById('mo-character-search');
        const options = [...picker.options];
        let matches = options;
        let selected = 0;
        const key = 'mo-character:' + location.pathname + location.search;
        const show = value => {
            picker.value = value;
            const position = picker.selectedIndex;
            prev.disabled = position <= 0;
            next.disabled = position < 0 || position === matches.length - 1;
            picker.disabled = !matches.length;
            if (!matches.length) return;
            selected = Number(value);
            cards.forEach((card, i) => { card.hidden = i !== selected; });
            try { sessionStorage.setItem(key, value); } catch (_) {}
        };
        let initial = 0;
        try { initial = Number(sessionStorage.getItem(key)) || 0; } catch (_) {}
        show(options[initial]?.value ?? options[0].value);
        document.querySelector('[data-mo-picker]').hidden = false;
        picker.addEventListener('change', () => show(picker.value));
        prev.addEventListener('click', () => show(matches[picker.selectedIndex - 1].value));
        next.addEventListener('click', () => show(matches[picker.selectedIndex + 1].value));
        search.addEventListener('input', () => {
            const query = normalize(search.value);
            matches = options.filter(option => normalize(option.dataset.name).includes(query));
            picker.replaceChildren(...(matches.length ? matches : [new Option('Персонажи не найдены', '')]));
            show(matches.includes(options[selected]) ? options[selected].value : matches[0]?.value ?? '');
        });
    }
    const markChanged = () => {
        const apply = document.getElementById('mo-apply');
        if (apply) apply.disabled = true;
        const notice = document.getElementById('mo-changed');
        if (notice) notice.hidden = false;
        const goalsNotice = document.querySelector('[data-mo-goals-changed]');
        if (goalsNotice) goalsNotice.hidden = false;
    };
    document.querySelectorAll('[data-mo-stat]').forEach(control => {
        const range = control.querySelector('[data-mo-range]');
        const number = control.querySelector('input[type=number]');
        const initial = Number(control.dataset.default);
        if (!Number.isFinite(initial)) return;
        const samples = [initial, Number(control.dataset.before), Number(control.dataset.current)].filter(Number.isFinite);
        const step = control.dataset.stat === 'Speed' ? 1 : Math.max(...samples.map(Math.abs)) >= 1000 ? 100 : 0.1;
        range.step = String(step);
        const sync = () => {
            const value = number.value === '' ? initial : Number(number.value);
            if (!Number.isFinite(value)) return;
            const values = [...samples, value];
            range.min = String(Math.floor(Math.min(0, ...values) * 1.5 / step) * step);
            range.max = String(Math.ceil(Math.max(step, ...values.map(Math.abs)) * 1.5 / step) * step);
            range.value = String(value);
            control.dataset.changed = String(number.value !== '' && value !== initial);
        };
        sync();
        range.addEventListener('input', () => {
            number.value = range.value;
            control.dataset.changed = String(Number(number.value) !== initial);
            number.dispatchEvent(new Event('input', {bubbles: true}));
        });
        range.addEventListener('change', () => number.dispatchEvent(new Event('change', {bubbles: true})));
        number.addEventListener('input', markChanged);
        number.addEventListener('change', sync);
        control.querySelector('[data-mo-reset]').addEventListener('click', () => {
            number.value = '';
            sync();
            number.dispatchEvent(new Event('input', {bubbles: true}));
            number.dispatchEvent(new Event('change', {bubbles: true}));
        });
    });
})();
