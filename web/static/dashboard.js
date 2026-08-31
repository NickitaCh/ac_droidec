// Простые независимые от фреймворков помощники для страниц дашборда:
// поиск по таблице (input[data-table-search]) и сортировка по клику на th[data-sort].

document.addEventListener("DOMContentLoaded", () => {
    // data-table-search — текстовый поиск по строке (row.dataset.search); необязательный
    // соседний select[data-table-filter] с тем же id таблицы — точный фильтр по значению
    // (row.dataset.mode для /admin/omicron-phrases: ТБ/ВГ/ВА/рейд/…, может быть несколько
    // через пробел — строка "по умолчанию" совпадает, если ЛЮБОЙ из омикронов персонажа
    // подходит под выбранный режим). Оба условия учитываются вместе, а не по отдельности,
    // чтобы поиск и фильтр не перетирали видимость друг друга.
    document.querySelectorAll("[data-table-search]").forEach((input) => {
        const table = document.getElementById(input.dataset.tableSearch);
        if (!table) return;
        const rows = () => table.tBodies[0].rows;
        const filterSelect = document.querySelector(`[data-table-filter="${input.dataset.tableSearch}"]`);
        const apply = () => {
            const q = input.value.trim().toLowerCase();
            const mode = filterSelect ? filterSelect.value : "";
            for (const row of rows()) {
                const matchesSearch = !q || row.dataset.search.includes(q);
                const matchesMode = !mode || (row.dataset.mode || "").split(" ").includes(mode);
                row.style.display = matchesSearch && matchesMode ? "" : "none";
            }
        };
        input.addEventListener("input", apply);
        if (filterSelect) filterSelect.addEventListener("change", apply);
    });

    // Поиск по ленте активности (/activity) — структура там не таблица, а вложенные
    // карточки дата -> игрок -> строка, поэтому отдельный от data-table-search виджет:
    // фильтрует строки с [data-search], затем прячет опустевшие карточки игрока и
    // целиком опустевшие дневные группы, а не просто скрывает отдельные <tr>.
    document.querySelectorAll("[data-feed-search]").forEach((input) => {
        const feed = document.getElementById(input.dataset.feedSearch);
        if (!feed) return;
        input.addEventListener("input", () => {
            const q = input.value.trim().toLowerCase();
            feed.querySelectorAll(".activity-event-row").forEach((row) => {
                row.style.display = !q || row.dataset.search.includes(q) ? "" : "none";
            });
            feed.querySelectorAll(".activity-player-card").forEach((card) => {
                const anyVisible = Array.from(card.querySelectorAll(".activity-event-row"))
                    .some((row) => row.style.display !== "none");
                card.style.display = anyVisible ? "" : "none";
            });
            feed.querySelectorAll(".activity-date-group").forEach((group) => {
                const anyVisible = Array.from(group.querySelectorAll(".activity-player-card"))
                    .some((card) => card.style.display !== "none");
                group.style.display = anyVisible ? "" : "none";
            });
        });
    });

    document.querySelectorAll("table.data").forEach((table) => {
        const headers = table.querySelectorAll("th[data-sort]");
        headers.forEach((th, colIndex) => {
            th.addEventListener("click", () => {
                const tbody = table.tBodies[0];
                const rows = Array.from(tbody.rows);
                const type = th.dataset.sort; // "num" или "text"
                const asc = th.dataset.sortDir !== "asc";

                rows.sort((a, b) => {
                    let va = a.cells[colIndex].dataset.value ?? a.cells[colIndex].textContent.trim();
                    let vb = b.cells[colIndex].dataset.value ?? b.cells[colIndex].textContent.trim();
                    if (type === "num") {
                        va = parseFloat(va) || 0;
                        vb = parseFloat(vb) || 0;
                        return asc ? va - vb : vb - va;
                    }
                    return asc ? String(va).localeCompare(String(vb), "ru") : String(vb).localeCompare(String(va), "ru");
                });

                headers.forEach((h) => { h.removeAttribute("data-sort-dir"); h.querySelector(".sort-arrow")?.remove(); });
                th.dataset.sortDir = asc ? "asc" : "desc";
                const arrow = document.createElement("span");
                arrow.className = "sort-arrow";
                arrow.textContent = asc ? "▲" : "▼";
                th.appendChild(arrow);

                rows.forEach((row) => tbody.appendChild(row));
            });
        });
    });

    // Живой поиск (персонаж для формы плейта, игрок для формы нарушения и т.п.) —
    // общий виджет: [data-unit-search] (легаси-имя, юниты) и [data-live-search]
    // (общий случай, настраивается через data-url/data-value-field/data-label-field/
    // data-min-length) используют один и тот же обработчик, только источник данных
    // и поля ответа разные. Выбор кладёт value-field в скрытое поле формы.
    const initSearchWidget = (wrap, { url, valueField, labelField, minLength, emptyText, required = true }) => {
        const input = wrap.querySelector(".unit-search-input");
        const hidden = wrap.querySelector(".unit-search-value");
        const results = wrap.querySelector(".unit-search-results");
        let items = [];
        let activeIndex = -1;
        let debounceTimer;

        const render = () => {
            results.innerHTML = "";
            if (items.length === 0) {
                results.innerHTML = `<div class="unit-search-empty">${emptyText}</div>`;
            } else {
                items.forEach((item, i) => {
                    const el = document.createElement("div");
                    el.className = "unit-search-result" + (i === activeIndex ? " active" : "");
                    el.textContent = item[labelField];
                    el.addEventListener("mousedown", (e) => { e.preventDefault(); select(item); });
                    results.appendChild(el);
                });
            }
            results.classList.add("open");
        };

        const select = (item) => {
            input.value = item[labelField];
            hidden.value = item[valueField];
            input.setCustomValidity("");
            results.classList.remove("open");
        };

        input.addEventListener("input", () => {
            hidden.value = "";
            const q = input.value.trim();
            clearTimeout(debounceTimer);
            if (q.length < minLength) { results.classList.remove("open"); return; }
            debounceTimer = setTimeout(async () => {
                try {
                    const resp = await fetch(`${url}?q=${encodeURIComponent(q)}`);
                    items = resp.ok ? await resp.json() : [];
                } catch {
                    items = [];
                }
                activeIndex = -1;
                render();
            }, 200);
        });

        input.addEventListener("keydown", (e) => {
            if (!results.classList.contains("open") || items.length === 0) return;
            if (e.key === "ArrowDown") { e.preventDefault(); activeIndex = Math.min(activeIndex + 1, items.length - 1); render(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); activeIndex = Math.max(activeIndex - 1, 0); render(); }
            else if (e.key === "Enter" && activeIndex >= 0) { e.preventDefault(); select(items[activeIndex]); }
            else if (e.key === "Escape") { results.classList.remove("open"); }
        });

        input.addEventListener("blur", () => setTimeout(() => results.classList.remove("open"), 150));

        // data-required="false" — для форм, где рядом есть равноценная альтернатива
        // выбору из подсказок (например, ручной ввод ID), поэтому пустой hidden не
        // должен блокировать отправку. По умолчанию — обязательный выбор, как раньше.
        if (required) {
            const form = wrap.closest("form");
            if (form) {
                form.addEventListener("submit", (e) => {
                    if (!hidden.value) {
                        e.preventDefault();
                        input.setCustomValidity("Выберите вариант из списка подсказок");
                        input.reportValidity();
                    }
                });
            }
        }
    };

    // Легаси: поиск персонажа для формы добавления требования к плейту (/plates/<name>) —
    // GET /plates/api/units?q=, выбор кладёт base_id в скрытое поле формы.
    document.querySelectorAll("[data-unit-search]").forEach((wrap) => {
        initSearchWidget(wrap, {
            url: "/plates/api/units", valueField: "base_id", labelField: "name",
            minLength: 2, emptyText: "Ничего не найдено",
        });
    });

    // Общий случай (например, поиск игрока для формы нарушения/дня рождения) —
    // источник и поля ответа задаются на разметке: data-url/data-value-field/
    // data-label-field/data-required (по умолчанию выбор из подсказок обязателен
    // для отправки формы; data-required="false" — когда рядом есть равноценная
    // альтернатива, например ручной ввод ID).
    document.querySelectorAll("[data-live-search]").forEach((wrap) => {
        initSearchWidget(wrap, {
            url: wrap.dataset.url,
            valueField: wrap.dataset.valueField || "value",
            labelField: wrap.dataset.labelField || "name",
            minLength: parseInt(wrap.dataset.minLength || "2", 10),
            emptyText: "Ничего не найдено",
            required: wrap.dataset.required !== "false",
        });
    });

    // Живой счётчик "выбрано модов/6" на /mod-builder — [data-set-picker] суммирует все
    // [data-set-count] инпуты при вводе и подсвечивает превышение (не блокирует отправку —
    // сумма сетов формально может быть меньше 6, если сборка ещё не полностью задана).
    document.querySelectorAll("[data-set-picker]").forEach((picker) => {
        const total = picker.querySelector("[data-set-total]");
        const totalLine = picker.querySelector(".mod-set-total");
        const inputs = picker.querySelectorAll("[data-set-count]");
        if (!total) return;
        const recalc = () => {
            let sum = 0;
            inputs.forEach((input) => { sum += parseInt(input.value, 10) || 0; });
            total.textContent = sum;
            if (totalLine) totalLine.classList.toggle("over-limit", sum > 6);
        };
        inputs.forEach((input) => input.addEventListener("input", recalc));
        recalc();
    });

    // Живая подсветка карточки слота мода на /mod-builder — бордер загорается сразу
    // при выборе сета, не дожидаясь отправки формы (серверная "filled" метка выставляется
    // только при рендере, а не отслеживает live-изменения на клиенте).
    document.querySelectorAll(".mod-slot-card select").forEach((select) => {
        const card = select.closest(".mod-slot-card");
        select.addEventListener("change", () => {
            card.classList.toggle("filled", !!select.value);
        });
    });

    // Повторяемые строки формы (статы от модов на /mod-builder) — [data-row-group]
    // оборачивает .row-group-rows (контейнер строк) + .row-group-add (кнопка "добавить"):
    // клонирует последнюю строку, очищает её поля. Последнюю оставшуюся строку удалить
    // нельзя (иначе форма могла бы уйти без единого input с именем stat_name/stat_value).
    document.querySelectorAll("[data-row-group]").forEach((group) => {
        const rows = group.querySelector(".row-group-rows");
        const addBtn = group.querySelector(".row-group-add");
        if (!rows || !addBtn) return;

        const bindRemove = (row) => {
            const btn = row.querySelector(".row-group-remove");
            if (!btn) return;
            btn.addEventListener("click", () => {
                if (rows.children.length > 1) row.remove();
            });
        };

        // Подпись единицы (%/число) у поля "Значение" — определяется выбранным статом в
        // той же строке (option.dataset.percent, см. stat_engine.PERCENT_STATS на бэкенде):
        // статы вроде Potency/Armor/Crit Chance вводятся в игровых %, Speed/Health — числом.
        const syncUnit = (row) => {
            const select = row.querySelector('select[name="stat_name"]');
            const unit = row.querySelector(".stat-value-unit");
            if (!select || !unit) return;
            const opt = select.options[select.selectedIndex];
            unit.textContent = opt && opt.dataset.percent === "1" ? "%" : "";
        };

        rows.querySelectorAll(".row-group-row").forEach((row) => {
            bindRemove(row);
            syncUnit(row);
        });

        group.addEventListener("change", (e) => {
            if (e.target.matches('select[name="stat_name"]')) {
                syncUnit(e.target.closest(".row-group-row"));
            }
        });

        addBtn.addEventListener("click", () => {
            const last = rows.querySelector(".row-group-row:last-child");
            if (!last) return;
            const clone = last.cloneNode(true);
            clone.querySelectorAll("input, select").forEach((el) => { el.value = ""; });
            bindRemove(clone);
            rows.appendChild(clone);
            syncUnit(clone);
        });
    });

    // Интерактивное автодополнение в текстовом поле правил /tb/platoons/filters — по
    // прямому запросу пользователя 2026-08-31 ("как в IDE или как в дискорде": начать
    // печатать ключевое слово или имя юнита, код предлагает варианты). Контекст
    // определяется чисто по тексту строки до курсора (без парсинга всего файла правил):
    //   1) курсор внутри ещё не закрытой "[" на этой строке — ищем игрока или юнита в
    //      зависимости от того, что стоит перед "[" (exclude/priority player -> игрок,
    //      иначе — юнит: exclude unit, bundle-триггер, элементы пула bundle после "->");
    //   2) курсор в самом начале строки, ничего похожего на "[" ещё нет — предлагаем
    //      ключевые слова целиком (exclude player [ / exclude unit [ / bundle [ / priority
    //      player [).
    // Позиционирование — классический приём "textarea caret position" (клон стилей
    // textarea в скрытый div, маркер-спан на месте курсора, координаты — через
    // getBoundingClientRect маркера и самой textarea).
    const getCaretCoordinates = (textarea, position) => {
        const mirror = document.createElement("div");
        const style = getComputedStyle(textarea);
        [
            "boxSizing", "width", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
            "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
            "fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
        ].forEach((prop) => { mirror.style[prop] = style[prop]; });
        mirror.style.position = "absolute";
        mirror.style.visibility = "hidden";
        mirror.style.whiteSpace = "pre-wrap";
        mirror.style.wordWrap = "break-word";
        mirror.style.top = "0";
        mirror.style.left = "-9999px";
        document.body.appendChild(mirror);
        mirror.textContent = textarea.value.substring(0, position);
        const marker = document.createElement("span");
        marker.textContent = "​";
        mirror.appendChild(marker);
        const rectMirror = mirror.getBoundingClientRect();
        const rectMarker = marker.getBoundingClientRect();
        document.body.removeChild(mirror);
        const rectTextarea = textarea.getBoundingClientRect();
        const lineHeight = parseFloat(style.lineHeight) || 16;
        return {
            top: rectMarker.top - rectMirror.top + rectTextarea.top - textarea.scrollTop + lineHeight,
            left: rectMarker.left - rectMirror.left + rectTextarea.left - textarea.scrollLeft,
        };
    };

    document.querySelectorAll(".platoon-filters-textarea").forEach((textarea) => {
        const KEYWORDS = [
            { insert: "exclude player [", label: "exclude player […] — исключить игрока" },
            { insert: "exclude unit [", label: "exclude unit […] — исключить юнита" },
            { insert: "priority player [", label: "priority player […] — приоритет игроку" },
            { insert: "bundle [", label: "bundle […] -> […] — привязать юнитов к тому же донору" },
        ];

        const box = document.createElement("div");
        box.className = "pf-autocomplete";
        document.body.appendChild(box);

        let items = [];
        let activeIndex = -1;
        let replaceFrom = 0;
        let replaceTo = 0;
        let debounceTimer;

        const close = () => { box.classList.remove("open"); items = []; activeIndex = -1; };

        const render = () => {
            box.innerHTML = "";
            if (items.length === 0) { close(); return; }
            items.forEach((item, i) => {
                const el = document.createElement("div");
                el.className = "pf-autocomplete-item" + (i === activeIndex ? " active" : "");
                el.textContent = item.label;
                el.addEventListener("mousedown", (e) => { e.preventDefault(); accept(item); });
                box.appendChild(el);
            });
            const pos = getCaretCoordinates(textarea, textarea.selectionStart);
            box.style.top = `${pos.top}px`;
            box.style.left = `${pos.left}px`;
            box.classList.add("open");
        };

        const accept = (item) => {
            const value = item.insert !== undefined ? item.insert : `${item.name}]`;
            const text = textarea.value;
            textarea.value = text.slice(0, replaceFrom) + value + text.slice(replaceTo);
            const newPos = replaceFrom + value.length;
            textarea.setSelectionRange(newPos, newPos);
            textarea.focus();
            close();
            evaluate();
        };

        const search = async (url, q, transform) => {
            clearTimeout(debounceTimer);
            if (q.trim().length < 2) { close(); return; }
            debounceTimer = setTimeout(async () => {
                try {
                    const resp = await fetch(`${url}?q=${encodeURIComponent(q.trim())}`);
                    const data = resp.ok ? await resp.json() : [];
                    items = data.map(transform);
                } catch {
                    items = [];
                }
                activeIndex = items.length ? 0 : -1;
                render();
            }, 200);
        };

        const evaluate = () => {
            if (textarea.selectionStart !== textarea.selectionEnd) { close(); return; }
            const pos = textarea.selectionStart;
            const text = textarea.value;
            const lineStart = text.lastIndexOf("\n", pos - 1) + 1;
            const lineSoFar = text.slice(lineStart, pos);

            const bracketIdx = lineSoFar.lastIndexOf("[");
            const closedAfter = bracketIdx >= 0 && lineSoFar.indexOf("]", bracketIdx) !== -1;
            if (bracketIdx >= 0 && !closedAfter) {
                const before = lineSoFar.slice(0, bracketIdx);
                const partial = lineSoFar.slice(bracketIdx + 1);
                replaceFrom = lineStart + bracketIdx + 1;
                replaceTo = pos;
                if (/(exclude|priority)\s+player\s*$/i.test(before)) {
                    search("/violations/api/players", partial, (p) => ({ name: p.name, label: p.name }));
                } else {
                    search("/tb/platoons/api/units", partial, (u) => ({ name: u.name, label: u.name }));
                }
                return;
            }

            const trimmed = lineSoFar.trim();
            if (bracketIdx === -1 && trimmed && /^[a-zA-Z ]*$/.test(trimmed)) {
                const leadingWs = lineSoFar.length - lineSoFar.trimStart().length;
                replaceFrom = lineStart + leadingWs;
                replaceTo = pos;
                const q = trimmed.toLowerCase();
                items = KEYWORDS.filter((k) => k.insert.toLowerCase().startsWith(q));
                activeIndex = items.length ? 0 : -1;
                render();
                return;
            }

            close();
        };

        textarea.addEventListener("input", evaluate);
        textarea.addEventListener("click", evaluate);
        textarea.addEventListener("keyup", (e) => {
            if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) evaluate();
        });
        textarea.addEventListener("keydown", (e) => {
            if (!box.classList.contains("open") || items.length === 0) return;
            if (e.key === "ArrowDown") { e.preventDefault(); activeIndex = Math.min(activeIndex + 1, items.length - 1); render(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); activeIndex = Math.max(activeIndex - 1, 0); render(); }
            else if ((e.key === "Enter" || e.key === "Tab") && activeIndex >= 0) { e.preventDefault(); accept(items[activeIndex]); }
            else if (e.key === "Escape") { close(); }
        });
        textarea.addEventListener("blur", () => setTimeout(close, 150));
    });

    // Поповеры переименования/редактирования (details.row-actions) и выпадающие
    // пункты навбара (details.nav-dropdown) — закрывать остальные открытые details
    // (в своей же группе) при открытии одного и по клику вне, иначе накапливаются открытыми.
    document.querySelectorAll(".row-actions details, .nav-dropdown").forEach((d) => {
        const group = d.classList.contains("nav-dropdown") ? ".nav-dropdown" : ".row-actions details";
        d.addEventListener("toggle", () => {
            if (!d.open) return;
            document.querySelectorAll(`${group}[open]`).forEach((other) => {
                if (other !== d) other.open = false;
            });
        });
    });
    document.addEventListener("click", (e) => {
        document.querySelectorAll(".row-actions details[open], .nav-dropdown[open]").forEach((d) => {
            if (!d.contains(e.target)) d.open = false;
        });
    });
});
