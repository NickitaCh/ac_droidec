(() => {
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
