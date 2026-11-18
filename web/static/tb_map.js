(() => {
  const phase = document.getElementById('tb-phase');
  const atlas = document.getElementById('atlas');
  const locations = [...(atlas?.querySelectorAll('.tb-map-location') ?? [])];
  const centerInAtlas = location => {
    if (location) atlas.scrollLeft = location.offsetLeft - atlas.clientWidth / 2;
  };
  centerInAtlas(locations.find(location => location.querySelector('[aria-current="page"]')));
  phase?.addEventListener('change', () => {
    atlas.querySelectorAll('[data-phase]').forEach(location => {
      location.hidden = phase.value !== 'all' && location.dataset.phase !== phase.value;
    });
    centerInAtlas(locations.find(location => !location.hidden));
  });
  const links = [...document.querySelectorAll('.tb-mission-nav a')];
  const mapLinks = [...document.querySelectorAll('[data-map-mission]')];
  const panels = [...document.querySelectorAll('.tb-mission')];
  const initialMission = links.find(link => link.hasAttribute('aria-current'))?.hash;
  const dialog = document.getElementById('tb-mission-dialog');
  const dialogContent = dialog?.querySelector('.tb-mission-dialog-content');
  const dialogTitle = document.getElementById('tb-mission-dialog-title');
  let dialogPanel;
  let panelPosition;

  const positionDialog = () => {
    const frameTop = window.frameElement?.getBoundingClientRect().top ?? 0;
    const top = Math.max(0, -frameTop);
    const bottom = Math.min(innerHeight, window.parent.innerHeight - frameTop);
    dialog.style.setProperty('--dialog-y', (top + bottom) / 2 + 'px');
    dialog.style.setProperty('--dialog-height', Math.max(100, bottom - top - 24) + 'px');
  };

  const openMission = target => {
    const selected = links.find(link => link.hash === target);
    if (!selected) return;
    [...links, ...mapLinks].forEach(link => {
      if (link.hash === selected.hash) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
    panels.forEach(panel => {
      const active = '#' + panel.id === selected.hash;
      panel.hidden = !active;
      if (active) panel.open = true;
    });
  };

  if (dialog && typeof dialog.showModal === 'function') {
    panels.forEach(panel => { panel.hidden = true; });
    const openDialog = target => {
      const panel = panels.find(item => '#' + item.id === target);
      if (!panel) return;
      [...links, ...mapLinks].forEach(link => {
        if (link.hash === target) link.setAttribute('aria-current', 'true');
        else link.removeAttribute('aria-current');
      });
      panelPosition = document.createComment('mission editor');
      panel.before(panelPosition);
      dialogPanel = panel;
      dialogTitle.textContent = panel.querySelector('summary').firstChild.textContent.trim();
      dialogContent.append(panel);
      panel.open = true;
      panel.hidden = false;
      positionDialog();
      dialog.showModal();
      dialog.scrollTop = 0;
    };
    [...links, ...mapLinks].forEach(link => link.addEventListener('click', event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openDialog(link.hash);
    }));
    dialog.querySelector('.tb-mission-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      dialogPanel.hidden = true;
      panelPosition.replaceWith(dialogPanel);
      [...links, ...mapLinks].forEach(link => link.removeAttribute('aria-current'));
      const url = new URL(location.href);
      url.searchParams.delete('mission');
      url.searchParams.delete('saved');
      if (url.hash.startsWith('#mission-')) url.hash = 'mission-map';
      history.replaceState(null, '', url);
    });
    window.parent.addEventListener('scroll', () => { if (dialog.open) positionDialog(); }, { passive: true });
    window.parent.addEventListener('resize', () => { if (dialog.open) positionDialog(); });
    if (location.hash.startsWith('#mission-') && location.hash !== '#mission-map') openDialog(location.hash);
    else if (initialMission !== '#mission-general' || new URLSearchParams(location.search).has('saved')) openDialog(initialMission);
  } else {
    window.addEventListener('hashchange', () => {
      if (location.hash !== '#mission-map') openMission(location.hash.startsWith('#mission-') ? location.hash : initialMission);
    });
    [...links, ...mapLinks].forEach(link => link.addEventListener('click', () => openMission(link.hash)));
    openMission(links.some(link => link.hash === location.hash) ? location.hash : initialMission);
  }
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
  const fields = [...document.querySelectorAll('.tb-note-form textarea')];
  const changed = new Set();
  const protectDrafts = event => {
    event.preventDefault();
    event.returnValue = '';
  };
  fields.forEach(field => {
    const initial = field.value;
    field.addEventListener('input', () => {
      field.setCustomValidity('');
      if (field.value === initial) changed.delete(field);
      else changed.add(field);
      if (changed.size) window.addEventListener('beforeunload', protectDrafts);
      else window.removeEventListener('beforeunload', protectDrafts);
    });
    field.form.addEventListener('submit', event => {
      if ([...changed].some(draft => draft !== field)
          && !window.confirm('В другой миссии есть несохранённая памятка. Продолжить сохранение?')) {
        event.preventDefault();
        return;
      }
      window.removeEventListener('beforeunload', protectDrafts);
    });
  });
})();
