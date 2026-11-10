(() => {
  const phase = document.getElementById('tb-phase');
  const atlas = document.querySelector('.tb-atlas');
  const filter = () => {
    document.querySelectorAll('.tb-stage').forEach(stage => {
      stage.hidden = phase.value !== 'all' && stage.dataset.phase !== phase.value;
    });
    atlas.classList.toggle('tb-one-stage', phase.value !== 'all');
  };
  if (phase && atlas) {
    if (window.matchMedia('(max-width: 550px)').matches) phase.value = phase.dataset.current;
    phase.addEventListener('change', filter);
    filter();
  }
  const openMission = () => {
    const panel = document.getElementById(location.hash.slice(1));
    if (panel && panel.classList.contains('tb-mission')) panel.open = true;
  };
  window.addEventListener('hashchange', openMission);
  document.querySelectorAll('.tb-mission-nav a').forEach(link => link.addEventListener('click', () => {
    const panel = document.getElementById(link.hash.slice(1));
    if (panel) panel.open = true;
  }));
  document.querySelectorAll('.tb-copy-squad').forEach(button => button.addEventListener('click', () => {
    const field = document.getElementById(button.dataset.target);
    const pack = button.dataset.pack;
    if (!field || !pack) return;
    const value = field.value.trim();
    const next = value ? value + '\n\n' + pack : pack;
    if (next.length > field.maxLength) {
      field.setCustomValidity('Для этого пака не хватает места. Сократите памятку.');
      field.reportValidity();
      return;
    }
    field.setCustomValidity('');
    field.value = next;
    field.dispatchEvent(new Event('input', {bubbles: true}));
    field.focus();
    field.scrollIntoView({block: 'center', behavior: 'smooth'});
  }));
  document.querySelectorAll('.tb-note-form textarea').forEach(field => field.addEventListener('input', () => field.setCustomValidity('')));
  openMission();
})();
