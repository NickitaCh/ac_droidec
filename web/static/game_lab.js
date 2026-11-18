(() => {
  const root = document.querySelector('.game-lab');
  if (!root) return;
  const bank = root.querySelector('[data-mod-inventory]');
  if (bank?.querySelector('[data-mod-row]')) {
    const filters = ['location', 'set', 'slot', 'primary'].map(
      name => [name, bank.querySelector(`[data-mod-${name}]`)]
    );
    const secondary = bank.querySelector('[data-mod-secondary]');
    const minimum = bank.querySelector('[data-mod-min]');
    const count = bank.querySelector('[data-mod-count]');
    const rows = [...bank.querySelectorAll('[data-mod-row]')].map(
      row => [row, JSON.parse(row.dataset.secondaries)]
    );
    const exporter = bank.querySelector('[data-mod-export]');
    const exportStatus = bank.querySelector('[data-mod-export-status]');
    for (const button of exporter.querySelectorAll('[data-mod-download]')) {
      button.addEventListener('click', () => {
        const selected = rows.filter(([row]) => button.dataset.modDownload === 'all' || !row.hidden);
        const available = selected.map(([row]) => JSON.parse(row.dataset.csv)).filter(Boolean);
        if (!available.length) {
          exportStatus.textContent = 'Нет модулей, которые можно выгрузить.';
          return;
        }
        const columns = JSON.parse(exporter.dataset.columns);
        const csv = [columns, ...available].map(row =>
          row.map(value => '"' + String(value ?? '').replaceAll('"', '""') + '"').join(',')
        ).join('\r\n');
        const url = URL.createObjectURL(new Blob(['\uFEFF', csv], {type: 'text/csv;charset=utf-8'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'swgoh-mods.csv';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        const skipped = selected.length - available.length;
        exportStatus.textContent = skipped ? 'Пропущено неполных записей — ' + skipped : '';
      });
    }
    bank.addEventListener('input', () => {
      minimum.disabled = !secondary.value;
      let matches = 0;
      for (const [row, stats] of rows) {
        const basic = filters.every(([key, control]) => !control.value || row.dataset[key] === control.value);
        const extra = !secondary.value || stats.some(stat =>
          String(stat.stat_id) === secondary.value && (!minimum.value || stat.value >= Number(minimum.value))
        );
        row.hidden = !(basic && extra);
        if (!row.hidden) matches++;
      }
      count.textContent = matches ? 'Найдено — ' + matches : 'По этим фильтрам модулей нет.';
    });
  }
  const search = root.querySelector('#lab-search');
  const cards = [...root.querySelectorAll('[data-lab-state]')];
  let mode = 'all';
  function update() {
    const query = search.value.trim().toLocaleLowerCase('ru');
    let count = 0;
    for (const card of cards) {
      const write = card.dataset.labState === 'write';
      card.hidden = !((mode === 'all' || (mode === 'write' ? write : !write)) && card.textContent.toLocaleLowerCase('ru').includes(query));
      if (!card.hidden) count++;
    }
    for (const section of root.querySelectorAll('.lab-group')) section.hidden = ![...section.querySelectorAll('[data-lab-state]')].some(c => !c.hidden);
    root.querySelector('#lab-count').textContent = 'Показано: ' + count;
  }
  search.addEventListener('input', update);
  for (const button of root.querySelectorAll('[data-lab-filter]')) button.addEventListener('click', () => {
    mode = button.dataset.labFilter;
    for (const peer of root.querySelectorAll('[data-lab-filter]')) peer.setAttribute('aria-pressed', String(peer === button));
    update();
  });
})();
