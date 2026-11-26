(() => {
  'use strict';
  // Commands verified against the PROD Discord REST schema and Telegram handlers.
  // Images: guild_units_image.render_player / toon_image.render, demo.db only.
  // Text excerpts: _stats_player, _tasks_player, build_player_check (demo guild 900).
  const examples = {
    ready: {
      discord: '/готовность цель: Лорд Вейдер', telegram: '/ready Лорд Вейдер',
      title: 'Готовность · Лорд Вейдер',
      body: "<a class=\"image-link\" href=\"/static/img/promo-ready.png\" target=\"_blank\" rel=\"noopener noreferrer\" aria-label=\"Открыть пример отчёта целиком\"><img class=\"bot-image\" src=\"/static/img/promo-ready.png\" alt=\"Готовность демо-игрока к Лорду Вейдеру: 14 из 15 требований выполнено\" loading=\"lazy\"></a><p class=\"image-caption\">Настоящий формат отчёта. Нажмите на картинку, чтобы рассмотреть.</p>",
      buttons: [],
      note: 'Личная готовность — после регистрации по коду союзника. Проверка других игроков и всей гильдии — офицерам.'
    },
    unit: {
      discord: '/юнит юнит: Дарт Вейдер', telegram: '/unit Дарт Вейдер',
      title: 'Дарт Вейдер · 7★ · R7',
      body: "<a class=\"image-link\" href=\"/static/img/promo-unit.png\" target=\"_blank\" rel=\"noopener noreferrer\" aria-label=\"Открыть пример отчёта целиком\"><img class=\"bot-image\" src=\"/static/img/promo-unit.png\" alt=\"Карточка Дарта Вейдера: статы, шесть модулей и способности\" loading=\"lazy\"></a><p class=\"image-caption\">Настоящий формат отчёта. Нажмите на картинку, чтобы рассмотреть.</p>",
      buttons: [],
      note: 'Карточка персонажа доступна после регистрации. Можно смотреть статы, модули, способности и сравнивать персонажей.'
    },
    stats: {
      discord: '/статы плейт: DEMO_GL', telegram: '/stats DEMO_GL',
      title: "📋 DEMO_GL · Демо-игрок · как есть",
      body: "<div class=\"plain-lines\"><b>Обязательные статы</b><br>❌ <b>Рей</b> — Основа: Стрела: Атака (нужно Стрела: Скорость)<br>❌ <b>Император Вечных ситхов</b> — Основа: Стрела: Здоровье (нужно Стрела: Скорость)<br><br><b>Персонажи</b> (5)<br>❌ Рей — 4/6<br>❌ Император Вечных ситхов — 3/5<br>✅ Верховный лидер Кайло Рен — 4/4<br>✅ Мастер-джедай Кеноби — 5/5<br>✅ Лорд Вейдер — 0/0</div>",
      buttons: [],
      note: 'Участник проверяет свои аккаунты по плейтам подключённой гильдии. Офицер задаёт требования на сайте и проверяет всю гильдию.'
    },
    dc: {
      discord: '/дк_требования проверить сезон: Долг и непокорность', telegram: '/dc',
      title: "📋 Датакроны · Демо-игрок · Долг и непокорность",
      body: "<div class=\"plain-lines\"><b>🔴 Обязательно — 0/4 закрыто</b><br><b>❌ Нейтральный адепт Силы · +5% к обороне за баффы</b><br><b>3 ур.:</b> Темная сторона: Призывы<br><b>6 ур.:</b> Нейтральный адепт Силы: +5% к обороне за баффы<br><b>9 ур.:</b> Тарон Маликос: Невосприимчивость к страху и потрясению, настак \"Грани безумия\" и т.д.<br><br><i>Фрагмент отчёта</i></div>",
      buttons: [],
      note: 'В Discord сезон выбирается в параметре «сезон». В Telegram /dc предложит выбор, если у гильдии есть требования к нескольким сезонам. Здесь показан фрагмент проверки на демо-данных.'
    },
    tasks: {
      discord: '/задачи отчёт', telegram: '/tasks',
      title: "📋 Задачи — Демо-игрок",
      body: "<div class=\"plain-lines\">🔵 <b>Капитан Тарпалс</b> — ♦️ Реликвия 9 (Назначено) · 6 → 6 · до 2026-11-04</div>",
      buttons: [],
      note: 'Участник видит задачи своих аккаунтов. Офицер ставит задачи на сайте и следит за прогрессом гильдии.'
    }
  };
  let current = 'ready';
  let platform = 'discord';
  const content = document.getElementById('example-content');
  function render() {
    const example = examples[current];
    document.getElementById('chat').className = 'chat ' + platform;
    document.getElementById('chat-symbol').textContent = platform === 'discord' ? '#' : '↗';
    document.getElementById('chat-title').textContent = platform === 'discord' ? 'помощник-гильдии' : 'Дроидец';
    document.getElementById('chat-subtitle').textContent = 'Пример команды в ' + (platform === 'discord' ? 'Discord' : 'Telegram');
    // Every value comes from the fixed local examples above, never user input.
    content.innerHTML = '<div class="message"><div class="avatar" aria-hidden="true">И</div><div class="message-body"><div class="author">Игрок <time>сегодня</time></div><code class="command"></code></div></div>' +
      '<div class="message"><div class="avatar bot" aria-hidden="true">Д</div><div class="message-body"><div class="author">Дроидец <span class="bot-badge">БОТ</span></div><div class="reply"><h3></h3>' + example.body + '</div><div class="mock-buttons" aria-label="Иллюстрация элементов ответа, неактивные кнопки">' + example.buttons.map(label => '<span>' + label + '</span>').join('') + '</div></div></div>';
    content.querySelector('.command').textContent = example[platform];
    content.querySelector('.reply h3').textContent = example.title;
    document.getElementById('access-note').textContent = example.note;
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
