(() => {
  const root = document.querySelector('.game-lab');
  if (!root) return;
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
