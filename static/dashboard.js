(function(){

let currentSort = { column: '', order: '' };
let starFilterActive = window.localStorage.getItem('scriptrr.starFilterActive') === 'true';

function updateStarFilterButton() {
  const button = document.querySelector('[data-star-filter]');
  if (!button) return;
  button.classList.toggle('text-amber-400', starFilterActive);
  button.classList.toggle('text-gray-500', !starFilterActive);
  button.title = starFilterActive ? 'Show all scripts' : 'Show starred scripts';
  button.setAttribute('aria-label', button.title);
}

function applyStarFilter() {
  document.querySelectorAll('tr[data-script-group]').forEach(function(row) {
    const shouldHide = starFilterActive && row.dataset.starred !== 'true';
    row.hidden = shouldHide;
    row.style.display = shouldHide ? 'none' : '';
  });
  updateStarFilterButton();
}

document.body.addEventListener('htmx:configRequest', function(evt) {
  if (evt.detail.path.includes('/v2/api/scripts?sort=')) {
    const url = new URL(evt.detail.path, window.location.origin);
    const sortCol = url.searchParams.get('sort');
    
    if (currentSort.column === sortCol) {
      currentSort.order = currentSort.order === 'asc' ? 'desc' : 'asc';
    } else {
      currentSort.column = sortCol;
      currentSort.order = (sortCol === 'last_run') ? 'desc' : 'asc';
    }
    evt.detail.path = url.pathname + '?sort=' + sortCol + '&order=' + currentSort.order;
  }
});

function restoreActionButtons(el) {
  if (!el || !el.matches || (!el.matches('button[hx-post^="/v2/api/run/"]') && !el.matches('button[hx-post^="/v2/api/stop/"]'))) return;
  window.setTimeout(function() {
    if (el.dataset.originalText) el.innerHTML = el.dataset.originalText;
    refreshIcons(el.parentNode || document);
  }, 900);
}

function openScheduleModal(name, url, currentSchedule) {
  document.getElementById('schedule-script-name').textContent = 'Configuring: ' + name;
  document.getElementById('schedule-input').value = currentSchedule || '';
  
  const form = document.getElementById('schedule-form');
  form.setAttribute('hx-post', '/v2/api/script/' + url + '/save-schedule');
  
  // Re-bind HTMX to the form since the target path was injected dynamically
  if (window.htmx) htmx.process(form);
  
  document.getElementById('schedule-modal').classList.remove('hidden');
}

function closeScheduleModal() {
  document.getElementById('schedule-modal').classList.add('hidden');
}

/**
 * Populates and opens the Edit Script modal with the given script data.
 * @param {Object} script - The script object with properties: name, description, tags, content.
 */
function openEditScriptModal(script) {
  // Set modal fields
  if (script.name !== undefined) {
    const nameInput = document.getElementById('edit-name');
    if (nameInput) nameInput.value = script.name;
  }
  if (script.description !== undefined) {
    const descInput = document.getElementById('edit-description');
    if (descInput) descInput.value = script.description;
  }
  if (script.tags !== undefined) {
    // script.tags currently comes in as comma-separated string (from data-edit-script)
    const tagsInput = document.getElementById('edit-tags');
    const availableContainer = document.getElementById('edit-available-tags');
    const appliedContainer = document.getElementById('edit-applied-tags');
    const newTagInput = document.getElementById('edit-new-tag');
    const addBtn = document.getElementById('edit-add-tag-btn');

    // Helper to create a chip/button element
    function makeTagChip(name, cls = 'bg-blue-950/40 text-blue-400') {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = cls + ' text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded';
      el.textContent = name;
      el.setAttribute('data-tag-name', name);
      return el;
    }

    // parse incoming tags (could be string or array)
    let applied = [];
    if (Array.isArray(script.tags)) applied = script.tags.slice();
    else if (typeof script.tags === 'string' && script.tags.trim() !== '') applied = script.tags.split(',').map(t => t.trim()).filter(Boolean);

    // Build available tags list from existing tag spans on the page
    let available = [];
    document.querySelectorAll('[data-tag-name]').forEach(function(el){
      const name = el.getAttribute('data-tag-name');
      if (name && !available.includes(name)) available.push(name);
    });

    // Merge applied tags into available list
    applied.forEach(t => { if (!available.includes(t)) available.push(t); });

    // Render available and applied containers
    function renderLists() {
      availableContainer.innerHTML = '';
      appliedContainer.innerHTML = '';

      available.forEach(function(name){
        // Applied tags already render in the applied list below; don't duplicate.
        if (applied.includes(name)) return;
        const chip = makeTagChip(name);
        chip.addEventListener('click', function(){
          // toggle applied
          if (!applied.includes(name)) applied.push(name);
          else applied = applied.filter(t => t !== name);
          renderLists();
        });
        availableContainer.appendChild(chip);
      });

      applied.forEach(function(name){
        const chip = makeTagChip(name, 'bg-emerald-600/10 text-emerald-400 border border-emerald-500/20');
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'ml-1 text-xs text-gray-400 hover:text-rose-400 transition';
        remove.title = 'Remove tag';
        remove.textContent = '×';
        remove.addEventListener('click', function(){ applied = applied.filter(t => t !== name); renderLists(); });
        const wrapper = document.createElement('span');
        wrapper.className = 'inline-flex items-center gap-1';
        wrapper.appendChild(chip);
        wrapper.appendChild(remove);
        appliedContainer.appendChild(wrapper);
      });

      // update hidden input value
      if (tagsInput) tagsInput.value = applied.join(', ');
    }

    renderLists();

    // Add new tag handler
    if (addBtn && newTagInput) {
      addBtn.onclick = function(){
        const val = (newTagInput.value || '').trim();
        if (!val) return;
        if (!available.includes(val)) available.push(val);
        if (!applied.includes(val)) applied.push(val);
        newTagInput.value = '';
        renderLists();
      };
    }
    // expose small helper for testing
    window.__scriptrr_tags = { available, applied };
  }
  // Set up form submission endpoint and target so HTMX or the fallback JS can submit edits
  const form = document.getElementById('edit-script-form');
  if (form && script.name) {
    const enc = encodeURIComponent(script.name);
    form.setAttribute('hx-post', '/v2/api/script/' + enc + '/save');
    form.setAttribute('hx-target', '#script-rows');
    form.setAttribute('hx-on', "htmx:afterRequest: document.getElementById('edit-script-modal').classList.add('hidden');");

    // If HTMX is available, process the form so HTMX will pick up the newly added attributes.
    // Otherwise, ensure our non-HTMX wiring picks up the form by calling wireDashboard on it.
    if (window.htmx) {
      htmx.process(form);
    } else {
      // wireDashboard will attach the fallback submit handler for forms with hx-post
      wireDashboard(form);
    }
    // Ensure hidden tags input is synced right before any submit (works for HTMX and fallback)
    form.addEventListener('submit', function(){
      try {
        const tagInput = form.querySelector('#edit-tags');
        if (!tagInput) return;
        const appliedContainer = form.querySelector('#edit-applied-tags');
        if (appliedContainer) {
          const tags = Array.from(appliedContainer.querySelectorAll('[data-tag-name]')).map(function(el){ return el.getAttribute('data-tag-name'); });
          tagInput.value = tags.join(', ');
        } else if (window.__scriptrr_tags && Array.isArray(window.__scriptrr_tags.applied)) {
          tagInput.value = window.__scriptrr_tags.applied.join(', ');
        }
      } catch (e) { /* no-op */ }
    });
  }
  if (script.content !== undefined) {
    const contentInput = document.getElementById('edit-content');
    if (contentInput) contentInput.value = script.content;
  }
  // Show the modal
  const modal = document.getElementById('edit-script-modal');
  if (modal) modal.classList.remove('hidden');
}

document.body.addEventListener('htmx:beforeRequest', function(evt) {
  const el = evt.detail && evt.detail.elt;
  if (el && el.matches && el.matches('button[hx-post^="/v2/api/run/"]')) {
    el.dataset.originalText = el.innerHTML;
    el.innerHTML = 'Starting';
    window.setTimeout(function() { if (el.innerHTML === 'Starting') restoreActionButtons(el); }, 5000);
  } else if (el && el.matches && el.matches('button[hx-post^="/v2/api/stop/"]')) {
    el.dataset.originalText = el.innerHTML;
    el.innerHTML = 'Stopping';
    window.setTimeout(function() { if (el.innerHTML === 'Stopping') restoreActionButtons(el); }, 5000);
  }
});

document.body.addEventListener('htmx:afterRequest', function(evt) {
  const el = evt.detail && evt.detail.elt;
  if (el && el.matches && el.matches('button[hx-post^="/v2/api/run/"]')) {
    const liveButtonId = el.getAttribute('data-open-live');
    const liveButton = document.getElementById(liveButtonId);
    if (liveButton) setTimeout(() => liveButton.click(), 100);
    restoreActionButtons(el);
  } else if (el && el.matches && el.matches('button[hx-post^="/v2/api/stop/"]')) {
    restoreActionButtons(el);
  }
});

document.body.addEventListener('htmx:responseError', function(evt) {
  const el = evt.detail && evt.detail.elt;
  if (el && el.matches && (el.matches('button[hx-post^="/v2/api/run/"]') || el.matches('button[hx-post^="/v2/api/stop/"]'))) {
    alert('Action failed: HTTP ' + evt.detail.xhr.status + ' ' + evt.detail.xhr.responseText);
    restoreActionButtons(el);
  }
});

document.body.addEventListener('htmx:sendError', function(evt) {
  const el = evt.detail && evt.detail.elt;
  if (el && el.matches && (el.matches('button[hx-post^="/v2/api/run/"]') || el.matches('button[hx-post^="/v2/api/stop/"]'))) {
    alert('Request failed before reaching the server.');
    restoreActionButtons(el);
  }
});

function refreshIcons(root) {
  if (window.lucide) window.lucide.createIcons();
}

let statusPollingStarted = false;
const liveStreams = {}; // key: script name, value: { source: EventSource, rowEl, consoleEl, stateEl }
async function pollStatuses() {
  try {
    const response = await fetch('/v2/api/statuses?_=' + Date.now());
    if (response.ok) {
      const statuses = await response.json();
      document.querySelectorAll('[data-status-name]').forEach(function(el) {
        const name = el.getAttribute('data-status-name');
        if (statuses[name]) {
          const prevText = el.textContent.trim();
          el.innerHTML = statuses[name];
          const newText = el.textContent.trim();
          
          if (prevText !== newText) {
            const tr = el.closest('tr');
            if (tr) {
              const actionContainer = tr.querySelector('[data-action-container]');
              if (actionContainer) {
                const nameUrl = encodeURIComponent(name);
                const loopIndex = tr.getAttribute('data-row-index');
                const includeAttr = loopIndex ? ` hx-include="#args-${loopIndex}" data-open-live="live-button-${loopIndex}"` : '';
                
                if (newText === 'RUNNING') {
                  actionContainer.innerHTML = `
                    <button disabled class="bg-blue-600/10 text-blue-400 border border-blue-500/20 opacity-60 cursor-not-allowed px-2 py-1 rounded text-sm font-medium flex items-center gap-1.5">
                      <i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin"></i> Running
                    </button>
                    <button class="bg-rose-600/10 text-rose-400 border border-rose-500/20 hover:bg-rose-600/30 px-2 py-1 rounded text-sm font-medium flex items-center gap-1.5 transition" hx-post="/v2/api/stop/${nameUrl}" hx-swap="none">
                      <i data-lucide="square" class="fill-current w-3.5 h-3.5"></i> Stop
                    </button>
                  `;
                  wireDashboard(actionContainer);
                  refreshIcons(actionContainer);
                  if (window.htmx) htmx.process(actionContainer);
                  // Auto-open live row and start streaming for this script
                  const liveRowId = `live-row-${loopIndex}`;
                  const liveRow = document.getElementById(liveRowId);
                  if (liveRow && liveRow.dataset.liveClosed !== 'true') {
                    // show the live row
                    if (liveRow.classList.contains('hidden')) {
                      liveRow.classList.remove('hidden');
                    }
                    // start stream if not already started
                    const nameKey = name;
                    if (!liveStreams[nameKey]) {
                      const consoleEl = document.getElementById(`live-console-${loopIndex}`);
                      const stateEl = document.getElementById(`live-state-${loopIndex}`);
                      const url = liveRow.getAttribute('data-live-url');
                      startLiveStream(nameKey, url, liveRow, consoleEl, stateEl, loopIndex);
                    }
                  }
                  } else {
                  const liveRow = document.getElementById(`live-row-${loopIndex}`);
                  if (liveRow) liveRow.dataset.liveClosed = 'false';
                  actionContainer.innerHTML = `
                    <button class="bg-blue-600/10 text-blue-400 border border-blue-500/20 hover:bg-blue-600/30 px-2 py-1 rounded text-sm font-medium flex items-center gap-1.5 transition" hx-post="/v2/api/run/${nameUrl}"${includeAttr} hx-swap="none">
                      <i data-lucide="play" class="fill-current w-3.5 h-3.5"></i> Run
                    </button>
                  `;
                  wireDashboard(actionContainer);
                  refreshIcons(actionContainer);
                  if (window.htmx) htmx.process(actionContainer);
                  
                    if (prevText === 'RUNNING') {
                    const liveRow = document.getElementById(`live-row-${loopIndex}`);
                    if (liveRow && !liveRow.classList.contains('hidden')) {
                      setTimeout(() => {
                        if (el.textContent.trim() !== 'RUNNING') {
                          liveRow.classList.add('hidden');
                          stopLiveStream(name);
                        }
                      }, 1500);
                    }
                  }
                }
              }
            }
          }
        }
      });
    }
  } catch (err) { console.error(err); }
  window.setTimeout(pollStatuses, 2000);
}

function wireDashboard(root) {
  const scope = root || document;
  if (!statusPollingStarted) {
    statusPollingStarted = true;
    pollStatuses();
  }

  if (!document.body.dataset.starFilterWired) {
    document.body.dataset.starFilterWired = '1';
    document.body.addEventListener('click', function(evt) {
      const button = evt.target.closest('[data-star-filter]');
      if (!button) return;
      evt.preventDefault();
      starFilterActive = !starFilterActive;
      window.localStorage.setItem('scriptrr.starFilterActive', String(starFilterActive));
      applyStarFilter();
    });

    document.body.addEventListener('htmx:afterSwap', applyStarFilter);
  }

  applyStarFilter();

  // Live streams are auto-managed; if rows are present we wire them for cleanup when HTMX re-renders
  // No click handlers — streams are started/stopped by pollStatuses

  if (!document.body.dataset.liveCloseWired) {
    document.body.dataset.liveCloseWired = '1';
    document.body.addEventListener('click', function(evt) {
      const button = evt.target.closest('[data-live-close]');
      if (!button) return;
      evt.preventDefault();
      evt.stopPropagation();
      closeLiveLog(button);
    });
  }

  scope.querySelectorAll('[data-log-button]:not([data-v3-log-wired])').forEach(function(button) {
    button.setAttribute('data-v3-log-wired', '1');
    button.addEventListener('click', async function(evt) {
      evt.preventDefault();
      const rowId = button.getAttribute('data-log-row');
      const consoleId = button.getAttribute('data-log-console');
      const url = button.getAttribute('data-log-url');
      const row = document.getElementById(rowId);
      const consoleEl = document.getElementById(consoleId);
      
       if (!row || !consoleEl) return;
       if (!row.classList.contains('hidden')) { row.classList.add('hidden'); return; }
       row.classList.remove('hidden');
      consoleEl.textContent = 'Loading log...\n';
      try {
        const res = await fetch(url);
        if (res.ok) { consoleEl.textContent = await res.text() || 'Log is empty.'; } 
        else { consoleEl.textContent = 'Failed to load log. HTTP ' + res.status; }
        consoleEl.scrollTop = consoleEl.scrollHeight;
      } catch(e) { consoleEl.textContent = 'Error loading log: ' + e; }
    });
  });

  // Wire viewer buttons for non-script files and logs
  scope.querySelectorAll('.view-file-btn:not([data-v3-file-wired])').forEach(function(btn){
    btn.setAttribute('data-v3-file-wired', '1');
    btn.addEventListener('click', async function(evt){
      evt.preventDefault();
      const url = btn.getAttribute('data-file-url');
      if (!url) return;
      const modal = document.getElementById('file-viewer-modal');
      const title = document.getElementById('file-viewer-title');
      const content = document.getElementById('file-viewer-content');
      if (!modal || !content) return;
      title.textContent = 'Loading...';
      content.textContent = 'Loading file...';
      modal.classList.remove('hidden');
      try {
        const res = await fetch(url + '?_=' + Date.now());
        if (res.ok) {
          const text = await res.text();
          title.textContent = url.split('/').pop();
          content.textContent = text || '(empty)';
        } else {
          title.textContent = 'Error';
          content.textContent = 'Failed to load file: HTTP ' + res.status;
        }
      } catch(e) {
        title.textContent = 'Error';
        content.textContent = 'Failed to load file: ' + e;
      }
    });
  });

  scope.querySelectorAll('.view-log-btn:not([data-v3-logview-wired])').forEach(function(btn){
    btn.setAttribute('data-v3-logview-wired', '1');
    btn.addEventListener('click', async function(evt){
      evt.preventDefault();
      const url = btn.getAttribute('url') || btn.getAttribute('data-log-url');
      if (!url) return;
      const modal = document.getElementById('file-viewer-modal');
      const title = document.getElementById('file-viewer-title');
      const content = document.getElementById('file-viewer-content');
      if (!modal || !content) return;
      title.textContent = 'Loading...';
      content.textContent = 'Loading log...';
      modal.classList.remove('hidden');
      try {
        const res = await fetch(url + '?_=' + Date.now());
        if (res.ok) {
          const text = await res.text();
          title.textContent = url.split('/').pop();
          content.textContent = text || '(empty)';
        } else {
          title.textContent = 'Error';
          content.textContent = 'Failed to load log: HTTP ' + res.status;
        }
      } catch(e) {
        title.textContent = 'Error';
        content.textContent = 'Failed to load log: ' + e;
      }
    });
  });

  // Close modal handler
  const closeBtn = document.getElementById('file-viewer-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', function(){
      const modal = document.getElementById('file-viewer-modal');
      if (modal) modal.classList.add('hidden');
    });
  }

  if (window.htmx) return;

  scope.querySelectorAll('button[hx-post]:not([data-v3-wired])').forEach(function(el){
    el.dataset.v3Wired = '1';
    el.addEventListener('click', async function(evt) {
      evt.preventDefault();
      const confirmMsg = el.getAttribute('hx-confirm');
      if (confirmMsg && !confirm(confirmMsg)) return;

      const include = el.getAttribute('hx-include');
      const body = new URLSearchParams();
      if (include) {
        const input = document.querySelector(include);
        if (input && input.name) body.set(input.name, input.value || '');
      }
      const response = await fetch(el.getAttribute('hx-post'), {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body.toString()
      });
      const targetSelector = el.getAttribute('hx-target');
      if (el.matches('button[hx-post^="/v2/api/run/"]')) {
        const liveButtonId = el.getAttribute('data-open-live');
        const liveButton = document.getElementById(liveButtonId);
        if (liveButton) setTimeout(() => liveButton.click(), 100);
      }
      if (targetSelector && targetSelector !== 'none') {
        const target = targetSelector === 'closest tr' ? el.closest('tr') : document.querySelector(targetSelector);
        if (target) {
          const html = await response.text();
          if (targetSelector === 'closest tr') { target.remove(); }
          else { target.innerHTML = html; wireDashboard(target); refreshIcons(target); if (window.htmx) htmx.process(target); }
        }
      } else if (el.textContent.includes('Save Defaults')) {
        alert(response.ok ? 'Saved default arguments.' : 'Failed to save default arguments.');
      }
    });
  });

  scope.querySelectorAll('[hx-get]:not([data-v3-wired])').forEach(function(el) {
    el.dataset.v3Wired = '1';
    el.addEventListener('click', async function(evt) {
      evt.preventDefault();
      const response = await fetch(el.getAttribute('hx-get'));
      const html = await response.text();
      const target = document.querySelector(el.getAttribute('hx-target'));
      if (target) {
        target.innerHTML = html; wireDashboard(target); refreshIcons(target); if (window.htmx) htmx.process(target);
        const input = target.querySelector('input[autofocus]');
        if (input) input.focus();
      }
    });
  });

  scope.querySelectorAll('form[hx-post]:not([data-v3-wired])').forEach(function(form) {
    form.dataset.v3Wired = '1';
    form.addEventListener('submit', async function(evt) {
      evt.preventDefault();
      // Before submitting, ensure any tag picker hidden input is up-to-date
      const tagPicker = form.querySelector('#edit-tags');
      if (tagPicker) {
        // already maintained by picker; nothing to do here for now
      }
      const multipart = form.enctype === 'multipart/form-data' || form.getAttribute('hx-encoding') === 'multipart/form-data';
      const request = { method: 'POST' };
      if (multipart) {
        request.body = new FormData(form);
      } else {
        request.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
        request.body = new URLSearchParams(new FormData(form)).toString();
      }
      const response = await fetch(form.getAttribute('hx-post'), request);
      const target = document.querySelector(form.getAttribute('hx-target'));
      if (target) { target.innerHTML = await response.text(); wireDashboard(target); refreshIcons(target); if (window.htmx) htmx.process(target); }
    });
  });
}

function startLiveStream(nameKey, url, rowEl, consoleEl, stateEl, loopIndex) {
  try {
    if (!url) return;
    if (liveStreams[nameKey]) return; // already streaming
    if (consoleEl) { consoleEl.textContent = 'Opening live log...\n'; }
    if (stateEl) { stateEl.textContent = 'live log (streaming)'; stateEl.className = 'text-[11px] text-emerald-400'; }

    const source = new EventSource(url + '?_=' + Date.now());
    liveStreams[nameKey] = { source, rowEl, consoleEl, stateEl };
    source.onmessage = function(evt) {
      if (consoleEl) {
        consoleEl.textContent += evt.data + '\n';
        consoleEl.scrollTop = consoleEl.scrollHeight;
      }
    };
    source.onerror = function() {
      try { source.close(); } catch(e){}
      if (stateEl) { stateEl.textContent = 'stream terminated'; stateEl.className = 'text-[11px] text-gray-500'; }
      delete liveStreams[nameKey];
    };
  } catch (e) { console.error('startLiveStream error', e); }
}

function stopLiveStream(nameKey) {
  try {
    const entry = liveStreams[nameKey];
    if (!entry) return;
    try { entry.source.close(); } catch (e) {}
    if (entry.stateEl) { entry.stateEl.textContent = 'closed'; entry.stateEl.className = 'text-[11px] text-gray-500'; }
    delete liveStreams[nameKey];
  } catch (e) { console.error('stopLiveStream error', e); }
}

function closeLiveLog(button) {
  const row = button && button.closest('[data-live-name]');
  if (!row) return;
  row.dataset.liveClosed = 'true';
  row.classList.add('hidden');
  stopLiveStream(row.getAttribute('data-live-name'));
}

async function toggleStar(button) {
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch(button.getAttribute('data-star-url'), { method: 'POST' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const starred = (await response.text()) === 'starred';
    const group = button.closest('tr[data-script-group]');
    if (group) {
      document.querySelectorAll('tr[data-script-group]').forEach(function(row) {
        if (row.dataset.scriptGroup === group.dataset.scriptGroup) row.dataset.starred = String(starred);
      });
    }
    const icon = button.querySelector('svg, i');
    button.title = starred ? 'Unstar script' : 'Star script';
    button.setAttribute('aria-label', button.title);
    if (icon) icon.classList.toggle('fill-current', starred);
    if (icon) icon.classList.toggle('text-amber-400', starred);
    applyStarFilter();
  } catch (error) {
    console.error('toggleStar error', error);
  } finally {
    button.disabled = false;
  }
}

const searchInput = document.querySelector('input[name="search"]');
let searchTimer = null;
if (searchInput && !window.htmx) {
  searchInput.addEventListener('input', function() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async function() {
      const response = await fetch('/v2/api/scripts/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({ search: searchInput.value }).toString()
      });
      const rows = document.querySelector('#script-rows');
      rows.innerHTML = await response.text();
      wireDashboard(rows); refreshIcons(rows); if (window.htmx) htmx.process(rows);
    }, 300);
  });
}

// Expose the edit modal opener intentionally while keeping other vars scoped
window.openEditScriptModal = openEditScriptModal;
window.openScheduleModal = openScheduleModal;
window.closeScheduleModal = closeScheduleModal;
window.closeLiveLog = closeLiveLog;
window.toggleStar = toggleStar;

wireDashboard(document);
refreshIcons(document);
document.body.addEventListener('htmx:afterProcessNode', function(evt) { wireDashboard(evt.target); refreshIcons(evt.target); });

})();
