'use strict';

const projects = document.querySelector('#mo-projects');
if (projects) {
    const find = selector => projects.querySelector(selector);
    const form = projects.closest('form');
    const data = JSON.parse(find('[data-project-data]').textContent);
    const {options} = data;
    const names = new Map(data.characters);
    const locks = new Map([...form.querySelectorAll('input[name=locked]')].map(input => [input.value, input]));
    const mode = find('[data-project-mode]');
    const editor = find('[data-project-editor]');
    const plateSettings = form.querySelector('[data-project-plate-settings]');
    const search = find('[data-project-search]');
    const picker = find('[data-project-picker]');
    const cards = find('[data-project-cards]');
    const dialog = find('[data-project-dialog]');
    const rules = find('[data-project-rules]');
    const title = find('[data-project-name]');
    const open = find('[data-project-open]');
    const status = find('[data-project-status]');
    const id = form.elements.project_id;
    const deleteProject = find('[data-project-delete]');
    deleteProject.hidden = !id.value;
    const payload = form.elements.project_payload;
    let entries = data.draft?.characters || [];
    let editing;
    let dirty = false;
    title.value = data.draft?.name || '';

    function node(tag, label) {
        const element = document.createElement(tag);
        if (label !== undefined) element.textContent = label;
        return element;
    }

    function button(label, action) {
        const element = node('button', label);
        element.type = 'button';
        element.className = 'button-secondary';
        element.addEventListener('click', action);
        return element;
    }

    function checkbox(label, checked, change) {
        const wrapper = node('label');
        wrapper.className = 'checkbox';
        const box = node('input');
        box.type = 'checkbox';
        box.checked = checked;
        box.addEventListener('change', () => change(box.checked));
        wrapper.append(box, node('span', label));
        return wrapper;
    }

    function select(label, values, selected, change, multiple = false) {
        const wrapper = node('label', label);
        const element = node('select');
        element.multiple = multiple;
        if (multiple) element.size = Math.min(values.length, 5);
        for (const [value, name] of values) {
            element.add(new Option(name, value, false, multiple ? selected.includes(value) : selected === value));
        }
        element.addEventListener('change', () => {
            changed();
            change(multiple ? [...element.selectedOptions].map(option => Number(option.value)) : element.value);
        });
        wrapper.append(element);
        return wrapper;
    }

    function changed() {
        dirty = true;
        requireRecalculation();
    }

    function portrait(base) {
        const frame = node('span');
        frame.className = 'unit-portrait unit-portrait-xs';
        const image = node('img');
        image.src = '/unit-img/' + encodeURIComponent(base);
        image.alt = '';
        image.loading = 'lazy';
        frame.append(image);
        return frame;
    }

    function showPicker() {
        const query = search.value.trim().toLocaleLowerCase();
        picker.replaceChildren();
        if (!query && document.activeElement !== search) return;
        const added = new Set(entries.map(entry => entry.base));
        const matches = data.characters.filter(([base, name]) => !added.has(base)
            && (name.toLocaleLowerCase().includes(query) || base.toLocaleLowerCase().includes(query)));
        for (const [base, name] of matches.slice(0, 12)) {
            const option = button('', () => {
                entries.push({base, enabled: true, rules: []});
                changed();
                search.value = '';
                showPicker();
                showCards();
                search.focus();
            });
            option.append(portrait(base), node('span', name));
            picker.append(option);
        }
        if (!matches.length) picker.append(node('span', 'Персонажи не найдены.'));
    }

    function move(index, offset) {
        const target = index + offset;
        [entries[index], entries[target]] = [entries[target], entries[index]];
        changed();
        showCards();
        cards.children[target].querySelector(offset < 0 ? '[data-down]' : '[data-up]').focus();
    }

    function showCards() {
        cards.replaceChildren();
        for (const [index, entry] of entries.entries()) {
            const name = names.get(entry.base) || entry.base;
            const card = node('article');
            const heading = node('h3');
            heading.append(portrait(entry.base), node('span', name));
            card.append(heading, checkbox('В подборе', entry.enabled, checked => {
                entry.enabled = checked;
                changed();
            }));
            const lock = locks.get(entry.base);
            if (lock) card.append(checkbox('🔒 Сохранить модули', lock.checked, checked => {
                lock.checked = checked;
                lock.dispatchEvent(new Event('change'));
            }));
            const actions = node('div');
            actions.className = 'mo-project-actions';
            const edit = button('✎ Требования (' + entry.rules.length + ')', () => {
                editing = entry;
                find('[data-project-unit]').textContent = name;
                showRules();
                dialog.showModal();
            });
            const up = button('↑', () => move(index, -1));
            const down = button('↓', () => move(index, 1));
            up.dataset.up = '';
            down.dataset.down = '';
            up.setAttribute('aria-label', 'Повысить приоритет: ' + name);
            down.setAttribute('aria-label', 'Понизить приоритет: ' + name);
            up.disabled = index === 0;
            down.disabled = index === entries.length - 1;
            actions.append(edit, up, down, button('Убрать', () => {
                entries.splice(index, 1);
                changed();
                showCards();
                showPicker();
                search.focus();
            }));
            card.append(actions);
            cards.append(card);
        }
    }

    function showRules() {
        rules.replaceChildren();
        for (const [index, rule] of editing.rules.entries()) {
            const row = node('div');
            row.className = 'mo-project-rule';
            if (rule.kind === 'num' || rule.kind === 'cmp') {
                row.append(select('Характеристика', options.stats, rule.stat, value => { rule.stat = value; }));
                row.append(select('Условие', [['>=', 'Не меньше'], ['<=', 'Не больше'], ['=', 'Равно']],
                    rule.op, value => { rule.op = value; }));
                if (rule.kind === 'cmp') {
                    const peers = [...names].filter(([base]) => base !== editing.base && entries.some(entry => entry.base === base));
                    row.append(select('Сравнить с', [['', 'Выберите персонажа'], ...peers], rule.compare,
                        value => { rule.compare = value; }));
                }
                const label = node('label', rule.kind === 'cmp' ? 'Разница' : 'Значение');
                const value = node('input');
                value.inputMode = 'decimal';
                value.value = rule.value;
                value.addEventListener('input', () => { rule.value = value.value; changed(); });
                label.append(value);
                row.append(label);
            } else if (rule.kind === 'set') {
                row.append(select('Сет', options.sets, rule.set_id, value => { rule.set_id = Number(value); }));
            } else if (rule.kind === 'any' || rule.kind === 'only') {
                row.append(select('Сеты', options.sets, rule.ids, values => { rule.ids = values; }, true));
            } else {
                row.append(select('Слот', Object.entries(options.slots).map(([slot, item]) => [slot, item.label]),
                    rule.slot, value => {
                        rule.slot = value;
                        rule.ids = [options.slots[value].options[0][0]];
                        showRules();
                    }));
                row.append(select('Допустимые основы', options.slots[rule.slot].options, rule.ids,
                    values => { rule.ids = values; }, true));
            }
            row.append(select('Приоритет', [['required', 'Обязательно'], ['optional', 'Желательно']],
                rule.priority, value => { rule.priority = value; }));
            row.append(button('Удалить', () => {
                editing.rules.splice(index, 1);
                changed();
                showRules();
            }));
            rules.append(row);
        }
    }

    function updateMode() {
        const projectMode = mode.value === 'project';
        editor.hidden = !projectMode;
        plateSettings.hidden = projectMode;
        for (const input of plateSettings.querySelectorAll('input, select')) input.disabled = projectMode;
    }

    function projectData() {
        return JSON.stringify({name: title.value, characters: entries});
    }

    function navigate(key, value) {
        if (!value) return;
        if (dirty && !window.confirm('Открыть без сохранения текущих изменений?')) {
            open.value = id.value;
            return;
        }
        const params = new URLSearchParams({ally_code: form.elements.ally_code.value, [key]: value});
        location.assign('/mod-optimizer?' + params);
    }

    mode.addEventListener('change', () => {
        requireRecalculation();
        updateMode();
    });
    title.addEventListener('input', changed);
    search.addEventListener('input', showPicker);
    search.addEventListener('focus', showPicker);
    search.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            picker.querySelector('button')?.click();
        }
    });
    open.addEventListener('change', () => navigate('project_id', open.value));
    const seed = find('[data-project-seed]');
    seed?.addEventListener('change', () => navigate('seed_plate', seed.value));
    form.querySelector('.mo-lock-list')?.addEventListener('change', event => {
        if (event.isTrusted && event.target.name === 'locked') showCards();
    });
    find('[data-project-new]').addEventListener('click', () => {
        if (dirty && !window.confirm('Создать новый проект без сохранения текущих изменений?')) return;
        requireRecalculation();
        entries = [];
        title.value = '';
        id.value = '';
        deleteProject.hidden = true;
        open.value = '';
        if (seed) seed.value = '';
        dirty = false;
        status.textContent = '';
        showCards();
        showPicker();
        title.focus();
    });
    find('[data-project-add-rule]').addEventListener('click', () => {
        if (editing.rules.length >= 30) return;
        const kind = find('[data-project-kind]').value;
        const rule = {kind, priority: 'required'};
        if (kind === 'set') rule.set_id = options.sets[0][0];
        else if (kind === 'any' || kind === 'only') rule.ids = [options.sets[0][0]];
        else if (kind === 'pri') {
            rule.slot = 'arrow';
            rule.ids = [options.slots.arrow.options[0][0]];
        } else {
            Object.assign(rule, {stat: 'Speed', op: '>=', value: ''});
            if (kind === 'cmp') Object.assign(rule, {compare: '', value: 1});
        }
        editing.rules.push(rule);
        changed();
        showRules();
    });
    find('[data-project-done]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', showCards);
    form.addEventListener('submit', event => {
        if (dialog.open) {
            event.preventDefault();
            return;
        }
        if (mode.value !== 'project') {
            payload.value = '';
            return;
        }
        const enabled = new Set(entries.filter(entry => entry.enabled).map(entry => entry.base));
        const invalid = entries.some(entry => entry.rules.some(rule => {
            if (rule.kind === 'cmp' && (!rule.compare || (entry.enabled && !enabled.has(rule.compare)))) return true;
            if (rule.ids && !rule.ids.length) return true;
            if (rule.kind !== 'num' && rule.kind !== 'cmp') return false;
            const raw = String(rule.value).trim().replace(',', '.');
            return !raw || !Number.isFinite(Number(raw)) || Math.abs(Number(raw)) > 1e9
                || (rule.kind === 'cmp' && Number(raw) < 0);
        }));
        if (!enabled.size || invalid) {
            event.preventDefault();
            status.textContent = enabled.size ? 'Проверьте значения требований и включите обоих персонажей из каждого сравнения.'
                : 'Включите хотя бы одного персонажа в подбор.';
            status.scrollIntoView({block: 'nearest'});
            return;
        }
        payload.value = projectData();
    });
    deleteProject.addEventListener('click', async () => {
        if (!window.confirm('Удалить сохранённый проект?')) return;
        const fields = new FormData();
        for (const field of ['csrf', 'ally_code', 'project_id']) fields.set(field, form.elements[field].value);
        deleteProject.disabled = true;
        try {
            const response = await fetch('/mod-optimizer/projects/delete', {method: 'POST', body: fields});
            if (!response.ok) throw new Error('Не удалось удалить проект.');
            const params = new URLSearchParams({ally_code: form.elements.ally_code.value});
            location.assign('/mod-optimizer?' + params);
        } catch (error) {
            status.textContent = error.message;
            deleteProject.disabled = false;
        }
    });
    find('[data-project-save]').addEventListener('click', async event => {
        const trigger = event.currentTarget;
        const snapshot = projectData();
        const fields = new FormData();
        for (const field of ['csrf', 'ally_code', 'project_id']) fields.set(field, form.elements[field].value);
        fields.set('project_payload', snapshot);
        for (const lock of locks.values()) if (lock.checked) fields.append('locked', lock.value);
        trigger.disabled = true;
        status.textContent = 'Сохраняю…';
        try {
            const response = await fetch('/mod-optimizer/projects/save', {method: 'POST', body: fields});
            const result = await response.json();
            if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить проект.');
            id.value = result.id;
            deleteProject.hidden = false;
            const option = [...open.options].find(item => item.value === result.id);
            if (option) option.textContent = result.name;
            else open.add(new Option(result.name, result.id));
            open.value = result.id;
            dirty = snapshot !== projectData();
            status.textContent = 'Проект сохранён.';
        } catch (error) {
            status.textContent = error.message || 'Не удалось сохранить проект.';
        } finally {
            trigger.disabled = false;
        }
    });

    projects.hidden = false;
    updateMode();
    showCards();
}
