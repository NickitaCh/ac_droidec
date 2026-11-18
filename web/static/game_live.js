(() => {
    const form = document.querySelector("#tb-live-refresh");
    const feedback = document.querySelector("#tb-live-feedback");
    if (!form || !feedback) return;
    const submit = form.querySelector("button");
    let timer;
    async function render(response, includeWatch) {
        if (!response.ok || response.redirected) throw new Error();
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const state = page.querySelector("#tb-live-state");
        if (!state) throw new Error();
        const current = document.querySelector("#tb-live-state");
        const opened = new Set(Array.from(
            current.querySelectorAll("details[open][data-live-details]"), row => row.dataset.liveDetails,
        ));
        for (const detail of state.querySelectorAll("details[data-live-details]")) {
            detail.open = opened.has(detail.dataset.liveDetails);
        }
        current.replaceWith(state);
        if (includeWatch) {
            const controls = page.querySelector("#tb-watch-controls");
            if (controls) document.querySelector("#tb-watch-controls")?.replaceWith(controls);
        }
    }

    function scheduleSnapshot() {
        clearTimeout(timer);
        if (!document.querySelector("#tb-live-state[data-watch-active]")) return;
        timer = setTimeout(async () => {
            try {
                // This GET only reads the site's saved snapshot. The server owns Watch.
                await render(await fetch(form.action, {cache: "no-store"}), true);
            } catch {
                feedback.textContent = "Не удалось получить сохранённый снимок.";
            } finally {
                scheduleSnapshot();
            }
        }, 15000);
    }

    async function refresh() {
        if (submit.disabled) return;
        clearTimeout(timer);
        submit.disabled = true;
        feedback.textContent = "Читаем данные игры…";
        try {
            await render(await fetch(form.action, {method: "POST", body: new FormData(form)}), false);
            feedback.textContent = "";
        } catch {
            feedback.textContent = "Не удалось обновить данные.";
        } finally {
            submit.disabled = false;
            scheduleSnapshot();
        }
    }
    form.addEventListener("submit", event => { event.preventDefault(); refresh(); });
    scheduleSnapshot();
})();
