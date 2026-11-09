(function () {
    'use strict';
    var form = document.querySelector('[data-guild-sync]');
    if (!form) return;
    var button = form.querySelector('button');
    var status = document.querySelector('[data-guild-sync-status]');
    var timer;
    var sawActive = false;
    function show(job) {
        var active = job.state === 'queued' || job.state === 'running';
        if (job.state === 'done' && sawActive) { location.reload(); return; }
        sawActive = sawActive || active;
        button.disabled = active;
        button.textContent = active ? 'Обновление запущено' : 'Обновить сейчас';
        status.textContent = job.state === 'queued' ? 'В очереди обновления… Можно закрыть страницу.' :
            job.state === 'running' ? 'Обновлено ' + job.completed + ' из ' + job.total + '. Ошибок: ' + job.failed + '.' : job.message || '';
        if (active) timer = setTimeout(poll, 3000);
    }
    function poll() {
        clearTimeout(timer);
        fetch('/sync/status', {headers: {Accept: 'application/json'}})
            .then(function (r) { if (!r.ok) throw Error(); return r.json(); })
            .then(show).catch(function () {
                status.textContent = 'Не удалось получить статус. Повторная проверка через 10 секунд.';
                timer = setTimeout(poll, 10000);
            });
    }
    form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        clearTimeout(timer);
        button.disabled = true;
        sawActive = true;
        status.textContent = 'Запускаю обновление…';
        fetch('/sync', {method: 'POST', headers: {Accept: 'application/json'}})
            .then(function (r) { if (!r.ok) throw Error(); return r.json(); })
            .then(show).catch(function () {
                button.disabled = false;
                status.textContent = 'Не удалось проверить запуск. Проверяю статус…';
                poll();
            });
    });
    poll();
})();
