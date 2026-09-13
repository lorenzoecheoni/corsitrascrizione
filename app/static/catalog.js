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
  const tabButtons = [...document.querySelectorAll('[data-catalog-tab-button]')];
  const maxSelection = 50;
  if (!search || !statusFilter || !collectionFilter || !selectedOnly || !selectVisible
      || !clearSelection || !selectionCount || !selectionDuration || !selectionBar
      || !submitSelection || !form || !selectionStatus || !catalogGrid || !cardsView
      || !listView) return;
  const rows = [...document.querySelectorAll('[data-video-row]')];
  const selects = [...document.querySelectorAll('[data-video-select]')];
  const viewStorageKey = 'bunny-video-report:catalog-view';
  let activeTab = tabButtons.find(button => button.getAttribute('aria-pressed') === 'true')
    ?.dataset.catalogTabButton || tabButtons[0]?.dataset.catalogTabButton || null;

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
  const selectKey = (select, index) => select.value || `catalog-position-${index}`;
  const selectedIds = () => new Set(selects.flatMap(
    (select, index) => select.checked ? [selectKey(select, index)] : [],
  ));
  const syncSelection = (key, checked) => {
    selects.forEach((select, index) => {
      if (selectKey(select, index) === key) select.checked = checked;
    });
  };
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
    return (!activeTab || row.dataset.catalogTab === activeTab)
      && (!query || normalize(searchable).includes(query))
      && (statusFilter.value === 'all' || row.dataset.status === statusFilter.value)
      && (collectionFilter.value === 'all' || row.dataset.collection === collectionFilter.value)
      && (!selectedOnly.checked || isSelected);
  };
  const selectedDuration = () => {
    const seen = new Set();
    return selects.reduce((total, select, index) => {
      const key = selectKey(select, index);
      if (!select.checked || seen.has(key)) return total;
      seen.add(key);
      return total + duration(rows[index]?.dataset.duration);
    }, 0);
  };
  const countSelected = () => selectedIds().size;
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
  tabButtons.forEach(button => button.addEventListener('click', () => {
    activeTab = button.dataset.catalogTabButton;
    tabButtons.forEach(candidate => candidate.setAttribute(
      'aria-pressed', String(candidate === button),
    ));
    update();
  }));
  selects.forEach((select, index) => select.addEventListener('change', () => {
    const key = selectKey(select, index);
    const wasAlreadySelected = selects.some(
      (candidate, candidateIndex) => candidate !== select
        && selectKey(candidate, candidateIndex) === key && candidate.checked,
    );
    if (select.checked && !wasAlreadySelected && countSelected() > maxSelection) {
      select.checked = false;
    } else {
      syncSelection(key, select.checked);
    }
    update();
  }));
  selectVisible.addEventListener('click', () => {
    const selected = selectedIds();
    let remaining = Math.max(0, maxSelection - selected.size);
    rows.forEach((row, index) => {
      const select = selects[index];
      if (!row.hidden && select && !select.checked && remaining > 0) {
        const key = selectKey(select, index);
        syncSelection(key, true);
        if (selected.has(key)) return;
        selected.add(key);
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
      return;
    }
    const submitted = new Set();
    selects.forEach((select, index) => {
      const key = selectKey(select, index);
      select.disabled = !select.checked || submitted.has(key);
      if (select.checked) submitted.add(key);
    });
  });

  update();
})();
