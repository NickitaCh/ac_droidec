# ac_droidec

Проверки исправлений без обращений к Discord, Telegram и Comlink:

```sh
python -X utf8 -m unittest discover -s tests -v
node scripts/check_dashboard.cjs
```

Python-проверки используют временные базы SQLite; нужны зависимости проекта. Проверка JavaScript использует только встроенные модули Node.js.
