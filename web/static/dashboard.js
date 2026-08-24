// Простые независимые от фреймворков помощники для страниц дашборда:
// поиск по таблице (input[data-table-search]) и сортировка по клику на th[data-sort].

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-table-search]").forEach((input) => {
        const table = document.getElementById(input.dataset.tableSearch);
        if (!table) return;
        const rows = () => table.tBodies[0].rows;
        input.addEventListener("input", () => {
            const q = input.value.trim().toLowerCase();
            for (const row of rows()) {
                row.style.display = !q || row.dataset.search.includes(q) ? "" : "none";
            }
        });
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
