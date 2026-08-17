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

    // Поиск персонажа для формы добавления требования к плейту (/plates/<name>) —
    // GET /plates/api/units?q=, выбор кладёт base_id в скрытое поле формы.
    document.querySelectorAll("[data-unit-search]").forEach((wrap) => {
        const input = wrap.querySelector(".unit-search-input");
        const hidden = wrap.querySelector(".unit-search-value");
        const results = wrap.querySelector(".unit-search-results");
        let items = [];
        let activeIndex = -1;
        let debounceTimer;

        const render = () => {
            results.innerHTML = "";
            if (items.length === 0) {
                results.innerHTML = '<div class="unit-search-empty">Ничего не найдено</div>';
            } else {
                items.forEach((item, i) => {
                    const el = document.createElement("div");
                    el.className = "unit-search-result" + (i === activeIndex ? " active" : "");
                    el.textContent = item.name;
                    el.addEventListener("mousedown", (e) => { e.preventDefault(); select(item); });
                    results.appendChild(el);
                });
            }
            results.classList.add("open");
        };

        const select = (item) => {
            input.value = item.name;
            hidden.value = item.base_id;
            input.setCustomValidity("");
            results.classList.remove("open");
        };

        input.addEventListener("input", () => {
            hidden.value = "";
            const q = input.value.trim();
            clearTimeout(debounceTimer);
            if (q.length < 2) { results.classList.remove("open"); return; }
            debounceTimer = setTimeout(async () => {
                try {
                    const resp = await fetch(`/plates/api/units?q=${encodeURIComponent(q)}`);
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

        const form = wrap.closest("form");
        if (form) {
            form.addEventListener("submit", (e) => {
                if (!hidden.value) {
                    e.preventDefault();
                    input.setCustomValidity("Выберите персонажа из списка подсказок");
                    input.reportValidity();
                }
            });
        }
    });

    // Поповеры переименования/редактирования (details.row-actions) — закрывать
    // остальные при открытии одного и по клику вне, иначе накапливаются открытыми.
    document.querySelectorAll(".row-actions details").forEach((d) => {
        d.addEventListener("toggle", () => {
            if (!d.open) return;
            document.querySelectorAll(".row-actions details[open]").forEach((other) => {
                if (other !== d) other.open = false;
            });
        });
    });
    document.addEventListener("click", (e) => {
        document.querySelectorAll(".row-actions details[open]").forEach((d) => {
            if (!d.contains(e.target)) d.open = false;
        });
    });
});
