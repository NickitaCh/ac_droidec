# ac_droidec

Установка и проверки используют Python 3.11 и зависимости из `requirements.txt`.

```sh
python -m pip install -r requirements.txt
```

Чтобы обновить зависимости, измените `requirements.in` и пересоберите список:

```sh
uv pip compile requirements.in --python-version 3.11 --universal -o requirements.txt
```

Веб по умолчанию запускается в рабочем режиме с защищённой cookie сессии. Сохраняйте `WEB_SESSION_SECRET` между перезапусками. Для местного запуска по HTTP задайте `WEB_ENV=development`.

Проверки исправлений без обращений к Discord, Telegram и Comlink:

```sh
python -X utf8 -m unittest discover -s tests -v
node scripts/check_dashboard.cjs
```

Python-проверки используют временные базы SQLite; нужны зависимости проекта. Проверка JavaScript использует только встроенные модули Node.js.
