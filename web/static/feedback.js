/* Журнал ошибок браузера + окно «Сообщить о проблеме» (2026-10-22).
   Грузится в <head> без defer, чтобы ловить ошибки с самого начала загрузки страницы.
   Журнал живёт в sessionStorage (последние MAX_ENTRIES записей вкладки, переживает переходы
   между страницами). При открытии окна к нему подмешиваются ошибки сервера этого
   пользователя за сутки (/feedback/log). Журнал можно приложить к обращению или скачать. */
(function () {
    "use strict";
    var KEY = "acDroidecLog";
    var MAX_ENTRIES = 80;
    var MAX_TEXT = 1500;

    function now() {
        var d = new Date();
        function p(n) { return (n < 10 ? "0" : "") + n; }
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }

    function read() {
        try { return JSON.parse(sessionStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
    }

    function add(type, text) {
        try {
            var entries = read();
            entries.push({ t: now(), type: type, page: location.pathname + location.search, text: String(text).slice(0, MAX_TEXT) });
            if (entries.length > MAX_ENTRIES) entries = entries.slice(entries.length - MAX_ENTRIES);
            sessionStorage.setItem(KEY, JSON.stringify(entries));
        } catch (e) { /* хранилище недоступно — журнал просто не ведётся */ }
    }

    function stringify(args) {
        return Array.prototype.map.call(args, function (a) {
            if (a instanceof Error) return a.stack || a.message;
            if (typeof a === "object") { try { return JSON.stringify(a); } catch (e) { return String(a); } }
            return String(a);
        }).join(" ");
    }

    add("открыта", document.referrer ? "переход с " + document.referrer : "страница");

    window.addEventListener("error", function (ev) {
        if (ev.error || ev.message) {
            add("ошибка JS", (ev.error && ev.error.stack) || (ev.message + " @ " + ev.filename + ":" + ev.lineno));
        } else if (ev.target && (ev.target.src || ev.target.href)) {
            add("не загрузилось", ev.target.src || ev.target.href);
        }
    }, true);
    window.addEventListener("unhandledrejection", function (ev) {
        var r = ev.reason;
        add("ошибка JS", (r && (r.stack || r.message)) || String(r));
    });
    ["error", "warn"].forEach(function (level) {
        var orig = console[level];
        if (!orig) return;
        console[level] = function () {
            add("console." + level, stringify(arguments));
            return orig.apply(console, arguments);
        };
    });
    if (window.fetch) {
        var origFetch = window.fetch;
        window.fetch = function (input, init) {
            var url = typeof input === "string" ? input : (input && input.url) || "";
            var method = (init && init.method) || (input && input.method) || "GET";
            return origFetch.apply(this, arguments).then(function (resp) {
                if (resp.status >= 400 && url.indexOf("/feedback") !== 0) add("запрос", method + " " + url + " → " + resp.status);
                return resp;
            }, function (err) {
                add("запрос", method + " " + url + " → сеть: " + (err && err.message));
                throw err;
            });
        };
    }

    function clientLines() {
        return read().map(function (e) {
            return "[" + e.t + "] " + e.type + " · " + e.page + " · " + e.text;
        });
    }

    function buildLog(serverLines) {
        var parts = [
            "Страница: " + location.href,
            "Браузер: " + navigator.userAgent,
            "Экран: " + window.innerWidth + "×" + window.innerHeight + ", тема: " + (document.documentElement.getAttribute("data-theme") || "системная"),
            "Время: " + now(),
            "",
            "---- браузер (эта вкладка) ----",
        ];
        var client = clientLines();
        parts = parts.concat(client.length ? client : ["(записей нет)"]);
        parts.push("", "---- сервер (ваши ошибки за сутки) ----");
        parts = parts.concat(serverLines && serverLines.length ? serverLines : ["(ошибок нет)"]);
        return parts.join("\n");
    }

    function download(text) {
        var blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "ac-droidec-журнал-" + now().replace(/[: ]/g, "-") + ".txt";
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    }

    document.addEventListener("DOMContentLoaded", function () {
        var dialog = document.getElementById("feedback-dialog");
        if (!dialog) return;
        var form = dialog.querySelector("form");
        var logBox = dialog.querySelector("[data-feedback-log]");
        var status = dialog.querySelector("[data-feedback-status]");
        var submit = dialog.querySelector("[data-feedback-submit]");
        var serverLines = [];

        function refreshLog() {
            logBox.value = buildLog(serverLines);
        }

        function open(kind) {
            form.reset();
            status.textContent = "";
            submit.disabled = false;
            if (kind) form.elements.kind.value = kind;
            refreshLog();
            if (!dialog.open) dialog.showModal();
            fetch("/feedback/log", { headers: { Accept: "application/json" } })
                .then(function (r) { return r.ok ? r.json() : { lines: [] }; })
                .then(function (data) { serverLines = data.lines || []; refreshLog(); })
                .catch(function () {});
            form.elements.message.focus();
        }

        document.querySelectorAll("[data-feedback-open]").forEach(function (btn) {
            btn.addEventListener("click", function (ev) {
                ev.preventDefault();
                open(btn.getAttribute("data-feedback-open"));
            });
        });
        dialog.querySelectorAll("[data-feedback-close]").forEach(function (btn) {
            btn.addEventListener("click", function () { dialog.close(); });
        });
        dialog.addEventListener("click", function (ev) { if (ev.target === dialog) dialog.close(); });
        dialog.querySelector("[data-feedback-download]").addEventListener("click", function () {
            refreshLog();
            download(logBox.value);
        });

        form.addEventListener("submit", function (ev) {
            ev.preventDefault();
            var data = new FormData(form);
            data.set("page_url", location.href);
            if (!form.elements.attach_log.checked) data.delete("log_text");
            data.delete("attach_log");
            submit.disabled = true;
            status.textContent = "Отправляю…";
            fetch("/feedback", { method: "POST", body: data, headers: { Accept: "application/json" } })
                .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Сервер ответил " + r.status }; }); })
                .then(function (res) {
                    if (res.ok) {
                        status.innerHTML = "Спасибо! Обращение №" + res.id + " отправлено. Ответ придёт в личку Discord и появится на странице <a href=\"/feedback\">«Мои обращения»</a>.";
                        form.elements.message.value = "";
                    } else {
                        status.textContent = res.error || "Не удалось отправить.";
                        submit.disabled = false;
                    }
                })
                .catch(function () {
                    status.textContent = "Нет связи с сервером — скачайте журнал и пришлите его в Discord.";
                    submit.disabled = false;
                });
        });
    });
})();
