// Деф на ВГ (/tw/def): окно кандидатов по паку, редактор слотов пака, фильтр карточек,
// копирование текста расстановки. Серверная часть — web/routes/tw_defense.py.
(function () {
    "use strict";

    const el = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    };

    const portrait = (baseId, size) => {
        const span = el("span", "unit-portrait unit-portrait-" + (size || "xs"));
        const img = document.createElement("img");
        img.alt = "";
        img.loading = "lazy";
        img.src = baseId ? `/unit-img/${encodeURIComponent(baseId)}` : "/static/img/game/portrait-fallback.svg";
        span.appendChild(img);
        return span;
    };

    // ---------------------------------------------------------------- фильтр карточек
    document.querySelectorAll("[data-twdef-filter]").forEach((input) => {
        const cards = document.querySelectorAll(input.dataset.twdefFilter);
        input.addEventListener("input", () => {
            const q = input.value.trim().toLowerCase();
            cards.forEach((card) => {
                card.hidden = q && !(card.dataset.search || "").includes(q);
            });
        });
    });

    // ---------------------------------------------------------------- данные для статов
    // Плашка _tw_def_calc_wait.html: опрашиваем статус фоновой загрузки калькулятора
    // статов и обновляем страницу, как только он готов.
    const calcWait = document.querySelector("[data-twdef-calc-wait]");
    if (calcWait) {
        const title = calcWait.querySelector("[data-calc-title]");
        const sub = calcWait.querySelector("[data-calc-sub]");
        const retry = calcWait.querySelector("[data-calc-retry]");
        const spinner = calcWait.querySelector(".twdef-spinner");
        let timer;
        const poll = async (again) => {
            try {
                const resp = await fetch(`/tw/def/api/calc-status${again ? "?retry=1" : ""}`, { headers: { Accept: "application/json" } });
                const st = await resp.json();
                if (st.ready) {
                    title.textContent = "Данные загружены — обновляю страницу…";
                    location.reload();
                    return;
                }
                if (st.error && !st.loading) {
                    calcWait.classList.add("twdef-calc-wait-error");
                    spinner.hidden = true;
                    title.textContent = "Не удалось загрузить данные игры";
                    sub.textContent = "Скорее всего, недоступен сервис игровых данных. Требования к статам пока не проверяются, остальное работает.";
                    retry.hidden = false;
                    return;
                }
            } catch { /* сеть моргнула — просто опрашиваем дальше */ }
            timer = setTimeout(() => poll(false), 3000);
        };
        retry.addEventListener("click", () => {
            calcWait.classList.remove("twdef-calc-wait-error");
            spinner.hidden = false;
            retry.hidden = true;
            title.textContent = "Загружаются данные игры для проверки статов…";
            clearTimeout(timer);
            poll(true);
        });
        timer = setTimeout(() => poll(false), 2000);
    }

    // ---------------------------------------------------------------- копирование
    document.querySelectorAll("[data-twdef-copy]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const area = document.getElementById(btn.dataset.twdefCopy);
            if (!area) return;
            try {
                await navigator.clipboard.writeText(area.value);
            } catch {
                area.select();
                document.execCommand("copy");
            }
            const old = btn.textContent;
            btn.textContent = "Скопировано";
            setTimeout(() => { btn.textContent = old; }, 1500);
        });
    });

    // ---------------------------------------------------------------- убрать / перенести назначение
    // Кнопки в строках карты без своих форм (на плане бывает до ~400 назначений) —
    // одна общая скрытая форма на действие.
    const delForm = document.getElementById("twdef-del-form");
    const moveForm = document.getElementById("twdef-move-form");
    const zonesNode = document.getElementById("twdef-zones");
    const zones = zonesNode ? JSON.parse(zonesNode.textContent) : [];
    document.addEventListener("click", (e) => {
        const del = e.target.closest("[data-twdef-del]");
        if (del && delForm) {
            delForm.elements.assignment_id.value = del.dataset.twdefDel;
            delForm.elements.back.value = del.dataset.back || "";
            del.disabled = true;
            delForm.submit();
            return;
        }
        const move = e.target.closest("[data-twdef-move]");
        if (move && moveForm) {
            const select = document.createElement("select");
            select.className = "twdef-move-select";
            select.setAttribute("aria-label", "Перенести в зону");
            zones.filter((z) => z.kind === move.dataset.kind).forEach((z) => {
                const opt = new Option(z.label, z.key, false, z.key === move.dataset.zone);
                select.add(opt);
            });
            move.replaceWith(select);
            select.focus();
            select.addEventListener("change", () => {
                if (select.value === move.dataset.zone) return;
                moveForm.elements.assignment_id.value = move.dataset.twdefMove;
                moveForm.elements.zone.value = select.value;
                select.disabled = true;
                moveForm.submit();
            });
            select.addEventListener("blur", () => { if (!select.disabled) select.replaceWith(move); });
        }
    });

    // ---------------------------------------------------------------- окно кандидатов
    const dialog = document.getElementById("twdef-picker");
    if (dialog) {
        const form = document.getElementById("twdef-picker-form");
        const list = document.getElementById("twdef-picker-list");
        const title = document.getElementById("twdef-picker-title");
        const sub = document.getElementById("twdef-picker-sub");
        const search = document.getElementById("twdef-picker-search");
        const filter = document.getElementById("twdef-picker-filter");
        const counts = document.getElementById("twdef-picker-counts");
        const loading = document.getElementById("twdef-picker-loading");
        const errorBox = document.getElementById("twdef-picker-error");
        const warningBox = document.getElementById("twdef-picker-warning");
        const footer = document.getElementById("twdef-picker-footer");
        const selectedLabel = document.getElementById("twdef-picker-selected");
        let candidates = [];
        let selectable = false;
        let free = 0;
        const checked = new Set();

        const STATUS_LABEL = { ok: "может", used: "занят в плане", no: "не проходит", excluded: "не участвует" };

        const updateSelected = () => {
            if (!selectable) return;
            const n = checked.size;
            selectedLabel.textContent = `Выбрано: ${n} из ${free} свободных мест`;
            selectedLabel.classList.toggle("error", n > free);
            form.querySelector("button[type=submit]").disabled = n === 0 || n > free;
        };

        const render = () => {
            const q = search.value.trim().toLowerCase();
            const mode = filter.value;
            list.innerHTML = "";
            let shown = 0;
            candidates.forEach((c) => {
                if (mode !== "all" && c.status !== mode) return;
                if (q && !c.name.toLowerCase().includes(q)) return;
                shown += 1;
                const li = el("li", `twdef-cand twdef-cand-${c.status}`);
                if (selectable && c.status === "ok") {
                    const box = document.createElement("input");
                    box.type = "checkbox";
                    box.checked = checked.has(c.ally_code);
                    box.addEventListener("change", () => {
                        if (box.checked) checked.add(c.ally_code); else checked.delete(c.ally_code);
                        updateSelected();
                    });
                    const label = el("label", "twdef-cand-pick");
                    label.appendChild(box);
                    label.appendChild(el("span", "twdef-cand-name", c.name));
                    li.appendChild(label);
                } else {
                    li.appendChild(el("span", "twdef-cand-name", c.name));
                }
                if (c.units && c.units.length) {
                    const strip = el("span", "twdef-units");
                    c.units.forEach((u) => {
                        const unit = el("span", "twdef-unit");
                        unit.title = u.name + (u.badge ? ` · ${u.badge}` : "");
                        unit.appendChild(portrait(u.base_id, "xs"));
                        if (u.badge) unit.appendChild(el("span", "twdef-unit-badge", u.badge));
                        strip.appendChild(unit);
                    });
                    li.appendChild(strip);
                }
                const meta = el("span", "twdef-cand-meta");
                if (c.status !== "ok") meta.appendChild(el("span", `badge ${c.status === "used" ? "badge-warn" : "badge-neutral"}`, STATUS_LABEL[c.status] || c.status));
                if (c.assigned) meta.appendChild(el("span", "muted", `в плане: ${c.assigned}`));
                if (c.status === "ok" && c.conflict) {
                    const conflict = el("span", "muted", `мешает ${c.conflict} др. пакам`);
                    conflict.title = "Сколько других паков библиотеки игрок сможет поставить, только если не брать его сюда";
                    meta.appendChild(conflict);
                }
                if (c.dc === "ok") meta.appendChild(el("span", "badge badge-ok", "ДК есть"));
                else if (c.dc === "no") meta.appendChild(el("span", "badge badge-warn", "нет ДК"));
                else if (c.dc === "used") meta.appendChild(el("span", "badge badge-warn", "ДК занят в другом паке"));
                else if (c.dc === "unknown") meta.appendChild(el("span", "badge badge-neutral", "ДК: нет данных"));
                if (c.suggested && selectable) meta.appendChild(el("span", "badge badge-ok", "предложен"));
                li.appendChild(meta);
                if (c.reason) li.appendChild(el("span", "twdef-cand-reason", c.reason));
                list.appendChild(li);
            });
            if (!shown) list.appendChild(el("li", "muted", "Никого."));
            const by = (s) => candidates.filter((c) => c.status === s).length;
            counts.textContent = `могут: ${by("ok")} · заняты: ${by("used")} · не проходят: ${by("no")}`;
        };

        const open = async ({ url, action, zone, squadId, heading }) => {
            selectable = Boolean(action);
            candidates = [];
            checked.clear();
            list.innerHTML = "";
            title.textContent = heading || "Кандидаты";
            sub.textContent = "";
            errorBox.hidden = true;
            warningBox.hidden = true;
            loading.hidden = false;
            footer.hidden = !selectable;
            search.value = "";
            filter.value = "ok";
            form.action = action || "";
            form.elements.zone.value = zone || "";
            form.elements.squad_id.value = squadId || "";
            dialog.showModal();
            try {
                const resp = await fetch(url, { headers: { Accept: "application/json" } });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || `Ошибка ${resp.status}`);
                candidates = data.candidates || [];
                free = data.free ?? 0;
                title.textContent = data.zone ? `${data.squad} → ${data.zone}` : `Кто может поставить: ${data.squad}`;
                sub.textContent = data.zone ? `Свободно мест в зоне: ${free}. Отмечены предложенные автоподбором — их можно поменять.` : "";
                if (data.warning) { warningBox.textContent = data.warning; warningBox.hidden = false; }
                candidates.forEach((c) => { if (c.suggested) checked.add(c.ally_code); });
                render();
                updateSelected();
            } catch (e) {
                errorBox.textContent = e.message || "Не удалось загрузить кандидатов";
                errorBox.hidden = false;
            } finally {
                loading.hidden = true;
            }
        };

        form.addEventListener("submit", (e) => {
            if (!selectable) { e.preventDefault(); return; }
            form.querySelectorAll("input[name=ally_codes]").forEach((n) => n.remove());
            checked.forEach((code) => {
                const hidden = document.createElement("input");
                hidden.type = "hidden";
                hidden.name = "ally_codes";
                hidden.value = code;
                form.appendChild(hidden);
            });
        });
        dialog.querySelector("[data-twdef-close]").addEventListener("click", () => dialog.close());
        document.getElementById("twdef-picker-none").addEventListener("click", () => {
            checked.clear();
            render();
            updateSelected();
        });
        search.addEventListener("input", render);
        filter.addEventListener("change", render);

        document.querySelectorAll("[data-twdef-check]").forEach((btn) => {
            btn.addEventListener("click", () => open({ url: btn.dataset.twdefCheck, heading: btn.dataset.title }));
        });
        document.querySelectorAll("[data-twdef-pick]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const select = btn.form.querySelector("select[name=squad_id]");
                const squadId = select.value;
                const countInput = btn.form.querySelector("input[name=count]");
                const params = new URLSearchParams({ squad_id: squadId, zone: btn.dataset.zone, count: countInput ? countInput.value : "0" });
                open({
                    url: `${btn.dataset.url}?${params}`,
                    action: btn.dataset.action,
                    zone: btn.dataset.zone,
                    squadId,
                    heading: select.options[select.selectedIndex].dataset.name || select.options[select.selectedIndex].text,
                });
            });
        });
    }

    // ---------------------------------------------------------------- редактор пака
    const editor = document.getElementById("twdef-editor");
    const dataNode = document.getElementById("twdef-squad-data");
    if (editor && dataNode) {
        const squad = JSON.parse(dataNode.textContent);
        const slotsBox = document.getElementById("twdef-slots");
        const tpl = document.getElementById("twdef-option-tpl");
        const addSlotBtn = document.getElementById("twdef-add-slot");
        const MAX = { character: 5, ship: 8 };
        const FIELDS = ["min_relic", "min_gear", "min_stars"];
        const statTpl = document.getElementById("twdef-stat-tpl");
        const MAX_STATS = 6;

        // Требования к статам юнита: строки «стат ≥ минимум». Старый min_speed — как скорость.
        const addStatRow = (row, stat, min) => {
            const box = row.querySelector(".twdef-stat-rows");
            if (box.children.length >= MAX_STATS) return null;
            const line = statTpl.content.firstElementChild.cloneNode(true);
            if (stat) line.querySelector("[data-stat]").value = stat;
            if (min !== undefined && min !== null) line.querySelector("[data-stat-min]").value = min;
            line.querySelector(".twdef-remove-stat").addEventListener("click", () => {
                line.remove();
                row.querySelector(".twdef-add-stat").disabled = false;
            });
            box.appendChild(line);
            row.querySelector(".twdef-add-stat").disabled = box.children.length >= MAX_STATS;
            return line;
        };
        const FLAGS = ["zeta", "omicron", "ultimate"];

        const combatType = () => editor.querySelector("input[name=combat_type]:checked").value;

        // У юнита без омикрона в игре требование «омикрон» бессмысленно — сервер его
        // всё равно снимет, поэтому галочку сразу делаем недоступной.
        const applyOmicron = (row) => {
            const box = row.querySelector("[data-field=omicron]");
            const none = row.dataset.hasOmicron === "false";
            box.disabled = none;
            if (none) box.checked = false;
            box.closest("label").title = none ? "У этого юнита нет омикрона в игре" : "Омикрон для ВГ (если у юнита нет ВГ-омикрона — любой)";
        };

        const attachSearch = (row) => {
            const wrap = row.querySelector(".twdef-unit-search");
            const input = wrap.querySelector(".unit-search-input");
            const results = wrap.querySelector(".unit-search-results");
            const pic = wrap.querySelector(".twdef-option-portrait img");
            let timer;
            let items = [];
            let active = -1;
            const choose = (item) => {
                row.dataset.baseId = item.base_id;
                row.dataset.hasOmicron = String(Boolean(item.has_omicron));
                applyOmicron(row);
                input.value = item.name;
                pic.src = `/unit-img/${encodeURIComponent(item.base_id)}`;
                input.setCustomValidity("");
                results.classList.remove("open");
            };
            const draw = () => {
                results.innerHTML = "";
                if (!items.length) results.appendChild(el("div", "unit-search-empty", "Ничего не найдено"));
                items.forEach((item, i) => {
                    const r = el("div", "unit-search-result unit-search-result-unit" + (i === active ? " active" : ""));
                    r.appendChild(portrait(item.base_id, "xs"));
                    r.appendChild(document.createTextNode(item.name));
                    r.addEventListener("mousedown", (e) => { e.preventDefault(); choose(item); });
                    results.appendChild(r);
                });
                results.classList.add("open");
            };
            input.addEventListener("input", () => {
                row.dataset.baseId = "";
                pic.src = "/static/img/game/portrait-fallback.svg";
                clearTimeout(timer);
                const q = input.value.trim();
                if (q.length < 2) { results.classList.remove("open"); return; }
                timer = setTimeout(async () => {
                    try {
                        const resp = await fetch(`/tw/def/api/units?${new URLSearchParams({ q, type: combatType() })}`);
                        items = resp.ok ? await resp.json() : [];
                    } catch { items = []; }
                    active = -1;
                    draw();
                }, 200);
            });
            input.addEventListener("keydown", (e) => {
                if (!results.classList.contains("open") || !items.length) return;
                if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); draw(); }
                else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); draw(); }
                else if (e.key === "Enter") { e.preventDefault(); if (active >= 0) choose(items[active]); }
                else if (e.key === "Escape") results.classList.remove("open");
            });
            input.addEventListener("blur", () => setTimeout(() => results.classList.remove("open"), 150));
        };

        const makeOption = (opt) => {
            const row = tpl.content.firstElementChild.cloneNode(true);
            row.dataset.baseId = opt.base_id || "";
            if (opt.base_id) {
                row.querySelector(".unit-search-input").value = opt.name || opt.base_id;
                row.querySelector(".twdef-option-portrait img").src = `/unit-img/${encodeURIComponent(opt.base_id)}`;
            }
            FIELDS.forEach((f) => {
                const input = row.querySelector(`[data-field=${f}]`);
                if (opt[f] !== undefined && opt[f] !== null) input.value = opt[f];
            });
            FLAGS.forEach((f) => { row.querySelector(`[data-field=${f}]`).checked = Boolean(opt[f]); });
            if (opt.base_id && opt.has_omicron !== undefined) {
                row.dataset.hasOmicron = String(Boolean(opt.has_omicron));
                applyOmicron(row);
            }
            row.querySelector(".twdef-remove-option").addEventListener("click", () => {
                const slot = row.closest(".twdef-slot-edit");
                row.remove();
                if (!slot.querySelector(".twdef-option-row")) slot.querySelector(".twdef-options").appendChild(makeOption({}));
                renumber();
            });
            const stats = (opt.stats || []).slice();
            if (opt.min_speed && !stats.some((x) => x.stat === "Speed")) stats.unshift({ stat: "Speed", min: opt.min_speed });
            stats.forEach((x) => addStatRow(row, x.stat, x.min));
            row.querySelector(".twdef-add-stat").addEventListener("click", () => {
                const line = addStatRow(row, null, null);
                if (line) line.querySelector("[data-stat-min]").focus();
            });
            attachSearch(row);
            return row;
        };

        const renumber = () => {
            const slots = slotsBox.querySelectorAll(".twdef-slot-edit");
            slots.forEach((slot, i) => {
                slot.querySelector(".twdef-slot-title").textContent =
                    `Слот ${i + 1}` + (i === 0 && combatType() === "character" ? " — лидер" : i === 0 ? " — флагман" : "");
                slot.querySelectorAll(".twdef-option-row").forEach((row, j) => row.classList.toggle("twdef-option-alt", j > 0));
            });
            addSlotBtn.disabled = slots.length >= MAX[combatType()];
        };

        const makeSlot = (slot) => {
            const box = el("div", "twdef-slot-edit");
            const head = el("div", "twdef-slot-edit-head");
            head.appendChild(el("strong", "twdef-slot-title"));
            const addAlt = el("button", "button-secondary twdef-small", "+ замена");
            addAlt.type = "button";
            addAlt.title = "Другой юнит, который можно поставить в этот слот, если основного нет";
            const remove = el("button", "button-danger twdef-small", "Убрать слот");
            remove.type = "button";
            head.appendChild(addAlt);
            head.appendChild(remove);
            box.appendChild(head);
            const options = el("div", "twdef-options");
            box.appendChild(options);
            const opts = (slot && slot.options && slot.options.length) ? slot.options : [{}];
            opts.forEach((o) => options.appendChild(makeOption(o)));
            addAlt.addEventListener("click", () => {
                const row = makeOption({});
                options.appendChild(row);
                renumber();
                row.querySelector(".unit-search-input").focus();
            });
            remove.addEventListener("click", () => {
                box.remove();
                if (!slotsBox.querySelector(".twdef-slot-edit")) slotsBox.appendChild(makeSlot(null));
                renumber();
            });
            return box;
        };

        (squad.slots && squad.slots.length ? squad.slots : [null]).forEach((s) => slotsBox.appendChild(makeSlot(s)));
        renumber();

        addSlotBtn.addEventListener("click", () => {
            if (slotsBox.querySelectorAll(".twdef-slot-edit").length >= MAX[combatType()]) return;
            const slot = makeSlot(null);
            slotsBox.appendChild(slot);
            renumber();
            slot.querySelector(".unit-search-input").focus();
        });

        editor.querySelectorAll("input[name=combat_type]").forEach((radio) => {
            radio.addEventListener("change", () => {
                editor.classList.toggle("twdef-editor-ship", combatType() === "ship");
                renumber();
            });
        });
        editor.classList.toggle("twdef-editor-ship", combatType() === "ship");

        editor.addEventListener("submit", (e) => {
            const slots = [];
            let bad = null;
            slotsBox.querySelectorAll(".twdef-slot-edit").forEach((slotBox) => {
                const options = [];
                slotBox.querySelectorAll(".twdef-option-row").forEach((row) => {
                    const input = row.querySelector(".unit-search-input");
                    if (!row.dataset.baseId) {
                        if (input.value.trim()) bad = bad || input;
                        return;
                    }
                    const opt = { base_id: row.dataset.baseId };
                    FIELDS.forEach((f) => {
                        const v = row.querySelector(`[data-field=${f}]`).value;
                        if (v !== "") opt[f] = parseInt(v, 10);
                    });
                    FLAGS.forEach((f) => { if (row.querySelector(`[data-field=${f}]`).checked) opt[f] = true; });
                    const stats = [];
                    row.querySelectorAll(".twdef-stat-row").forEach((line) => {
                        const v = line.querySelector("[data-stat-min]").value;
                        if (v !== "") stats.push({ stat: line.querySelector("[data-stat]").value, min: parseFloat(v) });
                    });
                    if (stats.length) opt.stats = stats;
                    options.push(opt);
                });
                if (options.length) slots.push({ options });
            });
            if (bad) {
                e.preventDefault();
                bad.setCustomValidity("Выберите юнита из подсказок");
                bad.reportValidity();
                return;
            }
            document.getElementById("twdef-slots-json").value = JSON.stringify(slots);
        });
    }
})();
