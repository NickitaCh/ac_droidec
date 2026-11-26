(() => {
  'use strict';
  // Commands verified against the PROD Discord REST schema and Telegram handlers.
  // Images: guild_units_image.render_player / toon_image.render, demo.db only.
  // Text excerpts: _stats_player, _tasks_player, build_player_check (demo guild 900).
  const examples = {
    ready: {
      discord: '/готовность цель: Лорд Вейдер', telegram: '/ready Лорд Вейдер',
      title: 'Готовность · Лорд Вейдер',
      body: "<a class=\"image-link\" href=\"/static/img/promo-ready.png?v=2\" target=\"_blank\" rel=\"noopener noreferrer\" aria-label=\"Открыть пример отчёта целиком\"><img class=\"bot-image\" src=\"/static/img/promo-ready.png?v=2\" alt=\"Отчёт о готовности демо-игрока к Лорду Вейдеру\" loading=\"lazy\"></a><p class=\"image-caption\">Настоящий формат отчёта. Нажмите на картинку, чтобы рассмотреть.</p>",
      buttons: [],
      note: 'Личная готовность — после регистрации по коду союзника. Проверка других игроков и всей гильдии — офицерам.'
    },
    unit: {
      discord: '/юнит юнит: Дарт Вейдер', telegram: '/unit Дарт Вейдер',
      title: 'Дарт Вейдер · 7★ · R7',
      body: "<a class=\"image-link\" href=\"/static/img/promo-unit.png?v=2\" target=\"_blank\" rel=\"noopener noreferrer\" aria-label=\"Открыть пример отчёта целиком\"><img class=\"bot-image\" src=\"/static/img/promo-unit.png?v=2\" alt=\"Карточка Дарта Вейдера: статы, шесть модулей и способности\" loading=\"lazy\"></a><p class=\"image-caption\">Настоящий формат отчёта. Нажмите на картинку, чтобы рассмотреть.</p>",
      buttons: [],
      note: 'Карточка персонажа доступна после регистрации. Можно смотреть статы, модули, способности и сравнивать персонажей.'
    },
    stats: {
      discord: '/статы плейт: DEMO_GL', telegram: '/stats DEMO_GL',
      title: "📋 DEMO_GL · Демо-игрок · как есть",
      body: "<div class=\"plain-lines\"><b>Обязательные статы</b><br>❌ <b>Император Вечных ситхов</b> — Основа: Стрела: Здоровье (нужно Стрела: Скорость)<br><br><b>Персонажи</b> (5)<br>❌ Император Вечных ситхов — 4/5<br>✅ Рей — 2/4<br>✅ Верховный лидер Кайло Рен — 4/4<br>✅ Мастер-джедай Кеноби — 4/4<br>✅ Лорд Вейдер — 0/0</div>",
      buttons: [],
      note: 'Участник проверяет свои аккаунты по плейтам подключённой гильдии. Офицер задаёт требования на сайте и проверяет всю гильдию.'
    },
    dc: {
      discord: '/дк_требования проверить сезон: Долг и непокорность', telegram: '/dc',
      title: "📋 Датакроны · Демо-игрок · Долг и непокорность",
      body: "<div class=\"plain-lines\"><b>🔴 Обязательно — 0/9 закрыто</b><br><b>❌ ТБ ×2</b><br><b>3 ур.:</b> Светлая сторона: Бонусная защита (75%, 50%, 25%) ИЛИ Светлая сторона: ШХ за бафы (5%)<br><b>6 ур.:</b> Джедай: с полным ХП/защитой в начале хода → +100% урона в этот ход. ИЛИ Джедай: +20% к атаке за предвидение (макс +100%)<br><b>9 ур.:</b> Лея (Обучение джедая): Нельзя атаковать Лею в начале боя; воскрешение союзников когда Лея погибает<br>📦 Подходящих ДК: 0/2<br><br><i>Фрагмент отчёта</i></div>",
      buttons: [],
      note: 'В Discord сезон выбирается в параметре «сезон». В Telegram /dc предложит выбор, если у гильдии есть требования к нескольким сезонам. Здесь показан фрагмент проверки на демо-данных.'
    },
    tasks: {
      discord: '/задачи отчёт', telegram: '/tasks',
      title: "📋 Задачи — Демо-игрок",
      body: "<div class=\"plain-lines\">🔵 <b>Бейлан Сколл</b> — ♦️ Реликвия 9 (Назначено) · 8 → 8 · до 2026-12-09<br>🟡 <b>Мастер-джедай Мейс Винду</b> — ♦️ Реликвия 10 (В работе) · 9 → 9 · до 2026-12-13<br>🟡 <b>Эзра Бриджер</b> — ♦️ Реликвия 8 (В работе) · 6 → 7 · до 2026-12-07</div>",
      buttons: [],
      note: 'Участник видит задачи своих аккаунтов. Офицер ставит задачи на сайте и следит за прогрессом гильдии.'
    }
  };
  let current = 'ready';
  let platform = 'discord';
  const content = document.getElementById('example-content');
  function render() {
    const example = examples[current];
    const image = current === 'ready' || current === 'unit';
    const body = example.body.replace(/<p class="image-caption">.*?<\/p>/g, '');
    const avatar = '<img src="/static/img/promo-avatar.png" alt="" width="40" height="40">';
    const chat = document.getElementById('chat');
    const header = document.getElementById('chat-header');
    const compose = document.getElementById('chat-compose');
    chat.className = 'chat ' + platform;
    if (platform === 'discord') {
      header.innerHTML = '<span class="dc-hash" aria-hidden="true">#</span><strong>помощник-гильдии</strong><span class="dc-header-icons" aria-hidden="true">⌕ &nbsp; ⋯</span>';
      content.innerHTML = '<div class="dc-invocation"><span class="dc-invocation-avatar" aria-hidden="true">И</span><span>Игрок использует <code class="command"></code></span></div>' +
        '<div class="dc-message"><div class="dc-avatar">' + avatar + '</div><div class="dc-message-main"><div class="dc-author">Дроидец <span class="dc-app">✓ ПРИЛОЖЕНИЕ</span><time>Сегодня, в 12:00</time></div>' +
        '<div class="dc-response ' + (image ? 'dc-attachment' : current === 'stats' ? 'dc-components' : 'dc-embed') + '">' +
        (image ? '' : '<h3 class="reply-title"></h3>') + body +
        (current === 'stats' ? '<div class="dc-select" aria-hidden="true">Выберите персонажа <span>⌄</span></div>' : '') + '</div></div></div>';
      compose.innerHTML = '<span class="dc-plus">+</span><span>Написать в #помощник-гильдии</span><span class="compose-end">☺</span>';
    } else {
      header.innerHTML = '<span class="tg-back" aria-hidden="true">‹</span><span class="tg-header-avatar">' + avatar + '</span><div><strong>Дроидец</strong><small>бот</small></div><span class="tg-menu" aria-hidden="true">⋮</span>';
      content.innerHTML = '<div class="tg-day">Сегодня</div><div class="tg-outgoing"><span class="command"></span><span class="tg-stamp">12:00 <span>✓✓</span></span></div>' +
        '<div class="tg-incoming ' + (image ? 'tg-photo' : '') + '">' + (image ? '' : '<h3 class="reply-title"></h3>') + body + '<span class="tg-stamp">12:00</span></div>';
      compose.innerHTML = '<span class="tg-clip" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 11.5 12 20.5a6 6 0 0 1-8.5-8.5l10-10a4 4 0 0 1 5.7 5.7L9.2 17.8a2 2 0 0 1-2.8-2.8L16 5.4"/></svg></span><span>Сообщение</span><span class="compose-end" aria-hidden="true">☺ <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></svg></span>';
    }
    content.querySelector('.command').textContent = example[platform];
    if (!image) content.querySelector('.reply-title').textContent = example.title;
    document.getElementById('access-note').textContent = example.note + (image ? ' Изображение можно открыть целиком нажатием.' : '');
  }
  document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => {
    current = button.dataset.example;
    document.querySelectorAll('[data-example]').forEach(item => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    render();
  }));
  document.querySelectorAll('[data-platform]').forEach(button => button.addEventListener('click', () => {
    platform = button.dataset.platform;
    document.querySelectorAll('[data-platform]').forEach(item => {
      const selected = item === button;
      item.classList.toggle('selected', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    render();
  }));
  render();
})();
