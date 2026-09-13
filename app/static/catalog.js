(() => {
  const search = document.getElementById('catalog-search');
  const statusFilter = document.getElementById('status-filter');
  const collectionFilter = document.getElementById('collection-filter');
  const selectedOnly = document.getElementById('selected-only');
  const selectVisible = document.getElementById('select-visible');
  const clearSelection = document.getElementById('clear-selection');
  const selectionCount = document.getElementById('selection-count');
  const selectionDuration = document.getElementById('selection-duration');
  const selectionBar = document.getElementById('selection-bar');
  const submitSelection = document.getElementById('analyse-selection');
  const form = document.getElementById('catalog-form');
  const selectionStatus = document.getElementById('selection-status');
  const catalogGrid = document.getElementById('catalog-grid');
  const cardsView = document.getElementById('catalog-view-cards');
  const listView = document.getElementById('catalog-view-list');
  const maxSelection = 50;
  if (!search || !statusFilter || !collectionFilter || !selectedOnly || !selectVisible
      || !clearSelection || !selectionCount || !selectionDuration || !selectionBar
      || !submitSelection || !form || !selectionStatus || !catalogGrid || !cardsView
      || !listView) return;
  const rows = [...document.querySelectorAll('[data-video-row]')];
  const selects = [...document.querySelectorAll('[data-video-select]')];
  const viewStorageKey = 'bunny-video-report:catalog-view';

  const setView = (view, persist = false) => {
    const selectedView = view === 'list' ? 'list' : 'cards';
    catalogGrid.dataset.view = selectedView;
    cardsView.setAttribute('aria-pressed', String(selectedView === 'cards'));
    listView.setAttribute('aria-pressed', String(selectedView === 'list'));
    if (persist) {
      try { localStorage.setItem(viewStorageKey, selectedView); } catch {}
    }
  };
  let storedView = 'cards';
  try { storedView = localStorage.getItem(viewStorageKey) || 'cards'; } catch {}
  setView(storedView);
  cardsView.addEventListener('click', () => setView('cards', true));
  listView.addEventListener('click', () => setView('list', true));

  const normalize = value => String(value || '').trim().toLocaleLowerCase('it');
  const duration = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const formatDuration = seconds => {
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`
      : `${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`;
  };
  const rowMatches = (row, isSelected) => {
    const query = normalize(search.value);
    const searchable = `${row.dataset.title || ''} ${row.dataset.description || ''}`;
    return (!query || normalize(searchable).includes(query))
      && (statusFilter.value === 'all' || row.dataset.status === statusFilter.value)
      && (collectionFilter.value === 'all' || row.dataset.collection === collectionFilter.value)
      && (!selectedOnly.checked || isSelected);
  };
  const selectedDuration = () => selects.reduce(
    (total, select, index) => total + (select.checked ? duration(rows[index]?.dataset.duration) : 0), 0,
  );
  const countSelected = () => selects.filter(select => select.checked).length;
  const update = () => {
    rows.forEach((row, index) => {
      row.hidden = !rowMatches(row, selects[index]?.checked);
    });
    const count = countSelected();
    selectionCount.textContent = String(count);
    selectionDuration.textContent = formatDuration(selectedDuration());
    selectionBar.hidden = count === 0;
    submitSelection.disabled = count === 0 || count > maxSelection;
    selectionStatus.textContent = count > maxSelection
      ? 'Seleziona al massimo 50 video prima di continuare.'
      : count === maxSelection
        ? 'Limite di 50 video raggiunto. Deseleziona un video per sceglierne un altro.'
        : `Puoi selezionare ancora ${maxSelection - count} video (massimo 50).`;
  };

  search.addEventListener('input', update);
  statusFilter.addEventListener('change', update);
  collectionFilter.addEventListener('change', update);
  selectedOnly.addEventListener('change', update);
  selects.forEach(select => select.addEventListener('change', () => {
    if (select.checked && countSelected() > maxSelection) select.checked = false;
    update();
  }));
  selectVisible.addEventListener('click', () => {
    let remaining = Math.max(0, maxSelection - countSelected());
    rows.forEach((row, index) => {
      if (!row.hidden && selects[index] && !selects[index].checked && remaining > 0) {
        selects[index].checked = true;
        remaining -= 1;
      }
    });
    update();
  });
  clearSelection.addEventListener('click', () => {
    selects.forEach(select => { select.checked = false; });
    update();
  });
  form.addEventListener('submit', event => {
    const count = countSelected();
    if (count === 0 || count > maxSelection) {
      event.preventDefault();
      update();
      if (count === 0) selectionStatus.textContent = 'Seleziona almeno un video per continuare (massimo 50).';
    }
  });

  update();
})();
