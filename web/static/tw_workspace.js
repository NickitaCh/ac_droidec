/* TW command center: local view switches, map inspector and clipboard. */
(() => {
    "use strict";
    if (window.twWorkspaceReady) return;
    window.twWorkspaceReady = true;

    // Navigation and ordinary forms share a visible, accessible progress state.
    // Keep named buttons enabled so their values still reach the server.
    const progress = document.createElement("div");
    progress.className = "tw-loading-notice";
    progress.hidden = true;
    progress.setAttribute("role", "status");
    progress.setAttribute("aria-live", "polite");
    const spinner = document.createElement("span");
    spinner.className = "tw-loading-spinner";
    spinner.setAttribute("aria-hidden", "true");
    const progressText = document.createElement("span");
    progress.append(spinner, progressText);
    document.body.append(progress);
    const resetProgress = () => {
        progress.hidden = true;
        document.querySelectorAll("[data-tw-submitting]").forEach(form => {
            delete form.dataset.twSubmitting;
            form.removeAttribute("aria-busy");
            form.querySelectorAll("[data-tw-busy-label]").forEach(button => {
                button.textContent = button.dataset.twBusyLabel;
                delete button.dataset.twBusyLabel;
            });
        });
    };
    document.addEventListener("submit", event => {
        const form = event.target;
        if (!form.closest("main, dialog") || form.method === "dialog" || form.target === "_blank") return;
        if (event.defaultPrevented) return;
        if (form.dataset.twSubmitting) { event.preventDefault(); return; }
        form.dataset.twSubmitting = "true";
        // Run after validation/confirm and other submit handlers have accepted it.
        queueMicrotask(() => {
            if (event.defaultPrevented) { delete form.dataset.twSubmitting; return; }
            form.setAttribute("aria-busy", "true");
            progressText.textContent = form.dataset.twLoading || (form.method === "get" ? "Загружаем…" : "Сохраняем изменения…");
            progress.hidden = false;
            if (event.submitter) {
                event.submitter.dataset.twBusyLabel = event.submitter.textContent;
                event.submitter.textContent = "Подождите…";
            }
        });
    });
    document.addEventListener("click", event => {
        const link = event.target.closest("a[href]");
        if (!link || !link.closest("main") || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target || link.hasAttribute("download") || link.dataset.ajaxSwap) return;
        const url = new URL(link.href, location.href);
        if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search)) return;
        queueMicrotask(() => {
            if (event.defaultPrevented) return;
            progressText.textContent = "Загружаем страницу…";
            progress.hidden = false;
        });
    });
    window.addEventListener("pageshow", resetProgress);
    const viewPanels = [...document.querySelectorAll("[data-tw-view-panel]")];
    const zonePanels = [...document.querySelectorAll("[data-tw-zone-panel]")];
    const setView = (key, updateUrl = true) => {
        if (!viewPanels.some(p => p.dataset.twViewPanel === key)) return;
        viewPanels.forEach(p => { p.hidden = p.dataset.twViewPanel !== key; });
        document.querySelectorAll("[data-tw-view]").forEach(a => {
            const active = a.dataset.twView === key;
            a.classList.toggle("active", active);
            if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
        });
        if (updateUrl) {
            const url = new URL(location.href);
            url.searchParams.set("view", key);
            if (key !== "map") url.hash = "";
            history.replaceState(null, "", url);
        }
    };
    const setZone = (key, scroll = false, updateUrl = true) => {
        if (!zonePanels.some(p => p.dataset.twZonePanel === key)) return;
        setView("map", updateUrl);
        zonePanels.forEach(p => { p.hidden = p.dataset.twZonePanel !== key; });
        document.querySelectorAll("[data-tw-zone]").forEach(b => {
            const active = b.dataset.twZone === key;
            b.classList.toggle("is-selected", active);
            b.setAttribute("aria-expanded", String(active));
        });
        if (updateUrl) {
            const url = new URL(location.href);
            url.hash = `zone-${key}`;
            history.replaceState(null, "", url);
        }
        if (scroll) document.querySelector(".tw-zone-inspector")?.scrollIntoView({behavior: "smooth", block: "start"});
    };
    document.addEventListener("click", e => {
        const view = e.target.closest("[data-tw-view]");
        if (view) { e.preventDefault(); setView(view.dataset.twView); }
        const zone = e.target.closest("[data-tw-zone]");
        if (zone) setZone(zone.dataset.twZone, matchMedia("(max-width: 1100px)").matches);
        const jump = e.target.closest("[data-tw-jump-zone]");
        if (jump) { e.preventDefault(); setZone(jump.dataset.twJumpZone, true); }
        const copy = e.target.closest("[data-tw-copy]");
        if (copy) {
            const field = document.getElementById(copy.dataset.twCopy);
            const text = field?.value;
            if (text === undefined) return;
            const fallback = () => { field.select(); return document.execCommand("copy"); };
            (navigator.clipboard ? navigator.clipboard.writeText(text).then(() => true).catch(fallback) : Promise.resolve(fallback()))
                .then(ok => { copy.textContent = ok ? "Скопировано" : "Выделите текст и скопируйте"; });
        }
    });
    if (zonePanels.length) {
        const initial = location.hash.replace("#zone-", "");
        setZone(zonePanels.some(p => p.dataset.twZonePanel === initial) ? initial : zonePanels[0].dataset.twZonePanel, false, false);
        setView(new URL(location.href).searchParams.get("view") || "map", false);
        window.addEventListener("hashchange", () => setZone(location.hash.replace("#zone-", ""), false, false));
        window.addEventListener("popstate", () => {
            setZone(location.hash.replace("#zone-", ""), false, false);
            setView(new URL(location.href).searchParams.get("view") || "map", false);
        });
    }
    const playerSearch = document.querySelector("[data-tw-player-search]");
    const emptyOnly = document.querySelector("[data-tw-player-empty]");
    const filterPlayers = () => {
        const query = (playerSearch?.value || "").trim().toLowerCase();
        document.querySelectorAll("[data-tw-player]").forEach(p => {
            p.hidden = !p.dataset.twPlayer.includes(query) || (Boolean(emptyOnly?.checked) && p.dataset.count !== "0");
        });
    };
    playerSearch?.addEventListener("input", filterPlayers);
    emptyOnly?.addEventListener("change", filterPlayers);
    document.querySelectorAll("[data-tw-kind-source]").forEach(source => {
        const target = source.form.querySelector("[data-tw-kind-target]");
        if (!target) return;
        const filter = () => {
            const kind = source.selectedOptions[0]?.dataset.kind;
            [...target.options].forEach(o => { o.hidden = o.disabled = o.dataset.kind !== kind; });
            if (!target.selectedOptions.length || target.selectedOptions[0].disabled) {
                const first = [...target.options].find(o => !o.disabled);
                target.value = first?.value || "";
            }
            target.dispatchEvent(new Event("change"));
        };
        source.addEventListener("change", filter);
        filter();
    });
    document.querySelectorAll("[data-tw-squad-select]").forEach(select => {
        const host = select.form.querySelector("[data-tw-squad-preview]");
        if (!host) return;
        const render = () => {
            const template = document.getElementById(`tw-squad-${select.value}`);
            if (template) host.replaceChildren(template.content.cloneNode(true));
            else host.textContent = "Нет подходящих паков — добавьте состав в библиотеку.";
        };
        select.addEventListener("change", render);
        render();
    });
})();
