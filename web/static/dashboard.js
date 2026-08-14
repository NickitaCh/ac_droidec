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
});
