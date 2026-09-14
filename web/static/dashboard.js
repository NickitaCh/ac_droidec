// Простые независимые от фреймворков помощники для страниц дашборда:
// поиск по таблице (input[data-table-search]) и сортировка по клику на th[data-sort].

document.addEventListener("DOMContentLoaded", () => {
    // Тумблер темы (base.html) — клик переключает light/dark и сохраняет выбор в
    // localStorage; сам атрибут data-theme на <html> уже мог быть выставлен раньше
    // инлайн-скриптом в <head>, чтобы не мигать не той темой при загрузке.
    const themeToggle = document.getElementById("theme-toggle");
    if (themeToggle) {
        themeToggle.addEventListener("click", () => {
            const root = document.documentElement;
            const isDark = root.getAttribute("data-theme") === "dark"
                || (!root.hasAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
            const next = isDark ? "light" : "dark";
            root.setAttribute("data-theme", next);
            try { localStorage.setItem("theme", next); } catch (e) {}
        });
    }

    // Сворачивание бокового меню до иконок (десктоп) — кнопка в .sidebar-top;
    // состояние сохраняется в localStorage и (как и тема) читается до первой отрисовки
    // инлайн-скриптом в <head>, чтобы не было вспышки развёрнутого меню при загрузке.
    // Открытые выпадашки (ТБ/Модули/…) закрываются при сворачивании — во всплывающем
    // виде поверх контента незачем сохранять состояние, оставшееся от развёрнутого меню.
    const sidebarToggle = document.getElementById("sidebar-collapse-toggle");
    if (sidebarToggle) {
        sidebarToggle.addEventListener("click", () => {
            const root = document.documentElement;
            const collapsing = root.getAttribute("data-sidebar") !== "collapsed";
            if (collapsing) {
                document.querySelectorAll(".sidebar .nav-dropdown[open]").forEach((d) => { d.open = false; });
                root.setAttribute("data-sidebar", "collapsed");
            } else {
                root.removeAttribute("data-sidebar");
            }
            try { localStorage.setItem("sidebarCollapsed", collapsing ? "1" : "0"); } catch (e) {}
        });
    }

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
            // Программная установка value не рождает нативное 'change' — страницам,
            // которым нужно среагировать на выбор (например, подгрузить омикроны
            // выбранного юнита на /tasks), приходится слушать это явно.
            hidden.dispatchEvent(new Event("change", { bubbles: true }));
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
            { insert: "exclude category [", label: "exclude category […] — исключить флот/пешку" },
            { insert: "exclude player [", label: "exclude player […] unit […] — исключить юнита только у этого игрока (+ stage […])" },
            { insert: "priority player [", label: "priority player […] — приоритет игроку" },
            { insert: "bundle [", label: "bundle […] -> […] — привязать юнитов к тому же донору" },
        ];
        const CATEGORY_OPTIONS = [
            { name: "флот", label: "флот" },
            { name: "пешка", label: "пешка" },
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
                } else if (/exclude\s+category\s*$/i.test(before)) {
                    const q = partial.trim().toLowerCase();
                    items = CATEGORY_OPTIONS.filter((c) => c.name.startsWith(q));
                    activeIndex = items.length ? 0 : -1;
                    render();
                } else if (/\bstage\s*$/i.test(before)) {
                    // Номера этапов — не юниты, никаких подсказок не показываем.
                    close();
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

    // Автодополнение в текстовом поле требований /omicrons/priority/rules — тот же приём,
    // что и у автодополнения /tb/platoons/filters выше (переиспользует getCaretCoordinates,
    // объявленный в этой же области видимости), но проще: единственный тип содержимого
    // скобок — имя юнита (эндпоинт /tb/platoons/api/units, общий с ТБ-взводами — специфики
    // ТБ в нём нет, см. план "Приоритеты омикронов для ВГ"), два ключевых слова строки:
    // "omicron [" / "require unit [".
    document.querySelectorAll(".omicron-rules-textarea").forEach((textarea) => {
        const KEYWORDS = [
            { insert: "omicron [", label: "omicron […] — к какому омикрону относится правило" },
            { insert: "require unit [", label: "require unit […] — юнит должен быть открыт (+ relic N)" },
            { insert: "require omicron [", label: "require omicron […] — другой омикрон должен уже быть поставлен (парные омикроны)" },
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

        const search = (url, q, transform, sep = "?", minLen = 2) => {
            clearTimeout(debounceTimer);
            if (q.trim().length < minLen) { close(); return; }
            debounceTimer = setTimeout(async () => {
                try {
                    const resp = await fetch(`${url}${sep}q=${encodeURIComponent(q.trim())}`);
                    const data = resp.ok ? await resp.json() : [];
                    items = data.map(transform);
                } catch {
                    items = [];
                }
                activeIndex = items.length ? 0 : -1;
                render();
            }, 200);
        };

        // "omicron [" и "require omicron [" допускают "Юнит: Название способности" — после
        // ":" внутри скобок ищем не юнитов, а омикрон-способности УЖЕ введённого юнита
        // (/omicrons/api/abilities, см. web/routes/guild_dashboard.py), нужно только когда у
        // юнита несколько омикрон-способностей. "require unit [" — всегда просто юнит.
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
                const isOmicronTarget = /(^|require\s+)omicron\s*$/i.test(before.trim());
                const colonIdx = isOmicronTarget ? partial.indexOf(":") : -1;
                if (colonIdx !== -1) {
                    const unitPart = partial.slice(0, colonIdx).trim();
                    const abilityPart = partial.slice(colonIdx + 1);
                    const leadingWs = abilityPart.length - abilityPart.trimStart().length;
                    replaceFrom = lineStart + bracketIdx + 1 + colonIdx + 1 + leadingWs;
                    replaceTo = pos;
                    if (unitPart) {
                        // minLen 0 — со списком способностей одного юнита (обычно 1-3
                        // штуки) нет смысла заставлять сначала что-то набрать: список должен
                        // появиться сразу после "Юнит:", чтобы было видно, что вообще писать.
                        search(
                            `/omicrons/api/abilities?unit=${encodeURIComponent(unitPart)}`,
                            abilityPart,
                            (a) => ({ name: a.name, label: a.name }),
                            "&",
                            0,
                        );
                    } else {
                        close();
                    }
                    return;
                }
                replaceFrom = lineStart + bracketIdx + 1;
                replaceTo = pos;
                search("/tb/platoons/api/units", partial, (u) => ({ name: u.name, label: u.name }));
                return;
            }

            // Ключевые слова ("omicron [", "require unit [", "require omicron [") нужно
            // предлагать не только в начале строки, но и после уже закрытой скобки —
            // например второе "omicron" в "omicron [...] require omicron" (парные омикроны).
            // bracketIdx — это lastIndexOf("["), он остаётся >= 0 и после закрытой скобки,
            // поэтому проверка "bracketIdx === -1" ошибочно отсекала этот случай — сравнивать
            // нужно с хвостом строки после последней "]", а не с bracketIdx.
            const lastCloseIdx = lineSoFar.lastIndexOf("]");
            const tail = lineSoFar.slice(lastCloseIdx + 1);
            const trimmedTail = tail.trim();
            if (trimmedTail && /^[a-zA-Z ]*$/.test(trimmedTail)) {
                const leadingWs = tail.length - tail.trimStart().length;
                replaceFrom = lineStart + lastCloseIdx + 1 + leadingWs;
                replaceTo = pos;
                const q = trimmedTail.toLowerCase();
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
        const isNavDropdown = d.classList.contains("nav-dropdown");
        const menu = isNavDropdown ? d.querySelector(".nav-dropdown-menu") : null;
        d.addEventListener("toggle", () => {
            if (!d.open) return;
            document.querySelectorAll(`${group}[open]`).forEach((other) => {
                if (other !== d) other.open = false;
            });
            // Свёрнутое боковое меню (иконки, см. .sidebar-collapse-toggle) — .sidebar сам
            // является скролл-контейнером (overflow-y: auto на десктопе), из-за чего браузер
            // по спеке overflow форсит и overflow-x в auto (нельзя сделать одну ось visible,
            // если другая auto/scroll) — CSS position:absolute для .nav-dropdown-menu в таком
            // сайдбаре обрезался бы вместо того, чтобы всплыть вправо поверх контента. fixed
            // выходит за пределы скролл-контейнера, но тогда его base — вьюпорт, а не
            // сайдбар, поэтому координаты считаем вручную от текущего summary.
            if (menu && document.documentElement.getAttribute("data-sidebar") === "collapsed") {
                const rect = d.querySelector("summary").getBoundingClientRect();
                menu.style.left = `${rect.right + 6}px`;
                menu.style.top = `${rect.top}px`;
            } else if (menu) {
                menu.style.left = "";
                menu.style.top = "";
            }
        });
    });
    document.addEventListener("click", (e) => {
        document.querySelectorAll(".row-actions details[open], .nav-dropdown[open]").forEach((d) => {
            if (!d.contains(e.target)) d.open = false;
        });
    });

    // ---- Попап выбора игрока на слот взвода ТБ (/tb/platoons) — по прямому запросу
    // пользователя 2026-09-14: список кандидатов раньше открывался инлайн в узкой
    // колонке .platoon-op-card (топ-20 по релику от get_player_unit_owners_bulk) — теперь
    // полноразмерный попап, куда /tb/platoons/api/slot отдаёт ПОЛНЫЙ список игроков гильдии
    // (включая тех, кто юнитом не владеет вообще — owns_unit:false), с поиском по имени и
    // сортировкой. Один общий <dialog> на странице, переиспользуется для любого слота.
    (() => {
        const dialog = document.getElementById("platoon-picker-dialog");
        if (!dialog) return;
        const listEl = document.getElementById("platoon-picker-list");
        const loadingEl = document.getElementById("platoon-picker-loading");
        const errorEl = document.getElementById("platoon-picker-error");
        const titleEl = document.getElementById("platoon-picker-title");
        const searchEl = document.getElementById("platoon-picker-search");
        const sortEl = document.getElementById("platoon-picker-sort");
        const closeBtn = document.getElementById("platoon-picker-close");
        const assignForm = document.getElementById("platoon-assign-form");

        let currentCandidates = [];
        let currentCtx = null;
        const STATUS_ORDER = { assigned: 0, eligible: 1, ineligible: 2 };

        function reasonText(c) {
            const reasons = [];
            if (!c.owns_unit) reasons.push("не владеет юнитом");
            else if (!c.meets_min) reasons.push(c.is_ship ? "не хватает ★" : "не хватает релика");
            if (c.used_elsewhere) reasons.push(`уже занят на этом этапе${c.used_at_label ? ": " + c.used_at_label : ""}`);
            if (c.excluded_by_filter) reasons.push("исключён фильтром");
            if (c.at_cap) reasons.push("лимит 10/этап на планету исчерпан");
            return reasons.join(", ");
        }

        function render() {
            const q = searchEl.value.trim().toLowerCase();
            let items = currentCandidates.filter((c) => c.name.toLowerCase().includes(q));
            const sortBy = sortEl.value;
            items.sort((a, b) => {
                if (sortBy === "name") return a.name.localeCompare(b.name, "ru");
                if (sortBy === "relic") {
                    const av = a.is_ship ? a.stars : a.relic;
                    const bv = b.is_ship ? b.stars : b.relic;
                    return bv - av;
                }
                const so = STATUS_ORDER[a.status] - STATUS_ORDER[b.status];
                return so !== 0 ? so : a.name.localeCompare(b.name, "ru");
            });
            listEl.innerHTML = "";
            if (!items.length) {
                listEl.innerHTML = '<li class="empty-state">Никого не найдено.</li>';
                return;
            }
            for (const c of items) {
                // used_elsewhere/excluded_by_filter/at_cap — жёсткие причины (кнопка
                // недоступна, как и раньше в инлайн-списке); просто "не хватает релика/★"
                // или "не владеет юнитом" — мягкая причина, кнопка активна (офицер может
                // назначить вручную всё равно, это уже было так в прежнем UI).
                const hardBlocked = c.used_elsewhere || c.excluded_by_filter || c.at_cap;
                const isAssigned = c.status === "assigned";
                const icon = isAssigned ? "🔒" : hardBlocked ? "🚫" : c.meets_min ? "✅" : "⚠️";
                const statValue = c.is_ship ? `${c.stars}★` : `релик ${c.relic}`;
                const reason = reasonText(c);

                const li = document.createElement("li");
                li.className = `platoon-picker-row platoon-picker-row-${isAssigned ? "assigned" : hardBlocked ? "ineligible" : "eligible"}`;
                li.innerHTML = `
                    <span class="platoon-picker-icon">${icon}</span>
                    <span class="platoon-picker-name">${c.is_priority ? "⭐ " : ""}${c.name}</span>
                    <span class="platoon-picker-stat">${statValue}</span>
                    ${reason ? `<span class="platoon-picker-reason">${reason}</span>` : ""}
                `;
                if (!isAssigned) {
                    const btn = document.createElement("button");
                    btn.type = "button";
                    btn.className = "button";
                    btn.textContent = "Назначить";
                    btn.disabled = hardBlocked;
                    btn.addEventListener("click", () => {
                        assignForm.plan_id.value = currentCtx.planId;
                        assignForm.round_num.value = currentCtx.round;
                        assignForm.planet.value = currentCtx.planet;
                        assignForm.operation.value = currentCtx.operation;
                        assignForm.slot_index.value = currentCtx.slotIndex;
                        assignForm.ally_code.value = c.ally_code;
                        assignForm.anchor.value = currentCtx.anchor;
                        assignForm.requestSubmit();
                    });
                    li.appendChild(btn);
                }
                listEl.appendChild(li);
            }
        }

        document.querySelectorAll(".platoon-pick-btn").forEach((btn) => {
            btn.addEventListener("click", async () => {
                currentCtx = {
                    planId: btn.dataset.planId, round: btn.dataset.round, planet: btn.dataset.planet,
                    operation: btn.dataset.operation, slotIndex: btn.dataset.slotIndex, anchor: btn.dataset.anchor,
                };
                titleEl.textContent = `${btn.dataset.unit} — ${btn.dataset.planet}, операция ${btn.dataset.operation}`;
                searchEl.value = "";
                sortEl.value = "status";
                listEl.innerHTML = "";
                errorEl.hidden = true;
                loadingEl.hidden = false;
                dialog.showModal();
                try {
                    const params = new URLSearchParams({
                        plan_id: currentCtx.planId, round: currentCtx.round, planet: currentCtx.planet,
                        operation: currentCtx.operation, slot_index: currentCtx.slotIndex,
                    });
                    const resp = await fetch(`/tb/platoons/api/slot?${params}`);
                    if (!resp.ok) throw new Error(await resp.text());
                    const data = await resp.json();
                    currentCandidates = data.candidates;
                    loadingEl.hidden = true;
                    render();
                } catch (e) {
                    loadingEl.hidden = true;
                    errorEl.hidden = false;
                    errorEl.textContent = "Не удалось загрузить список кандидатов.";
                }
            });
        });

        searchEl.addEventListener("input", render);
        sortEl.addEventListener("change", render);
        closeBtn.addEventListener("click", () => dialog.close());
        dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
    })();

    // ---- Попап привязки Discord из виджета "Состав гильдии" на главной — клик по
    // бейджу "не привязан" (data-register-link-btn) открывает форму /registration
    // (POST) с уже заполненным ally_code, офицеру остаётся указать только Discord ID.
    (() => {
        const dialog = document.getElementById("register-link-dialog");
        if (!dialog) return;
        const nameEl = document.getElementById("register-link-name");
        const allyCodeDisplayEl = document.getElementById("register-link-ally-code-display");
        const allyCodeInput = document.getElementById("register-link-ally-code");
        const cancelBtn = document.getElementById("register-link-cancel");

        document.querySelectorAll("[data-register-link-btn]").forEach((btn) => {
            btn.addEventListener("click", () => {
                nameEl.textContent = btn.dataset.name;
                allyCodeDisplayEl.textContent = btn.dataset.allyCode;
                allyCodeInput.value = btn.dataset.allyCode;
                dialog.showModal();
            });
        });

        cancelBtn.addEventListener("click", () => dialog.close());
        dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
    })();
});
