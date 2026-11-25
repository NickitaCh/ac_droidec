(() => {
    'use strict';
    const panel = document.getElementById('mo-browser');
    if (!panel) return;
    const status = panel.querySelector('[data-browser-status]');
    const cancel = panel.querySelector('[data-browser-cancel]');
    let worker;
    let cancelled = false;
    let uploading = false;
    async function submit(fields) {
        const response = await fetch('/mod-optimizer/browser/result', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({plan: panel.dataset.plan, csrf: panel.dataset.csrf, ...fields})
        });
        const data = await response.json();
        if (!response.ok) throw Error(data.detail || 'Не удалось сохранить результат.');
        location.assign(data.url);
    }
    function fail(message) {
        worker?.terminate();
        status.textContent = message;
        cancel.disabled = false;
        cancel.textContent = 'Закрыть расчёт';
    }
    cancel.addEventListener('click', async () => {
        if (uploading) return;
        cancelled = true;
        worker?.terminate();
        cancel.disabled = true;
        try { await submit({cancel: true}); }
        catch (error) { fail(error.message); }
    });
    (async () => {
        try {
            if (!window.Worker || !window.WebAssembly) throw Error('Для подбора нужен браузер с поддержкой WebAssembly.');
            const response = await fetch('/mod-optimizer/browser/inputs?plan=' + encodeURIComponent(panel.dataset.plan), {cache: 'no-store'});
            const data = await response.json();
            if (!response.ok) throw Error(data.detail || 'Не удалось подготовить расчёт.');
            if (cancelled) return;
            worker = new Worker('/static/mod_optimizer_worker.js?v=20261125projects');
            worker.onerror = () => fail('Вычислитель остановился. Закройте расчёт и попробуйте снова.');
            worker.onmessage = async ({data: message}) => {
                if (cancelled) return;
                if (message.type === 'progress') status.textContent = message.message;
                if (message.type === 'error') fail(message.message);
                if (message.type === 'complete') {
                    worker.terminate();
                    uploading = true;
                    cancel.disabled = true;
                    status.textContent = 'Проверяю и сохраняю результат…';
                    try { await submit(message.result); }
                    catch (error) { uploading = false; fail(error.message); }
                }
            };
            worker.postMessage(data);
        } catch (error) { fail(error.message); }
    })();
})();
