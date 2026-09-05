// web/static/js/dashboard.js

// Chapter/source URLs come from tracked external sources (MangaDex, Kagane,
// etc.), not from the user -- only allow plain http(s) links through to
// window.open()/href before opening/rendering them, so a malicious tracked
// source can't hand back a javascript: URI or similar.
function isSafeUrl(url) {
	return typeof url === 'string' && /^https?:\/\//i.test(url);
}

let loadGenres;
let loadCustomTagsFilterSection;
let loadMobileCustomTagsFilterSection;

// ─── Constants ───────────────────────────────────────────────
const SORT_ICONS = {
asc: `
<path d="M11 16H17"/>
<path d="M11 20H19"/>
<path d="M11 12H15"/>
<path d="M4 8L7 5L10 8"/>
<path d="M7 20L7 6"/>
`,
desc: `
<path d="M11 9H17"/>
<path d="M11 5H19"/>
<path d="M11 13H15"/>
<path d="M10 17L7 20L4 17"/>
<path d="M7 5V19"/>
`
};

// ─── Volume-aware sorting helpers ─────────────────────────────
function getVolumeKey(vol) {
	if (vol == null || vol === '') return [0, 0];
	vol = String(vol).trim();
	const num = Number(vol);
	if (!isNaN(num)) {
		return [1, num];
	}
	return [2, vol];
}
function compareChapters(a, b) {
	const volA = getVolumeKey(a.volume);
	const volB = getVolumeKey(b.volume);
	if (volA[0] !== volB[0]) return volA[0] - volB[0];
	if (volA[0] === 1) {
		if (volA[1] !== volB[1]) return volA[1] - volB[1];
	} else {
		if (volA[1] !== volB[1]) return volA[1].localeCompare(volB[1]);
	}
	return a.chapter_number - b.chapter_number;
}

// ─── In-Memory State (no URL params) ───────────────────────────
const state = {
	page: 1,
	status: 'reading',
	sort: 'unread_first',
	dir: 'asc',
	type: [],
	genre: [],
	rating: [
		{ name: 'mature', mode: 'exclude' },
		{ name: 'explicit', mode: 'exclude' }
	],
	pubStatus: [],
	readableOn: [],
	customTags: [], // custom_tags.id list, simple include-only (no exclude mode)
	allSeries: [], // Store fetched series
	hasLoadedOnce: false, // Track if initial load completed
	lastPage: undefined // Track page changes for scroll behavior
};

// Loading state to prevent multiple simultaneous loads
let isLoadingPage = false;

// Saved filter/sort "bookmarks" (the dropdown left of the status filter).
// bookmarksCache mirrors /api/filter-bookmarks; state.activeBookmarkId
// tracks which one is currently applied (used for the checkmark in the
// list and to decide whether "Save" overwrites it or prompts for a new one).
let bookmarksCache = [];
state.activeBookmarkId = null;
// Staged renames in the Manage Views modal ({id: newName}) - not sent to
// the server until the modal's Close/Save button is clicked, matching the
// rest of the app's "stage locally, one button commits" pattern (e.g.
// Series Settings) instead of saving on every blur.
let pendingBookmarkRenames = {};

// Track default excluded ratings to hide from count
const DEFAULT_EXCLUDED_RATINGS = ['mature', 'explicit'];

// Ratings reset to this (Mature/Explicit excluded), not to an empty filter
function getDefaultRatingState() {
	return DEFAULT_EXCLUDED_RATINGS.map(name => ({ name, mode: 'exclude' }));
}

// Shared by the desktop and mobile Tags dropdowns. Mature/Explicit start
// hidden by default (a safety default, not a filter the user picked), so
// they're never counted here regardless of mode -- only genres, custom
// tags, and any rating the user has actively touched away from that
// baseline count toward the badge.
function formatTagsTriggerText(genreCount, ratingList, customTagCount) {
	const activeRatingCount = ratingList.filter(r =>
		!(DEFAULT_EXCLUDED_RATINGS.includes(r.name) && r.mode === 'exclude')
	).length;
	const totalCount = genreCount + activeRatingCount + customTagCount;

	return totalCount === 0 ? 'Tags' : `${totalCount} Selected`;
}

// ─── Filter Bookmarks (saved views) ───────────────────────────────
// A bookmark is a named snapshot of every filter/sort dimension at once.
// Applying one overwrites state.* wholesale and resyncs every control that
// reflects it (desktop status/sort/checkboxes + the mobile drawer's
// duplicates, mirroring exactly what resetMobileFilters() already does for
// the hardcoded defaults, just generalized to arbitrary saved values).

const STATUS_LABELS_FOR_BOOKMARKS = {
	all: 'All Statuses', reading: 'Reading', plan_to_read: 'Plan to Read',
	on_hold: 'On Hold', dropped: 'Dropped', completed: 'Completed'
};
const SORT_LABELS_FOR_BOOKMARKS = {
	unread_first: 'Unread First', title: 'Title (A→Z)', latest_release: 'Chapter Released',
	last_added: 'Last Added', total_chapters: 'Total Chapters', available_chapters: 'Available Chapters'
};
const TYPE_LABELS_FOR_BOOKMARKS = { manga: 'Manga', manhwa: 'Manhwa', manhua: 'Manhua', other: 'Other' };
const PUB_STATUS_LABELS_FOR_BOOKMARKS = {
	reading: 'Reading', completed: 'Completed', on_hold: 'On Hold', dropped: 'Dropped', plan_to_read: 'Plan to Read'
};
const READABLE_ON_LABELS_FOR_BOOKMARKS = { mangadex: 'MangaDex', kagane: 'Kagane', atsu: 'Atsumaru', asura: 'AsuraScans', hive: 'HiveToons' };

function captureCurrentFilterState() {
	return {
		status: state.status,
		sort: state.sort,
		dir: state.dir,
		type: [...state.type],
		genre: state.genre.map(g => ({ ...g })),
		rating: state.rating.map(r => ({ ...r })),
		pubStatus: [...state.pubStatus],
		readableOn: [...state.readableOn],
		customTags: [...state.customTags]
	};
}

function normalizeFilterStateForCompare(fs) {
	return JSON.stringify({
		status: fs.status, sort: fs.sort, dir: fs.dir,
		type: [...(fs.type || [])].sort(),
		genre: [...(fs.genre || [])].map(g => ({ name: g.name, mode: g.mode })).sort((a, b) => a.name.localeCompare(b.name)),
		rating: [...(fs.rating || [])].map(r => ({ name: r.name, mode: r.mode })).sort((a, b) => a.name.localeCompare(b.name)),
		pubStatus: [...(fs.pubStatus || [])].sort(),
		readableOn: [...(fs.readableOn || [])].sort(),
		customTags: [...(fs.customTags || [])].sort((a, b) => a - b)
	});
}

function filterStatesEqual(a, b) {
	return normalizeFilterStateForCompare(a) === normalizeFilterStateForCompare(b);
}

function applyFilterBookmarkState(fs) {
	state.status = fs.status || 'reading';
	state.sort = fs.sort || 'unread_first';
	state.dir = fs.dir || 'asc';
	state.type = Array.isArray(fs.type) ? [...fs.type] : [];
	state.genre = Array.isArray(fs.genre) ? fs.genre.map(g => ({ ...g })) : [];
	state.rating = Array.isArray(fs.rating) ? fs.rating.map(r => ({ ...r })) : [];
	state.pubStatus = Array.isArray(fs.pubStatus) ? [...fs.pubStatus] : [];
	state.readableOn = Array.isArray(fs.readableOn) ? [...fs.readableOn] : [];
	state.customTags = Array.isArray(fs.customTags) ? [...fs.customTags] : [];
	state.tagsMode = 'include';
	state.page = 1;

	// Tags-mode toggle (desktop + mobile) resets to Include - the applied
	// genre/rating filters are self-describing via their own per-entry
	// mode either way, same as the plain Reset flow.
	[document.getElementById('btn-tags-mode'), document.getElementById('mobile-btn-tags-mode')].forEach(btn => {
		if (!btn) return;
		btn.dataset.mode = 'include';
		const icon = btn.querySelector('svg');
		const text = btn.querySelector('span');
		if (icon) icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
		if (text) text.textContent = 'Include Mode';
	});

	// Clear every checkbox (desktop + mobile), then set only what this
	// bookmark specifies.
	document.querySelectorAll(`
		#filter-type-container input[type="checkbox"],
		#filter-genre-container input[type="checkbox"],
		#filter-genre-container .rating-checkbox,
		#filter-pub-status-container input[type="checkbox"],
		#filter-readable-on-container input[type="checkbox"],
		#mobile-filter-type-container input[type="checkbox"],
		#mobile-filter-genre-container input[type="checkbox"],
		#mobile-filter-genre-container .rating-checkbox,
		#mobile-filter-pub-status-container input[type="checkbox"],
		#mobile-filter-readable-on-container input[type="checkbox"]
	`).forEach(cb => { cb.checked = false; });
	document.querySelectorAll(`
		#filter-genre-container .genre-list-section input[type="checkbox"],
		#mobile-filter-genre-container .genre-list-section input[type="checkbox"],
		#filter-genre-container .rating-checkbox,
		#mobile-filter-genre-container .rating-checkbox
	`).forEach(cb => { delete cb.dataset.mode; });

	state.type.forEach(v => {
		document.querySelectorAll(`#filter-type-container input[value="${v}"], #mobile-filter-type-container input[value="${v}"]`)
			.forEach(cb => { cb.checked = true; });
	});
	state.pubStatus.forEach(v => {
		document.querySelectorAll(`#filter-pub-status-container input[value="${v}"], #mobile-filter-pub-status-container input[value="${v}"]`)
			.forEach(cb => { cb.checked = true; });
	});
	state.readableOn.forEach(v => {
		document.querySelectorAll(`#filter-readable-on-container input[value="${v}"], #mobile-filter-readable-on-container input[value="${v}"]`)
			.forEach(cb => { cb.checked = true; });
	});
	state.genre.forEach(g => {
		document.querySelectorAll(`#filter-genre-container .genre-list-section input[value="${g.name}"], #mobile-filter-genre-container .genre-list-section input[value="${g.name}"]`)
			.forEach(cb => { cb.dataset.mode = g.mode; });
	});
	state.rating.forEach(r => {
		document.querySelectorAll(`#filter-genre-container .rating-checkbox[value="${r.name}"], #mobile-filter-genre-container .rating-checkbox[value="${r.name}"]`)
			.forEach(cb => { cb.dataset.mode = r.mode; });
	});
	state.customTags.forEach(tagId => {
		document.querySelectorAll(`#filter-genre-container .custom-tags-section input[value="${tagId}"], #mobile-filter-genre-container .custom-tags-section input[value="${tagId}"]`)
			.forEach(cb => { cb.checked = true; });
	});

	const setText = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
	setText('filter-status-trigger', STATUS_LABELS_FOR_BOOKMARKS[state.status] || state.status);
	setText('sort-order-trigger', SORT_LABELS_FOR_BOOKMARKS[state.sort] || state.sort);
	document.querySelectorAll('.single-select-menu .option-item').forEach(opt => {
		opt.classList.remove('selected');
		if ((opt.dataset.value === state.status && opt.closest('#filter-status-container')) ||
			(opt.dataset.value === state.sort && opt.closest('#sort-order-container'))) {
			opt.classList.add('selected');
		}
	});

	const typeTrigger = document.getElementById('filter-type-trigger');
	const mobileTypeTrigger = document.getElementById('mobile-filter-type-trigger');
	if (typeTrigger) updateTriggerText(typeTrigger, state.type, TYPE_LABELS_FOR_BOOKMARKS, 'Content Type');
	if (mobileTypeTrigger) updateTriggerText(mobileTypeTrigger, state.type, TYPE_LABELS_FOR_BOOKMARKS, 'Content Type');

	const pubTrigger = document.getElementById('filter-pub-status-trigger');
	const mobilePubTrigger = document.getElementById('mobile-filter-pub-status-trigger');
	if (pubTrigger) updateTriggerText(pubTrigger, state.pubStatus, PUB_STATUS_LABELS_FOR_BOOKMARKS, 'Publication Status');
	if (mobilePubTrigger) updateTriggerText(mobilePubTrigger, state.pubStatus, PUB_STATUS_LABELS_FOR_BOOKMARKS, 'Publication Status');

	const readableTrigger = document.getElementById('filter-readable-on-trigger');
	const mobileReadableTrigger = document.getElementById('mobile-filter-readable-on-trigger');
	if (readableTrigger) updateTriggerText(readableTrigger, state.readableOn, READABLE_ON_LABELS_FOR_BOOKMARKS, 'Readable On');
	if (mobileReadableTrigger) updateTriggerText(mobileReadableTrigger, state.readableOn, READABLE_ON_LABELS_FOR_BOOKMARKS, 'Readable On');

	const tagsText = formatTagsTriggerText(state.genre.length, state.rating, state.customTags.length);
	setText('filter-genre-trigger', tagsText);
	setText('mobile-filter-genre-trigger', tagsText);

	const sortIcon = document.getElementById('sort-direction-icon');
	if (sortIcon) sortIcon.innerHTML = SORT_ICONS[state.dir];
	const mobileSortIcon = document.querySelector('#mobile-sort-direction svg');
	if (mobileSortIcon) mobileSortIcon.innerHTML = SORT_ICONS[state.dir];

	document.querySelectorAll('.multi-select-menu, .single-select-menu').forEach(menu => {
		menu.classList.add('hidden');
	});
}

async function fetchFilterBookmarks() {
	try {
		const res = await fetch('/api/filter-bookmarks');
		if (!res.ok) return;
		const data = await res.json();
		bookmarksCache = data.bookmarks || [];
	} catch (e) {
		console.error('Failed to load filter bookmarks:', e);
	}
}

function updateBookmarkTriggerText(name) {
	['filter-bookmark-trigger-text', 'mobile-filter-bookmark-trigger-text'].forEach(id => {
		const el = document.getElementById(id);
		if (el) el.textContent = name;
	});
}

function closeBookmarkMenus() {
	document.getElementById('filter-bookmark-menu')?.classList.add('hidden');
	document.getElementById('mobile-filter-bookmark-menu')?.classList.add('hidden');
	document.getElementById('bookmark-new-view-form')?.classList.add('hidden');
	document.getElementById('mobile-bookmark-new-view-form')?.classList.add('hidden');
}

function renderBookmarkList() {
	[['bookmark-list', 'bookmark-search-input'], ['mobile-bookmark-list', 'mobile-bookmark-search-input']].forEach(([listId, searchId]) => {
		const el = document.getElementById(listId);
		if (!el) return;
		const q = (document.getElementById(searchId)?.value || '').trim().toLowerCase();
		const filtered = bookmarksCache.filter(b => b.name.toLowerCase().includes(q));
		el.innerHTML = filtered.length === 0
			? '<p class="bookmark-empty">No views match.</p>'
			: filtered.map(b => `
				<div class="bookmark-item ${b.id === state.activeBookmarkId ? 'active' : ''}" data-bookmark-id="${b.id}">
					<span class="bookmark-item-name">${escapeHtml(b.name)}</span>
					<svg class="bookmark-item-check" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
						<polyline points="20 6 9 17 4 12"/>
					</svg>
				</div>
			`).join('');
		el.querySelectorAll('.bookmark-item').forEach(item => {
			item.addEventListener('click', () => selectBookmarkById(parseInt(item.dataset.bookmarkId, 10)));
		});
	});
}

function updateBookmarkUpdateButtonState() {
	// "Update current view" only makes sense for a non-builtin bookmark
	// that's actually diverged from its saved state - Default is always
	// protected regardless, and there's nothing to write back if nothing
	// changed. "Save as new view" has no such restriction - always enabled.
	const activeBookmark = bookmarksCache.find(b => b.id === state.activeBookmarkId);
	const matchesActive = activeBookmark ? filterStatesEqual(captureCurrentFilterState(), activeBookmark.filter_state) : true;
	const canUpdate = !!activeBookmark && !activeBookmark.is_builtin && !matchesActive;
	['bookmark-update-btn', 'mobile-bookmark-update-btn'].forEach(id => {
		const btn = document.getElementById(id);
		if (btn) btn.disabled = !canUpdate;
	});
}

function selectBookmarkById(id) {
	const bm = bookmarksCache.find(b => b.id === id);
	if (!bm) return;
	state.activeBookmarkId = bm.id;
	applyFilterBookmarkState(bm.filter_state);
	updateBookmarkTriggerText(bm.name);
	closeBookmarkMenus();
	renderBookmarkList();
	updateBookmarkUpdateButtonState();
	loadPage();
}

async function handleUpdateCurrentViewClick() {
	const activeBookmark = bookmarksCache.find(b => b.id === state.activeBookmarkId);
	if (!activeBookmark || activeBookmark.is_builtin) return; // button should be disabled anyway
	const currentFs = captureCurrentFilterState();
	try {
		const res = await fetch(`/api/filter-bookmarks/${activeBookmark.id}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ filter_state: currentFs })
		});
		if (res.ok) {
			activeBookmark.filter_state = currentFs;
			updateBookmarkUpdateButtonState();
			showNotification(`Updated view "${activeBookmark.name}"`, 'bookmark_updated');
		} else {
			showNotification('Failed to update view', 'error');
		}
	} catch (e) {
		showNotification('Failed to update view', 'error');
	}
}

// Inline name-entry form for "Save as new view" - swaps in over the whole
// list/actions area (not just appended below the button that opened it)
// instead of the browser's native prompt().
function showNewViewForm(formId, nameInputId, mainId) {
	document.getElementById(mainId)?.classList.add('hidden');
	document.getElementById(formId)?.classList.remove('hidden');
	const input = document.getElementById(nameInputId);
	if (input) { input.value = ''; input.focus(); }
}

function hideNewViewForm(formId, mainId) {
	document.getElementById(formId)?.classList.add('hidden');
	document.getElementById(mainId)?.classList.remove('hidden');
}

async function submitNewView(nameInputId, formId, mainId) {
	const input = document.getElementById(nameInputId);
	const name = (input?.value || '').trim();
	if (!name) { input?.focus(); return; }

	const currentFs = captureCurrentFilterState();
	try {
		const res = await fetch('/api/filter-bookmarks', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ name, filter_state: currentFs })
		});
		if (res.ok) {
			const { id } = await res.json();
			await fetchFilterBookmarks();
			state.activeBookmarkId = id;
			const newBookmark = bookmarksCache.find(b => b.id === id);
			if (newBookmark) updateBookmarkTriggerText(newBookmark.name);
			hideNewViewForm(formId, mainId);
			renderBookmarkList();
			updateBookmarkUpdateButtonState();
			showNotification(`Saved view "${name}"`, 'bookmark_added');
		} else {
			showNotification('Failed to save view', 'error');
		}
	} catch (e) {
		showNotification('Failed to save view', 'error');
	}
}

function setupNewViewForm(newBtnId, formId, nameInputId, confirmId, cancelId, mainId) {
	document.getElementById(newBtnId)?.addEventListener('click', () => showNewViewForm(formId, nameInputId, mainId));
	document.getElementById(cancelId)?.addEventListener('click', () => hideNewViewForm(formId, mainId));
	document.getElementById(confirmId)?.addEventListener('click', () => submitNewView(nameInputId, formId, mainId));
	const nameInput = document.getElementById(nameInputId);
	if (nameInput) {
		nameInput.addEventListener('click', (e) => e.stopPropagation());
		nameInput.addEventListener('keydown', (e) => {
			if (e.key === 'Enter') { e.preventDefault(); submitNewView(nameInputId, formId, mainId); }
			else if (e.key === 'Escape') { e.preventDefault(); hideNewViewForm(formId, mainId); }
		});
	}
}

function setupBookmarkDropdown(triggerId, menuId, searchId, formId, mainId) {
	const trigger = document.getElementById(triggerId);
	const menu = document.getElementById(menuId);
	if (!trigger || !menu) return;
	// Auto-focusing the search box on mobile summons the on-screen keyboard,
	// eating most of the drawer's remaining vertical space - only do it on
	// desktop, where there's no keyboard to worry about.
	const isMobileDrawer = triggerId.startsWith('mobile-');

	document.addEventListener('click', (e) => {
		if (!menu.contains(e.target) && e.target !== trigger && !trigger.contains(e.target)) {
			menu.classList.add('hidden');
		}
	});

	trigger.addEventListener('click', (e) => {
		e.stopPropagation();
		const wasHidden = menu.classList.contains('hidden');
		closeAllMultiSelectMenus();
		menu.classList.toggle('hidden', !wasHidden);
		if (wasHidden) {
			// Always reopen to the search/list view, even if a "save as new
			// view" form was left open the last time this was closed.
			hideNewViewForm(formId, mainId);
			const searchInput = document.getElementById(searchId);
			if (searchInput) {
				searchInput.value = '';
				if (!isMobileDrawer) searchInput.focus();
			}
			renderBookmarkList();
		}
	});

	const searchInput = document.getElementById(searchId);
	if (searchInput) {
		searchInput.addEventListener('input', () => renderBookmarkList());
		searchInput.addEventListener('click', (e) => e.stopPropagation());
	}
}

function renderManageBookmarksList() {
	const container = document.getElementById('manage-bookmarks-list');
	if (!container) return;
	container.innerHTML = bookmarksCache.map(b => `
		<div class="manage-bookmark-row" data-bookmark-id="${b.id}">
			<input type="text" class="manage-bookmark-name-input" value="${escapeHtml(pendingBookmarkRenames[b.id] ?? b.name)}" ${b.is_builtin ? 'disabled' : ''} />
			${b.is_builtin
				? '<span class="manage-bookmark-builtin-label">Protected</span>'
				: `<button type="button" class="manage-bookmark-delete-btn" title="Delete view">
					<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
						<polyline points="3 6 5 6 21 6"/>
						<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
					</svg>
				</button>`
			}
		</div>
	`).join('');

	container.querySelectorAll('.manage-bookmark-row').forEach(row => {
		const id = parseInt(row.dataset.bookmarkId, 10);
		const bm = bookmarksCache.find(b => b.id === id);
		if (!bm) return;

		const input = row.querySelector('.manage-bookmark-name-input');
		if (input && !bm.is_builtin) {
			input.addEventListener('keydown', (e) => { if (e.key === 'Enter') input.blur(); });
			// Staged, not saved here - the Close/Save button at the bottom
			// commits every pending rename at once.
			input.addEventListener('input', () => {
				const newName = input.value.trim();
				if (!newName || newName === bm.name) {
					delete pendingBookmarkRenames[id];
				} else {
					pendingBookmarkRenames[id] = newName;
				}
				updateManageBookmarksButtonState();
			});
		}

		const deleteBtn = row.querySelector('.manage-bookmark-delete-btn');
		if (deleteBtn) {
			// Swaps the row into an inline "Delete "X"? Cancel / Delete"
			// confirm state instead of deleting on the first click - matches
			// the app's avoidance of native confirm() elsewhere.
			deleteBtn.addEventListener('click', () => {
				row.innerHTML = `
					<span class="manage-bookmark-confirm-text">Delete "${escapeHtml(bm.name)}"?</span>
					<div class="manage-bookmark-confirm-actions">
						<button type="button" class="bookmark-form-btn bookmark-form-cancel manage-bookmark-cancel-delete">Cancel</button>
						<button type="button" class="bookmark-form-btn bookmark-form-delete-confirm manage-bookmark-confirm-delete">Delete</button>
					</div>
				`;
				row.querySelector('.manage-bookmark-cancel-delete')?.addEventListener('click', renderManageBookmarksList);
				row.querySelector('.manage-bookmark-confirm-delete')?.addEventListener('click', async () => {
					try {
						const res = await fetch(`/api/filter-bookmarks/${id}`, { method: 'DELETE' });
						if (res.ok) {
							const wasActive = state.activeBookmarkId === id;
							bookmarksCache = bookmarksCache.filter(b => b.id !== id);
							delete pendingBookmarkRenames[id]; // no point saving a rename for a view that's gone
							renderManageBookmarksList();
							updateManageBookmarksButtonState();
							renderBookmarkList();
							if (wasActive) {
								const fallback = bookmarksCache.find(b => b.is_builtin);
								if (fallback) selectBookmarkById(fallback.id);
							}
							showNotification('View deleted', 'bookmark_deleted');
						} else {
							showNotification('Failed to delete view', 'error');
						}
					} catch (e) {
						showNotification('Failed to delete view', 'error');
					}
				});
			});
		}
	});
}

async function openManageBookmarksModal() {
	// Manage views is reachable from inside the mobile filter drawer;
	// #manage-bookmarks-modal's z-index (3500) is set above the drawer's
	// (3000) specifically so both can stay open at once instead of forcing
	// the drawer closed.
	pendingBookmarkRenames = {};
	await fetchFilterBookmarks();
	renderManageBookmarksList();
	updateManageBookmarksButtonState();
	document.getElementById('manage-bookmarks-modal')?.classList.remove('hidden');
}

function updateManageBookmarksButtonState() {
	const btn = document.getElementById('btn-manage-bookmarks-close');
	if (!btn) return;
	const hasPending = Object.keys(pendingBookmarkRenames).length > 0;
	btn.textContent = hasPending ? 'Save' : 'Close';
	btn.classList.toggle('btn-primary', hasPending);
	btn.classList.toggle('btn-secondary', !hasPending);
}

async function handleManageBookmarksCloseClick() {
	const entries = Object.entries(pendingBookmarkRenames);
	if (entries.length === 0) {
		closeManageBookmarksModal();
		return;
	}

	const btn = document.getElementById('btn-manage-bookmarks-close');
	if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

	try {
		const results = await Promise.all(entries.map(([id, name]) =>
			fetch(`/api/filter-bookmarks/${id}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ name })
			}).then(res => ({ id: parseInt(id, 10), name, ok: res.ok }))
		));

		const failed = results.filter(r => !r.ok);
		results.filter(r => r.ok).forEach(r => {
			const bm = bookmarksCache.find(b => b.id === r.id);
			if (bm) bm.name = r.name;
			if (state.activeBookmarkId === r.id) updateBookmarkTriggerText(r.name);
			delete pendingBookmarkRenames[r.id];
		});
		renderBookmarkList();

		if (failed.length > 0) {
			showNotification(`Failed to rename ${failed.length} view(s)`, 'error');
			renderManageBookmarksList();
			updateManageBookmarksButtonState();
			// Leave the modal open so the still-pending ones aren't lost.
		} else {
			showNotification('Views updated', 'bookmark_updated');
			closeManageBookmarksModal();
		}
	} catch (e) {
		showNotification('Failed to save changes', 'error');
	} finally {
		if (btn) btn.disabled = false;
	}
}

function closeManageBookmarksModal() {
	document.getElementById('manage-bookmarks-modal')?.classList.add('hidden');
}

// Reset Filters sets state.* directly rather than going through
// applyFilterBookmarkState()/selectBookmarkById(), so it has to separately
// tell the bookmark tracker "we're back to Default" - otherwise the
// dropdown keeps showing whatever was last explicitly selected even
// though the actual filters reverted.
function markDefaultBookmarkActive() {
	const def = bookmarksCache.find(b => b.is_builtin);
	if (!def) return;
	state.activeBookmarkId = def.id;
	updateBookmarkTriggerText(def.name);
	renderBookmarkList();
	updateBookmarkUpdateButtonState();
}

async function initBookmarkDropdown() {
	await fetchFilterBookmarks();
	const def = bookmarksCache.find(b => b.is_builtin);
	if (def) {
		state.activeBookmarkId = def.id;
		updateBookmarkTriggerText(def.name);
	}
	renderBookmarkList();
	updateBookmarkUpdateButtonState();

	setupBookmarkDropdown('filter-bookmark-trigger', 'filter-bookmark-menu', 'bookmark-search-input', 'bookmark-new-view-form', 'bookmark-menu-main');
	setupBookmarkDropdown('mobile-filter-bookmark-trigger', 'mobile-filter-bookmark-menu', 'mobile-bookmark-search-input', 'mobile-bookmark-new-view-form', 'mobile-bookmark-menu-main');

	document.getElementById('bookmark-update-btn')?.addEventListener('click', handleUpdateCurrentViewClick);
	document.getElementById('mobile-bookmark-update-btn')?.addEventListener('click', handleUpdateCurrentViewClick);
	setupNewViewForm('bookmark-new-btn', 'bookmark-new-view-form', 'bookmark-new-view-name', 'bookmark-new-view-confirm', 'bookmark-new-view-cancel', 'bookmark-menu-main');
	setupNewViewForm('mobile-bookmark-new-btn', 'mobile-bookmark-new-view-form', 'mobile-bookmark-new-view-name', 'mobile-bookmark-new-view-confirm', 'mobile-bookmark-new-view-cancel', 'mobile-bookmark-menu-main');
	document.getElementById('bookmark-manage-btn')?.addEventListener('click', () => { closeBookmarkMenus(); openManageBookmarksModal(); });
	document.getElementById('mobile-bookmark-manage-btn')?.addEventListener('click', () => { closeBookmarkMenus(); openManageBookmarksModal(); });
	document.getElementById('btn-manage-bookmarks-close')?.addEventListener('click', handleManageBookmarksCloseClick);
	// Click on the dimmed backdrop (not the panel itself) closes it too -
	// routed through the same save-if-pending handler as the button so a
	// staged rename isn't silently discarded just because it was closed a
	// different way.
	document.getElementById('manage-bookmarks-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'manage-bookmarks-modal') handleManageBookmarksCloseClick();
	});
}

// ─── Bulk Selection State ────────────────────────────────────────
const bulkState = {
	selectedIds: new Set(),
	isBulkMode: false
};

function enterBulkMode() {
	bulkState.isBulkMode = true;
	document.querySelectorAll('.series-card').forEach(card => {
		card.classList.add('bulk-mode');
	});
	document.getElementById('default-actions').style.display = 'none';
	document.getElementById('bulk-actions').style.display = 'flex';
}
function exitBulkMode() {
	bulkState.isBulkMode = false;
	bulkState.selectedIds.clear();
	document.querySelectorAll('.series-card').forEach(card => {
		card.classList.remove('bulk-mode', 'selected');
	});
	document.getElementById('default-actions').style.display = 'flex';
	document.getElementById('bulk-actions').style.display = 'none';
}
function toggleCardSelection(seriesId) {
	const card = document.querySelector(`.series-card[data-series-id="${seriesId}"]`);
	if (!card) return;
	if (bulkState.selectedIds.has(seriesId)) {
		bulkState.selectedIds.delete(seriesId);
		card.classList.remove('selected');
	} else {
		bulkState.selectedIds.add(seriesId);
		card.classList.add('selected');
	}
	if (bulkState.selectedIds.size === 0) {
		exitBulkMode();
	} else if (!bulkState.isBulkMode) {
		enterBulkMode();
	}
}

// ─── Core Functions ──────────────────────────────────────────
function saveChapter(seriesId, chapter, oldChapter = null) {
  return fetch(`/api/series/${seriesId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_chapter: chapter })
  }).then(res => {
    // ADDED: Show notification on successful chapter update
    if (res.ok) {
      // Find series title and old chapter from card if not provided
      const card = document.querySelector(`.series-card[data-series-id="${seriesId}"]`);
      const seriesTitle = card?.querySelector('.card-title')?.textContent || 'Series';
      
      // Get old chapter if not provided
      if (oldChapter === null) {
        // Try to find it from state.allSeries
        const series = state.allSeries?.find(s => s.id === seriesId);
        oldChapter = series?.current_chapter ?? null;
      }
      
      // Format chapter text
      const formatChapter = (ch) => ch === -1 ? 'Not started' : `Ch.${ch}`;
      const newChapterText = formatChapter(chapter);
      
      // Show notification with or without old chapter
      if (oldChapter !== null && oldChapter !== chapter) {
        const oldChapterText = formatChapter(oldChapter);
        showNotification(`${seriesTitle} updated from ${oldChapterText} to ${newChapterText}`, 'read');
      } else {
        showNotification(`${seriesTitle} updated to ${newChapterText}`, 'read');
      }
    }
    return res;
  });
}

let currentSeriesIdForEdit = null;
// Store original values for comparison
let originalSeriesValues = null;
// Staged cover pick from the settings modal's cover-edit menu -- null means
// "no change staged", same convention as the title/chapter staging below.
let pendingCoverUrl = null;
// Resolves to this series' sources (with per-source cover_url) once fetched
// in openEditModal; the cover menu awaits it so it doesn't need its own fetch.
let currentSeriesSourcesPromise = null;
// Same idea for previously-uploaded covers, so re-picking one doesn't require
// re-uploading. currentSeriesUploadsCache is kept in sync so a fresh upload
// can be prepended without waiting on another round-trip to the server.
let currentSeriesUploadsPromise = null;
let currentSeriesUploadsCache = [];
// The full MangaDex cover gallery (every volume/locale variant), fetched
// automatically when a MangaDex source was added - same await-a-promise
// pattern as sources/uploads above, no separate fetch needed in the menu.
let currentSeriesMangadexCoversPromise = null;
// Which page of 4 gallery covers is currently showing - reset whenever a
// series' modal (re)opens so it doesn't carry over between series.
let mangadexCoverPage = 0;
const MANGADEX_COVERS_PER_PAGE = 4;
// Aggregate chapter count shown next to the source name in the Source
// selector -- we don't track a per-source chapter count, so this is the
// series-wide latest_chapter as a reasonable stand-in.
let currentSeriesLatestChapter = null;
// Custom tags (user-defined, separate from scraped genres): allCustomTagsCache
// is the shared global tag vocabulary. Creating/deleting a tag is a global
// action and commits immediately (like Source add/remove). But which tags
// apply to *this* series is staged like title/chapter/cover/status --
// currentSeriesTagIds is the saved baseline (fetched on modal open),
// pendingSeriesTagIds is what the checkboxes currently show; Save diffs the
// two and only then calls the attach/detach endpoints.
let allCustomTagsCache = [];
let currentSeriesTagIds = [];
let pendingSeriesTagIds = [];
// Which source is primary: staged like tags/chapter/cover/status rather
// than committed immediately, so it's consistent with the rest of the
// modal (everything else needs Save; this used to apply the instant you
// clicked the star icon, with no way to back out short of picking
// another source back).
let currentPrimarySourceId = null;
let pendingPrimarySourceId = null;
// Series Settings modal: 'select' (dropdown, picks an actually-tracked
// chapter) or 'manual' (free-entry Volume/Chapter steppers, for chapters
// read ahead of what's been scraped). manualChapterValue/manualVolumeValue
// are seeded from the current series values the first time manual mode is
// entered (manualValuesSeeded guards against re-seeding on a later toggle,
// which would stomp whatever the user already typed).
let chapterInputMode = 'select';
let manualValuesSeeded = false;
let manualChapterValue = 0;
let manualVolumeValue = null;

// Shared close path for the Series Settings modal, used from both desktop
// (Edit) and mobile (the merged Edit action, which opens this same modal
// instead of the old separate mobile Edit/Settings ones). Desktop only
// ever needs to clear overflow:hidden -- but mobile's bottom sheet locks
// the background scroll with position:fixed + a negative top offset
// (since overflow:hidden alone doesn't stop iOS rubber-band scrolling),
// which needs the matching restore or the page is left unscrollable.
function closeEditSeriesModal() {
	document.getElementById('edit-series-modal')?.classList.add('hidden');
	// The Tags popover gets reparented to <body> while open (see
	// openTagsMenu) to escape the modal's overflow clipping, so it's no
	// longer a descendant that hiding the modal auto-hides.
	document.getElementById('settings-tags-menu')?.classList.add('hidden');

	if (document.body.style.position === 'fixed') {
		const savedScrollY = mobileState.scrollY || 0;
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
		mobileState.scrollY = 0;
		window.scrollTo(0, savedScrollY);
	} else {
		document.body.style.overflow = '';
	}
}

function openEditModal(series) {
	currentSeriesIdForEdit = series.id;
	pendingCoverUrl = null;

	const fixChaptersLink = document.getElementById('settings-fix-chapters-link');
	if (fixChaptersLink) fixChaptersLink.href = `/chapter-fixes?series_id=${series.id}`;
	// closeCoverMenu() lives inside the DOMContentLoaded closure below and
	// isn't reachable from this top-level function, so reset directly.
	document.getElementById('settings-cover-menu')?.classList.add('hidden');
	document.getElementById('settings-cover-col')?.classList.remove('cover-menu-open');

	// Store original values including current chapter
	originalSeriesValues = {
		title: series.title || '',
		cover_url: series.cover_url || '',
		status: series.status || 'plan_to_read',
		current_chapter: series.current_chapter,
		current_volume: series.current_volume ?? null
	};

	const statusSelect = document.getElementById('edit-status');
	if (statusSelect) statusSelect.value = originalSeriesValues.status;
	syncStatusCustomUI();

	// Reset the manual chapter/volume entry mode -- closeManualChapterMode()
	// lives inside the DOMContentLoaded closure below and isn't reachable
	// from this top-level function, so reset directly.
	chapterInputMode = 'select';
	manualValuesSeeded = false;
	document.getElementById('chapter-manual-group')?.classList.add('hidden');
	document.getElementById('chapter-select-group')?.classList.remove('hidden');
	const chapterModeToggle = document.getElementById('chapter-mode-toggle');
	if (chapterModeToggle) chapterModeToggle.textContent = 'manually';

	// Reset the Source selector's dropdown -- closeSourceMenu() lives inside
	// the DOMContentLoaded closure below, same reason as the two resets above.
	document.getElementById('settings-source-menu')?.classList.add('hidden');
	document.getElementById('settings-source-selector')?.classList.remove('open');
	currentSeriesLatestChapter = series.latest_chapter ?? null;

	// Same reset for the Tags picker.
	document.getElementById('settings-tags-menu')?.classList.add('hidden');

	// Same reset for the Chapter and Status custom dropdowns.
	document.getElementById('chapter-select-menu')?.classList.add('hidden');
	document.getElementById('chapter-select-trigger')?.classList.remove('open');
	document.getElementById('settings-status-menu')?.classList.add('hidden');
	document.getElementById('settings-status-selector')?.classList.remove('open');

	document.getElementById('edit-series-id').value = series.id;
	document.getElementById('edit-series-title-heading').textContent = series.title || 'Series Settings';

	const coverImg = document.getElementById('edit-series-cover-img');
	coverImg.src = (series.cover_protected_url || series.cover_url || '/static/placeholder.png').replace(/\s+/g, '');
	coverImg.alt = series.title || '';

	currentSeriesSourcesPromise = fetch(`/api/series/${series.id}/sources`)
		.then(r => r.json())
		.then(data => data.sources || [])
		.catch(() => []);
	currentSeriesSourcesPromise.then(sources => {
		const primary = sources.find(s => s.is_primary);
		currentPrimarySourceId = primary ? primary.id : null;
		pendingPrimarySourceId = currentPrimarySourceId;
		renderSourceSelector(sources);
		renderSourceList(sources);
	});

	currentSeriesUploadsPromise = fetch(`/api/series/${series.id}/uploaded-covers`)
		.then(r => r.json())
		.then(data => {
			currentSeriesUploadsCache = data.covers || [];
			return currentSeriesUploadsCache;
		})
		.catch(() => {
			currentSeriesUploadsCache = [];
			return currentSeriesUploadsCache;
		});

	mangadexCoverPage = 0;
	currentSeriesMangadexCoversPromise = fetch(`/api/series/${series.id}/mangadex-covers`)
		.then(r => r.json())
		.then(data => data.covers || [])
		.catch(() => []);

	Promise.all([
		fetch('/api/custom-tags').then(r => r.json()).catch(() => []),
		fetch(`/api/series/${series.id}/custom-tags`).then(r => r.json()).catch(() => ({ tag_ids: [] }))
	]).then(([allTags, seriesTags]) => {
		allCustomTagsCache = allTags || [];
		currentSeriesTagIds = seriesTags.tag_ids || [];
		pendingSeriesTagIds = [...currentSeriesTagIds];
		renderTagsList();
		renderTagsSelectorText();
	});

	// Load chapters (existing code)
	fetch(`/api/series/${series.id}/chapters`)
		.then(r => r.json())
		.then(chapters => {
			const select = document.getElementById('edit-current-chapter');
			select.innerHTML = '<option value="-1">Not started</option>';
			const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
			const useVolumeLabels = !hasAnyNullVolume;
			const comparator = useVolumeLabels
				? (a, b) => {
					const volA = getVolumeKey(a.volume);
					const volB = getVolumeKey(b.volume);
					if (volA[0] !== volB[0]) return volA[0] - volB[0];
					if (volA[0] === 1) {
						if (volA[1] !== volB[1]) return volA[1] - volB[1];
					} else {
						if (volA[1] !== volB[1]) return volA[1].localeCompare(volB[1]);
					}
					return a.chapter_number - b.chapter_number;
				}
				: (a, b) => a.chapter_number - b.chapter_number;
			const sortedChapters = [...chapters].sort(comparator);
			const numeric = sortedChapters.filter(ch => !ch.is_oneshot).reverse();
			const oneshots = sortedChapters.filter(ch => ch.is_oneshot);
			function formatLabel(ch) {
				if (ch.is_oneshot) {
					return oneshots.length === 1 ? "Oneshot" : `Oneshot ${oneshots.indexOf(ch) + 1}`;
				}
				if (useVolumeLabels && ch.volume) {
					return `Vol.${ch.volume} Ch.${ch.chapter_number}`;
				}
				return `Ch.${ch.chapter_number}`;
			}
			let hasExactMatch = false;
			numeric.forEach(ch => {
				const opt = document.createElement('option');
				opt.value = ch.chapter_number;
				opt.textContent = formatLabel(ch);
				if (ch.chapter_number === parseFloat(series.current_chapter)) {
					opt.selected = true;
					hasExactMatch = true;
				}
				select.appendChild(opt);
			});
			oneshots.forEach(ch => {
				const opt = document.createElement('option');
				opt.value = ch.chapter_number;
				opt.textContent = formatLabel(ch);
				if (ch.chapter_number === parseFloat(series.current_chapter)) {
					opt.selected = true;
					hasExactMatch = true;
				}
				select.appendChild(opt);
			});

			// The current chapter was set manually (past what's tracked, or
			// into a gap) if it's not "not started" and doesn't match any
			// real tracked chapter -- open straight into manual mode instead
			// of showing a dropdown that can't actually represent it.
			const savedChapter = parseFloat(series.current_chapter);
			if (!isNaN(savedChapter) && savedChapter >= 0 && !hasExactMatch) {
				enterManualChapterMode();
			}

			syncChapterCustomList();
			updateSaveButtonState();
		})
		.catch(err => console.error('Chapter load error:', err));

	document.getElementById('edit-series-modal').classList.remove('hidden');
	document.body.style.overflow = 'hidden';
	// Reset scroll to the top -- .settings-modal keeps whatever scroll
	// position it was left at (most noticeable on mobile's full-screen
	// layout, where it's easy to scroll down to Actions/Delete/Save),
	// so reopening for a different series without this would leave it
	// dropped in wherever the last series' modal happened to be scrolled.
	const editModalContent = document.querySelector('#edit-series-modal .modal-content');
	if (editModalContent) editModalContent.scrollTop = 0;
}

// Effective chapter/volume the Save button would submit, depending on
// whether the modal is in dropdown ('select') or manual-entry mode. The
// dropdown never touches volume (matches its pre-existing behavior), so
// volume changes only ever come from manual mode.
function getPendingChapterAndVolume() {
	if (chapterInputMode === 'manual') {
		return { chapter: manualChapterValue, volume: manualVolumeValue };
	}
	const select = document.getElementById('edit-current-chapter');
	return { chapter: select ? parseFloat(select.value) : NaN, volume: null };
}

// Save button is only actionable once the pending chapter/volume and/or
// title actually differ from what's saved -- nothing to submit otherwise.
// Order-independent set comparison for the two custom-tag id lists.
function tagIdSetsDiffer(a, b) {
	if (a.length !== b.length) return true;
	const sortedA = [...a].sort((x, y) => x - y);
	const sortedB = [...b].sort((x, y) => x - y);
	return sortedA.some((id, i) => id !== sortedB[i]);
}

function updateSaveButtonState() {
	const btn = document.getElementById('btn-save-chapter');
	const heading = document.getElementById('edit-series-title-heading');
	if (!btn) return;

	const { chapter: pendingChapter, volume: pendingVolume } = getPendingChapterAndVolume();
	const chapterChanged = pendingChapter !== (originalSeriesValues?.current_chapter ?? null);
	const volumeChanged = chapterInputMode === 'manual' && pendingVolume !== (originalSeriesValues?.current_volume ?? null);
	const titleChanged = heading && heading.textContent !== (originalSeriesValues?.title || '');
	const coverChanged = pendingCoverUrl !== null && pendingCoverUrl !== (originalSeriesValues?.cover_url || '');
	const statusSelect = document.getElementById('edit-status');
	const statusChanged = statusSelect && statusSelect.value !== (originalSeriesValues?.status || '');
	const tagsChanged = tagIdSetsDiffer(pendingSeriesTagIds, currentSeriesTagIds);
	const primarySourceChanged = pendingPrimarySourceId !== currentPrimarySourceId;

	btn.disabled = !(chapterChanged || volumeChanged || titleChanged || coverChanged || statusChanged || tagsChanged || primarySourceChanged);
}

function formatManualStepperValues() {
	const chapEl = document.getElementById('manual-chapter-value');
	if (chapEl) chapEl.value = String(manualChapterValue);

	const volEl = document.getElementById('manual-volume-value');
	if (volEl) {
		volEl.value = manualVolumeValue === null ? '' : manualVolumeValue;
		volEl.classList.toggle('settings-stepper-value-muted', manualVolumeValue === null);
	}
}

// Seed manual values from the series' actual saved values (only once per
// modal open) rather than reading the dropdown's live selection, since
// the chapter list loads asynchronously and might not be populated yet
// if the user clicks "manually" right after opening the modal.
function seedManualValuesIfNeeded() {
	if (manualValuesSeeded) return;
	const savedChapter = originalSeriesValues?.current_chapter;
	manualChapterValue = typeof savedChapter === 'number' && savedChapter >= 0 ? savedChapter : 0;
	manualVolumeValue = originalSeriesValues?.current_volume || null;
	manualValuesSeeded = true;
}

function enterManualChapterMode() {
	chapterInputMode = 'manual';
	seedManualValuesIfNeeded();
	formatManualStepperValues();
	document.getElementById('chapter-select-group')?.classList.add('hidden');
	document.getElementById('chapter-manual-group')?.classList.remove('hidden');
	const toggle = document.getElementById('chapter-mode-toggle');
	if (toggle) toggle.textContent = 'automatically';
	updateSaveButtonState();
}

function enterSelectChapterMode() {
	chapterInputMode = 'select';
	document.getElementById('chapter-manual-group')?.classList.add('hidden');
	document.getElementById('chapter-select-group')?.classList.remove('hidden');
	const toggle = document.getElementById('chapter-mode-toggle');
	if (toggle) toggle.textContent = 'manually';
	updateSaveButtonState();
}

// Mirrors the hidden #edit-current-chapter <select> (still the source of
// truth -- all the volume/oneshot/matching logic that populates it is
// untouched) into a custom dropdown matching Source/Tags' visual style.
// Called after the select's options are (re)built, and after picking a row.
function syncChapterCustomList() {
	const select = document.getElementById('edit-current-chapter');
	const list = document.getElementById('chapter-select-list');
	const triggerText = document.getElementById('chapter-select-trigger-text');
	if (!select || !list || !triggerText) return;

	// "Not started" is the fallback, not usually what you're scanning a long
	// chapter list for -- sort it to the bottom instead of leaving it first.
	const options = Array.from(select.options);
	const notStarted = options.filter(opt => opt.value === '-1');
	const rest = options.filter(opt => opt.value !== '-1');
	const orderedOptions = [...rest, ...notStarted];

	list.innerHTML = orderedOptions.map(opt => `
		<div class="settings-dropdown-item ${opt.selected ? 'selected' : ''}" data-value="${opt.value}" data-search="${opt.textContent.toLowerCase()}">${opt.textContent}</div>
	`).join('');

	const selectedOption = select.options[select.selectedIndex];
	triggerText.textContent = selectedOption ? selectedOption.textContent : 'Not started';

	list.querySelectorAll('.settings-dropdown-item').forEach(item => {
		item.addEventListener('click', () => {
			select.value = item.dataset.value;
			select.dispatchEvent(new Event('change', { bubbles: true }));
			syncChapterCustomList();
			// closeChapterSelectMenu() lives inside the DOMContentLoaded
			// closure below and isn't reachable from this top-level function.
			document.getElementById('chapter-select-menu')?.classList.add('hidden');
			document.getElementById('chapter-select-trigger')?.classList.remove('open');
		});
	});
}

// Same idea for the hidden #edit-status <select>.
function syncStatusCustomUI() {
	const select = document.getElementById('edit-status');
	const triggerText = document.getElementById('settings-status-selector-text');
	if (!select || !triggerText) return;
	const selectedOption = select.options[select.selectedIndex];
	triggerText.textContent = selectedOption ? selectedOption.textContent : 'Reading';
	document.querySelectorAll('#settings-status-list .settings-dropdown-item').forEach(item => {
		item.classList.toggle('selected', item.dataset.value === select.value);
	});
}

// ─── Source Management Functions ─────────────────────────────
let pendingSourceChanges = {
	hasChanges: false,
	primarySourceId: null,
	originalPrimaryId: null
};

async function loadSeriesSources(seriesId) {
	try {
		const res = await fetch(`/api/series/${seriesId}/sources`);
		if (!res.ok) throw new Error('Failed to load sources');

		const data = await res.json();
		const container = document.getElementById('sources-list');

		if (data.sources.length === 0) {
			container.innerHTML = '<p class="loading-text">No sources found.</p>';
			return;
		}

		// Store original primary source
		const primarySource = data.sources.find(s => s.is_primary);
		pendingSourceChanges.originalPrimaryId = primarySource ? primarySource.id : null;
		pendingSourceChanges.primarySourceId = pendingSourceChanges.originalPrimaryId;
		pendingSourceChanges.hasChanges = false;

		renderSources(data.sources);
		initializeDragAndDrop();

	} catch (err) {
		console.error('Failed to load sources:', err);
		document.getElementById('sources-list').innerHTML =
			'<p class="loading-text">Error loading sources.</p>';
	}
}

function renderSources(sources) {
  const container = document.getElementById('sources-list');

  // Sort so primary is always first
  const sortedSources = [...sources].sort((a, b) => {
    if (a.id === pendingSourceChanges.primarySourceId) return -1;
    if (b.id === pendingSourceChanges.primarySourceId) return 1;
    return 0;
  });

  container.innerHTML = sortedSources.map(source => {
		// FIXED: Always use source.source_type directly (don't read from DOM)
		const sourceTypeLabel = {
			'mangadex': 'MangaDex',
			'kagane': 'Kagane',
			'atsu': 'Atsumaru',
			'asura': 'AsuraScans',
			'hive': 'HiveToons',
			'unknown': 'Unknown'
		}[source.source_type.toLowerCase()] || source.source_type;

		const isPrimary = source.id === pendingSourceChanges.primarySourceId;

    return `
      <div class="source-item ${isPrimary ? 'primary' : ''}" data-source-id="${source.id}" data-source-type="${escapeHtml(source.source_type)}" draggable="true">
        <div class="source-drag-handle">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="9" cy="5" r="1.5"/>
            <circle cx="9" cy="12" r="1.5"/>
            <circle cx="9" cy="19" r="1.5"/>
            <circle cx="15" cy="5" r="1.5"/>
            <circle cx="15" cy="12" r="1.5"/>
            <circle cx="15" cy="19" r="1.5"/>
          </svg>
        </div>
        <div class="source-info">
          <div class="source-type">
            ${escapeHtml(sourceTypeLabel)}
            ${isPrimary ? '<span class="primary-badge">PRIMARY</span>' : ''}
          </div>
          <div class="source-url">${escapeHtml(source.source_url)}</div>
        </div>
        <div class="source-actions">
          <button class="btn-icon" data-action="open" data-url="${escapeHtml(source.source_url)}" title="Open Source">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
            </svg>
          </button>
          ${!isPrimary && sortedSources.length > 1 ? `
            <button class="btn-icon danger" onclick="removeSource(${currentSeriesIdForEdit}, ${source.id})" title="Remove Source">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
              </svg>
            </button>
          ` : ''}
        </div>
      </div>
    `;
	}).join('');

	container.querySelectorAll('[data-action="open"]').forEach(btn => {
		btn.addEventListener('click', () => {
			if (isSafeUrl(btn.dataset.url)) window.open(btn.dataset.url, '_blank');
		});
	});
}

function initializeDragAndDrop() {
	const container = document.getElementById('sources-list');
	if (!container) return;

	let draggedElement = null;

	container.addEventListener('dragstart', (e) => {
		if (e.target.classList.contains('source-item')) {
			draggedElement = e.target;
			e.target.classList.add('dragging');
		}
	});

	container.addEventListener('dragend', (e) => {
		if (e.target.classList.contains('source-item')) {
			e.target.classList.remove('dragging');
			draggedElement = null;
		}
	});

	container.addEventListener('dragover', (e) => {
		e.preventDefault();
		const afterElement = getDragAfterElement(container, e.clientY);
		const dragging = document.querySelector('.dragging');

		if (afterElement == null) {
			container.appendChild(dragging);
		} else {
			container.insertBefore(dragging, afterElement);
		}
	});

	container.addEventListener('drop', (e) => {
		e.preventDefault();

		// Get the new order and mark the first as primary
		const sourceItems = container.querySelectorAll('.source-item:not(.sources-help-text)');
		if (sourceItems.length > 0) {
			const newPrimaryId = parseInt(sourceItems[0].dataset.sourceId);

			if (newPrimaryId !== pendingSourceChanges.primarySourceId) {
				pendingSourceChanges.primarySourceId = newPrimaryId;
				pendingSourceChanges.hasChanges = true;

				// Re-render to update visual state
				const currentSources = Array.from(sourceItems).map(item => ({
				id: parseInt(item.dataset.sourceId),
				source_type: item.dataset.sourceType, // CHANGED: Read from data attribute
				source_url: item.querySelector('.source-url').textContent,
				is_primary: parseInt(item.dataset.sourceId) === newPrimaryId
				}));

				renderSources(currentSources);
				initializeDragAndDrop(); // Re-initialize after render
			}
		}
	});
}

function getDragAfterElement(container, y) {
	const draggableElements = [...container.querySelectorAll('.source-item:not(.dragging)')];

	return draggableElements.reduce((closest, child) => {
		const box = child.getBoundingClientRect();
		const offset = y - box.top - box.height / 2;

		if (offset < 0 && offset > closest.offset) {
			return { offset: offset, element: child };
		} else {
			return closest;
		}
	}, { offset: Number.NEGATIVE_INFINITY }).element;
}

async function saveSourceChanges(seriesId) {
	if (!pendingSourceChanges.hasChanges) return;

	try {
		const res = await fetch(`/api/series/${seriesId}/sources/${pendingSourceChanges.primarySourceId}/primary`, {
			method: 'POST'
		});

		if (res.ok) {
		// ADDED: Show notification for source removed
		const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
		showNotification(`Source removed from ${seriesTitle}`, 'source_removed');
		await loadSeriesSources(seriesId);
		} else {
		const data = await res.json();
		showNotification('Failed to remove source: ' + (data.error || 'Unknown error'), 'error'); // CHANGED
		}
	} catch (err) {
		console.error('Failed to save source changes:', err);
		throw err;
	}
}

async function removeSource(seriesId, sourceId) {
	if (!confirm('Remove this source? Chapters from this source will remain but won\'t be updated.')) {
		return;
	}

	try {
		const res = await fetch(`/api/series/${seriesId}/sources/${sourceId}`, {
			method: 'DELETE'
		});

		if (res.ok) {
			// ADDED: Show notification for source removed
			const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
			showNotification(`Source removed from ${seriesTitle}`, 'source_removed');
			await loadSeriesSources(seriesId);
		} else {
			const data = await res.json();
			showNotification('Failed to remove source: ' + (data.error || 'Unknown error'), 'error'); // CHANGED
		}
	} catch (err) {
		console.error('Failed to remove source:', err);
		showNotification('Error: ' + err.message, 'error');
	}
}

function showAddSourceForm() {
	document.getElementById('add-source-form').classList.remove('hidden');
	document.getElementById('new-source-url').focus();
}

function hideAddSourceForm() {
	document.getElementById('add-source-form').classList.add('hidden');
	document.getElementById('new-source-url').value = '';
}

async function addNewSource() {
	const url = document.getElementById('new-source-url').value.trim();

	if (!url) {
		alert('Please enter a source URL');
		return;
	}

	// Validate URL — NOTE: fixed extra spaces in comparison
	if (!url.startsWith('https://mangadex.org/') && !url.startsWith('https://kagane.to/') && !url.startsWith('https://kagane.org/') && !url.startsWith('https://atsu.moe/') && !url.startsWith('https://asurascans.com/comics/') && !url.startsWith('https://hivetoons.org/series/')) {
		alert('Only MangaDex, Kagane, Atsumaru, AsuraScans, and HiveToons sources are supported');
		return;
	}

	// Check if source already exists in the UI
	const existingSources = document.querySelectorAll('.source-item');
	for (const sourceItem of existingSources) {
		const existingUrl = sourceItem.querySelector('.source-url')?.textContent?.trim();
		if (existingUrl === url) {
			showNotification('Source already exists', 'error');
			return;
		}
	}

	try {
		const res = await fetch(`/api/series/${currentSeriesIdForEdit}/sources`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ source_url: url })
		});

		if (res.ok) {
			// ADDED: Show notification for source added
			const seriesTitle = document.getElementById('edit-title')?.value || 'Series';
			showNotification(`Source added to ${seriesTitle}`, 'source_added');
			hideAddSourceForm();
			await loadSeriesSources(currentSeriesIdForEdit);
		} else {
			const data = await res.json().catch(() => ({}));
			const errorMsg = data.error || 'Unknown error';
			
			// Since backend doesn't give specific errors, provide helpful message
			if (res.status === 500) {
				showNotification('Failed to add source - please check if it already exists or try again', 'error');
			} else if (errorMsg.toLowerCase().includes('already exists') || errorMsg.toLowerCase().includes('duplicate')) {
				showNotification('Source already exists', 'error');
			} else {
				showNotification(errorMsg, 'error');
			}
		}
	} catch (err) {
		console.error('Failed to add source:', err);
		showNotification('Network error - please try again', 'error');
	}
}

// ─── Series Settings modal: Source selector (Kenmei-style dropdown,
// plus add/remove/set-primary which Kenmei doesn't need to support) ──
const SOURCE_TYPE_LABELS = {
	mangadex: 'MangaDex', kagane: 'Kagane', atsu: 'Atsumaru', asura: 'AsuraScans', hive: 'HiveToons', unknown: 'Unknown'
};

function renderSourceSelector(sources) {
	const dot = document.getElementById('settings-source-dot');
	const nameEl = document.getElementById('settings-source-name');
	const chapEl = document.getElementById('settings-source-chapter');
	if (!dot || !nameEl || !chapEl) return;

	if (!sources || sources.length === 0) {
		dot.classList.add('inactive');
		nameEl.textContent = 'No source';
		chapEl.textContent = '';
		return;
	}

	const primary = sources.find(s => s.is_primary) || sources[0];
	dot.classList.toggle('inactive', !primary.is_primary);
	nameEl.textContent = SOURCE_TYPE_LABELS[primary.source_type] || primary.source_type;
	chapEl.textContent = (currentSeriesLatestChapter !== null && currentSeriesLatestChapter !== undefined)
		? `Ch. ${currentSeriesLatestChapter}` : '';
}

function renderSourceList(sources) {
	const list = document.getElementById('settings-source-list');
	if (!list) return;

	if (!sources || sources.length === 0) {
		list.innerHTML = '<p class="settings-cover-menu-empty">No sources linked.</p>';
		return;
	}

	list.innerHTML = sources.map(s => {
		const label = SOURCE_TYPE_LABELS[s.source_type] || s.source_type;
		return `
			<div class="settings-source-item">
				<span class="settings-source-dot ${s.is_primary ? '' : 'inactive'}"></span>
				<span class="settings-source-item-name">${escapeHtml(label)}</span>
				${s.is_primary ? '<span class="settings-source-item-badge">PRIMARY</span>' : ''}
				<div class="settings-source-item-actions">
					${!s.is_primary ? `
						<button type="button" class="btn-icon" data-action="primary" data-source-id="${s.id}" title="Set as primary">
							<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
								<path d="M12 2l3.09 6.26L22 9.27l-5 4.87L18.18 21 12 17.77 5.82 21 7 14.14l-5-4.87 6.91-1.01L12 2z"/>
							</svg>
						</button>
					` : ''}
					<button type="button" class="btn-icon" data-action="open" data-url="${escapeHtml(s.source_url)}" title="Open source">
						<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
						</svg>
					</button>
					${!s.is_primary ? `
						<button type="button" class="btn-icon danger" data-action="remove" data-source-id="${s.id}" title="Remove source">
							<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
								<path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
							</svg>
						</button>
					` : ''}
				</div>
			</div>
		`;
	}).join('');

	list.querySelectorAll('[data-action="open"]').forEach(btn => {
		btn.addEventListener('click', (e) => {
			e.stopPropagation();
			if (isSafeUrl(btn.dataset.url)) window.open(btn.dataset.url, '_blank');
		});
	});
	list.querySelectorAll('[data-action="primary"]').forEach(btn => {
		btn.addEventListener('click', (e) => {
			e.stopPropagation();
			setSeriesSourceAsPrimary(btn.dataset.sourceId);
		});
	});
	list.querySelectorAll('[data-action="remove"]').forEach(btn => {
		btn.addEventListener('click', (e) => {
			e.stopPropagation();
			const row = btn.closest('.settings-source-item');
			if (row) showRemoveSourceConfirm(row, btn.dataset.sourceId);
		});
	});
}

// Swaps a source row into an inline "Remove this source? Cancel / Remove"
// confirm state instead of removing on the first click - matches the app's
// avoidance of native confirm() elsewhere (e.g. Manage Views' delete confirm).
function showRemoveSourceConfirm(row, sourceId) {
	row.innerHTML = `
		<span class="settings-source-confirm-text">Remove this source?</span>
		<div class="settings-source-confirm-actions">
			<button type="button" class="bookmark-form-btn bookmark-form-cancel settings-source-cancel-remove">Cancel</button>
			<button type="button" class="bookmark-form-btn bookmark-form-delete-confirm settings-source-confirm-remove">Remove</button>
		</div>
	`;
	row.querySelector('.settings-source-cancel-remove')?.addEventListener('click', async (e) => {
		e.stopPropagation();
		renderSourceList(currentSeriesSourcesPromise ? await currentSeriesSourcesPromise : []);
	});
	row.querySelector('.settings-source-confirm-remove')?.addEventListener('click', (e) => {
		e.stopPropagation();
		removeSeriesSource(sourceId);
	});
}

async function refreshSourcesUI() {
	if (!currentSeriesIdForEdit) return;
	try {
		const res = await fetch(`/api/series/${currentSeriesIdForEdit}/sources`);
		const data = await res.json();
		const sources = data.sources || [];
		currentSeriesSourcesPromise = Promise.resolve(sources);
		// Called after an immediate action (add/remove source) commits, so
		// this is a fresh server-truth baseline -- drop any staged primary
		// pick rather than risk it pointing at a source that's now gone.
		const primary = sources.find(s => s.is_primary);
		currentPrimarySourceId = primary ? primary.id : null;
		pendingPrimarySourceId = currentPrimarySourceId;
		renderSourceSelector(sources);
		renderSourceList(sources);
		updateSaveButtonState();
	} catch (e) {
		showNotification('Failed to refresh sources', 'error');
	}
}

// Stages which source is primary, same as picking a status/tag/cover --
// only actually commits when Save is clicked (see the btn-save-chapter
// handler). Used to hit the API immediately, inconsistent with every
// other field in this modal.
async function setSeriesSourceAsPrimary(sourceId) {
	if (!currentSeriesIdForEdit || !currentSeriesSourcesPromise) return;
	pendingPrimarySourceId = parseInt(sourceId, 10);
	const sources = await currentSeriesSourcesPromise;
	// The API always returns the primary source first, so that's the order
	// the list re-appears in once Save actually commits this and the page
	// reloads -- sort here too so the pending primary jumps to the top
	// immediately instead of only reordering once saved (it would just
	// swap the PRIMARY badge onto whichever position the click was on,
	// otherwise -- correct end state, but the list visibly "waiting on
	// Save" to reorder looked like the click hadn't really registered).
	const withPendingPrimary = sources
		.map(s => ({ ...s, is_primary: s.id === pendingPrimarySourceId }))
		.sort((a, b) => (b.is_primary ? 1 : 0) - (a.is_primary ? 1 : 0));
	renderSourceSelector(withPendingPrimary);
	renderSourceList(withPendingPrimary);
	updateSaveButtonState();
}

async function removeSeriesSource(sourceId) {
	if (!currentSeriesIdForEdit) return;
	try {
		const res = await fetch(`/api/series/${currentSeriesIdForEdit}/sources/${sourceId}`, { method: 'DELETE' });
		if (res.ok) {
			await refreshSourcesUI();
			showNotification('Source removed', 'source_removed');
			loadPage();
		} else {
			const data = await res.json().catch(() => ({}));
			showNotification(data.error || 'Failed to remove source', 'error');
		}
	} catch (e) {
		showNotification('Failed to remove source', 'error');
	}
}

async function addSeriesSource(url) {
	if (!currentSeriesIdForEdit || !url) return;
	try {
		const res = await fetch(`/api/series/${currentSeriesIdForEdit}/sources`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ source_url: url })
		});
		const data = await res.json().catch(() => ({}));
		if (res.ok) {
			await refreshSourcesUI();
			showNotification('Source added', 'source_added');
			loadPage();
		} else {
			showNotification(data.error || 'Failed to add source', 'error');
		}
	} catch (e) {
		showNotification('Failed to add source', 'error');
	}
}

// ─── Series Settings modal: custom Tags picker ───────────────────
function renderTagsSelectorText() {
	const textEl = document.getElementById('settings-tags-selector-text');
	if (!textEl) return;
	const names = pendingSeriesTagIds
		.map(id => allCustomTagsCache.find(t => t.id === id)?.name)
		.filter(Boolean);

	if (names.length === 0) {
		textEl.textContent = 'Choose a tag';
		textEl.classList.add('settings-tags-selector-muted');
	} else if (names.length === 1) {
		textEl.textContent = names[0];
		textEl.classList.remove('settings-tags-selector-muted');
	} else {
		textEl.textContent = `${names[0]} +${names.length - 1}`;
		textEl.classList.remove('settings-tags-selector-muted');
	}
}

function renderTagsList() {
	const list = document.getElementById('settings-tags-list');
	if (!list) return;

	if (allCustomTagsCache.length === 0) {
		list.innerHTML = '<p class="settings-cover-menu-empty">No tags yet -- create one below.</p>';
		return;
	}

	list.innerHTML = allCustomTagsCache.map(t => {
		const checked = pendingSeriesTagIds.includes(t.id);
		return `
			<div class="settings-tag-item ${checked ? 'checked' : ''}" data-tag-id="${t.id}">
				<span class="settings-tag-checkbox">
					${checked ? '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>' : ''}
				</span>
				<span class="settings-tag-item-name">${escapeHtml(t.name)}</span>
				<button type="button" class="btn-icon danger settings-tag-delete" data-tag-id="${t.id}" title="Delete tag">
					<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
					</svg>
				</button>
			</div>
		`;
	}).join('');

	list.querySelectorAll('.settings-tag-item').forEach(item => {
		item.addEventListener('click', (e) => {
			if (e.target.closest('.settings-tag-delete')) return;
			toggleSeriesCustomTag(parseInt(item.dataset.tagId, 10));
		});
	});

	list.querySelectorAll('.settings-tag-delete').forEach(btn => {
		btn.addEventListener('click', async (e) => {
			e.stopPropagation();
			const tagId = parseInt(btn.dataset.tagId, 10);
			const tag = allCustomTagsCache.find(t => t.id === tagId);
			if (!confirm(`Delete the tag "${tag ? tag.name : ''}"? This removes it from every series, not just this one.`)) return;

			try {
				const res = await fetch(`/api/custom-tags/${tagId}`, { method: 'DELETE' });
				if (res.ok) {
					allCustomTagsCache = allCustomTagsCache.filter(t => t.id !== tagId);
					// Deleting the tag globally is immediate and independent of
					// Save -- scrub it from both the baseline and the staged
					// selection so it can't linger as a false "pending change".
					currentSeriesTagIds = currentSeriesTagIds.filter(id => id !== tagId);
					pendingSeriesTagIds = pendingSeriesTagIds.filter(id => id !== tagId);
					renderTagsList();
					renderTagsSelectorText();
					updateSaveButtonState();
					if (typeof loadCustomTagsFilterSection === 'function') loadCustomTagsFilterSection();
					if (typeof loadMobileCustomTagsFilterSection === 'function') loadMobileCustomTagsFilterSection();
				} else {
					showNotification('Failed to delete tag', 'error');
				}
			} catch (err) {
				showNotification('Failed to delete tag', 'error');
			}
		});
	});
}

// Selecting/deselecting which tags apply to this series only stages the
// change locally (like title/chapter) -- committed on Save, unlike tag
// creation/deletion which are immediate global actions.
function toggleSeriesCustomTag(tagId) {
	pendingSeriesTagIds = pendingSeriesTagIds.includes(tagId)
		? pendingSeriesTagIds.filter(id => id !== tagId)
		: [...pendingSeriesTagIds, tagId];
	renderTagsList();
	renderTagsSelectorText();
	updateSaveButtonState();
}

async function createAndApplyCustomTag(name) {
	if (!currentSeriesIdForEdit || !name) return;
	try {
		const res = await fetch('/api/custom-tags', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ name })
		});
		const data = await res.json().catch(() => ({}));
		if (res.ok) {
			if (!allCustomTagsCache.find(t => t.id === data.id)) {
				allCustomTagsCache = [...allCustomTagsCache, { id: data.id, name: data.name }]
					.sort((a, b) => a.name.localeCompare(b.name));
			}
			// The tag itself is created immediately, but applying it to this
			// series is staged like any other tag selection -- Save required.
			toggleSeriesCustomTag(data.id);
			if (typeof loadCustomTagsFilterSection === 'function') loadCustomTagsFilterSection();
			if (typeof loadMobileCustomTagsFilterSection === 'function') loadMobileCustomTagsFilterSection();
		} else {
			showNotification(data.error || 'Failed to create tag', 'error');
		}
	} catch (err) {
		showNotification('Failed to create tag', 'error');
	}
}

// ─── Skeleton Card Creation ──────────────────────────────────────
// Mobile cards hide .card-info entirely (title is overlaid on the cover
// instead, see renderSeriesCard) -- skip that content block here too, or
// the skeleton shows a block of lines the real card never has on mobile,
// and the whole area collapses away the instant real data loads in.
function createSkeletonCard() {
	const skeleton = document.createElement('div');
	skeleton.className = 'skeleton-card';
	skeleton.innerHTML = isMobileDevice() ? `
		<div class="skeleton-cover"></div>
	` : `
		<div class="skeleton-cover"></div>
		<div class="skeleton-content">
			<div class="skeleton-line title"></div>
			<div class="skeleton-line subtitle"></div>
			<div class="skeleton-line buttons"></div>
			<div class="skeleton-line btn-bottom"></div>
		</div>
	`;
	return skeleton;
}

// ─── Lazy Loading Image Observer ─────────────────────────────────
const imageObserver = new IntersectionObserver((entries) => {
	entries.forEach(entry => {
		if (entry.isIntersecting) {
			const img = entry.target;
			const src = img.dataset.src;
			if (src) {
				img.classList.add('loading');
				img.src = src;
				img.onload = () => {
					img.classList.remove('loading');
					img.classList.add('loaded');
				};
				img.onerror = () => {
					img.classList.remove('loading');
					img.src = '/static/placeholder.png';
				};
				imageObserver.unobserve(img);
			}
		}
	});
}, {
	rootMargin: '50px' // Start loading 50px before entering viewport
});

// ─── Card Rendering ──────────────────────────────────────────
function renderSeriesCard(series, chapters = null) {
	const initialCurrent = parseFloat(series.current_chapter);
	const isNotStarted = (initialCurrent === -1);
	const card = document.createElement('div');
	card.className = 'series-card fade-in';
	card.dataset.seriesId = series.id;
	let releaseText = '';
	if (series.latest_release) {
		const releaseDate = new Date(series.latest_release);
		const diffMs = Date.now() - releaseDate.getTime();
		const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
		const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
		if (diffDays > 0) releaseText = `${diffDays}d ago`;
		else if (diffHours > 0) releaseText = `${diffHours}h ago`;
		else releaseText = 'Just now';
	}
	const cleanCoverUrl = (series.cover_protected_url || series.cover_url || '/static/placeholder.png').replace(/\s+/g, '');
	const placeholderUrl = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 221 331"%3E%3Crect fill="%231a2436" width="221" height="331"/%3E%3C/svg%3E';
	card.innerHTML = `
<div class="series-cover-container">
<div class="series-checkbox">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
</svg>
</div>
<img class="series-cover loading" src="${placeholderUrl}" data-src="${cleanCoverUrl}" loading="lazy" referrerpolicy="no-referrer" onerror="this.src='/static/placeholder.png'">
${releaseText ? `<div class="last-release">${releaseText}</div>` : ''}
${isMobileDevice() ? `<div class="mobile-card-title"><span>${escapeHtml(series.title)}</span></div>` : ''}
</div>
<div class="card-info">
<div class="card-title">${escapeHtml(series.title)}</div>
<div class="card-chapters">
<span class="chapter-not-started">Loading...</span>
<span class="chapter-not-started">Loading...</span>
</div>
<div class="card-button-groups">
<div class="button-group single-group">
<button class="btn-set" title="Settings">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
</svg>
</button>
<button class="btn-search-google" title="Search Next Chapter">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<circle cx="12" cy="12" r="10"></circle>
<path d="M2 12h20"></path>
<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
</svg>
</button>
<button class="btn-source" title="Go to Source">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M15 3h6v6"></path>
<path d="M10 14 21 3"></path>
<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
</svg>
</button>
<button class="btn-dec" title="Previous">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M5 12h14"></path>
</svg>
</button>
<button class="btn-inc" title="Next">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M5 12h14"></path>
<path d="M12 5v14"></path>
</svg>
</button>
<button class="btn-accept" title="Confirm">
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M18 6 7 17l-5-5"></path>
<path d="m22 10-7.5 7.5L13 16"></path>
</svg>
</button>
</div>
</div>
<div class="card-buttons-bottom">
<button class="btn-next">Loading...</button>
</div>
</div>
`;
	const btnSet = card.querySelector('.btn-set');
	const btnSearchGoogle = card.querySelector('.btn-search-google');
	const btnSource = card.querySelector('.btn-source');
	const btnDec = card.querySelector('.btn-dec');
	const btnInc = card.querySelector('.btn-inc');
	const btnAccept = card.querySelector('.btn-accept');
	const btnNext = card.querySelector('.btn-next');

	function makeChapterComparator(useVolume) {
		return (a, b) => {
			if (useVolume) {
				const volA = getVolumeKey(a.volume);
				const volB = getVolumeKey(b.volume);
				if (volA[0] !== volB[0]) return volA[0] - volB[0];
				if (volA[0] === 1) {
					if (volA[1] !== volB[1]) return volA[1] - volB[1];
				} else {
					if (volA[1] !== volB[1]) return volA[1].localeCompare(volB[1]);
				}
				return a.chapter_number - b.chapter_number;
			} else {
				return a.chapter_number - b.chapter_number;
			}
		};
	}

	// Chapter/source URLs come from tracked external sources, not from the
	// user -- reject anything that isn't a plain http(s) link (e.g. a
	// javascript: URI) before it ever reaches an href, on top of escaping it
	// for the HTML attribute itself.
	function safeHref(url) {
		if (isSafeUrl(url)) {
			return escapeHtml(url);
		}
		return '#';
	}

	function formatChapterLabel(chapterData, useVolume) {
		if (chapterData.is_oneshot) {
			return 'Oneshot';
		}
		if (useVolume && chapterData.volume) {
			return `Ch.${chapterData.chapter_number} (Vol.${escapeHtml(chapterData.volume)})`;
		}
		return `Ch.${chapterData.chapter_number}`;
	}

	(async () => {
		try {
			// Use provided chapters or fetch if not available
			let chaptersData;
			if (chapters !== null) {
				chaptersData = chapters;
			} else {
				const res = await fetch(`/api/series/${series.id}/chapters`);
				chaptersData = res.ok ? await res.json() : [];
			}
			
			const hasAnyNullVolume = chaptersData.some(ch => ch.volume == null || ch.volume === '');
			const useVolumeSorting = !hasAnyNullVolume;
			const comparator = makeChapterComparator(useVolumeSorting);
			const sortedChapters = [...chaptersData].sort(comparator);
			card.sortedChapters = sortedChapters;
			card.useVolumeSorting = useVolumeSorting;
			let pendingIndex = -1;
			let pendingChapterNumber = null;
			let pendingHasExactMatch = false;
			if (isNotStarted) {
			pendingIndex = -1;
			} else {
				const targetNum = initialCurrent;
				pendingChapterNumber = targetNum;
				const matches = sortedChapters
					.map((ch, idx) => ({ ch, idx }))
					.filter(item => item.ch.chapter_number === targetNum);
				if (matches.length === 0) {
					// No chapter tracked with this exact number -- happens when
					// current_chapter was set manually ahead of (or into a gap
					// in) what's actually been scraped. Fall back to whichever
					// tracked chapter is the highest one at or below the target,
					// so the card reads as "caught up" instead of "not started"
					// until a chapter past the target actually gets tracked.
					// pendingHasExactMatch stays false so updateChapterDisplay()
					// knows to show the literal manually-set number instead of
					// whatever chapter this fallback index actually points to.
					let fallbackIdx = -1;
					let fallbackChapterNum = -Infinity;
					sortedChapters.forEach((ch, idx) => {
						if (ch.chapter_number <= targetNum && ch.chapter_number >= fallbackChapterNum) {
							fallbackChapterNum = ch.chapter_number;
							fallbackIdx = idx;
						}
					});
					pendingIndex = fallbackIdx;
				} else if (matches.length === 1) {
					pendingIndex = matches[0].idx;
					pendingHasExactMatch = true;
				} else {
					pendingHasExactMatch = true;
					if (useVolumeSorting) {
						const best = matches.reduce((a, b) => {
							const volA = a.ch.volume ? parseFloat(a.ch.volume) || 0 : 0;
							const volB = b.ch.volume ? parseFloat(b.ch.volume) || 0 : 0;
							return volB > volA ? b : a;
						});
						pendingIndex = best.idx;
					} else {
						pendingIndex = matches[0].idx;
					}
				}
			}
			card.pendingIndex = pendingIndex;
			card.originalIndex = pendingIndex;
			card.pendingChapterNumber = pendingChapterNumber;
			card.pendingHasExactMatch = pendingHasExactMatch;
			updateUnreadBadge();
			updateChapterDisplay();
			updateButtonState();
		} catch (e) {
			console.error('Chapter fetch failed:', e);
			card.sortedChapters = [];
			card.useVolumeSorting = false;
			card.pendingIndex = -1;
			updateChapterDisplay();
			updateButtonState();
		}
	})();

	function updateUnreadBadge() {
		const sorted = card.sortedChapters || [];
		const pendingIndex = card.pendingIndex;
		const coverContainer = card.querySelector('.series-cover-container');
		const existingBadge = coverContainer.querySelector('.unread-badge');
		if (existingBadge) existingBadge.remove();
		let unreadCount = 0;
		if (sorted.length > 0) {
			const numeric = sorted.filter(ch => !ch.is_oneshot);
			if (numeric.length > 0) {
				if (pendingIndex === -1) {
					unreadCount = numeric.length;
				} else {
					unreadCount = sorted.slice(pendingIndex + 1).filter(ch => !ch.is_oneshot).length;
				}
			} else if (pendingIndex === -1) {
				unreadCount = sorted.length;
			}
		}
		if (unreadCount > 0) {
			const badge = document.createElement('div');
			badge.className = 'unread-badge';
			badge.textContent = String(unreadCount);
			coverContainer.insertBefore(badge, coverContainer.firstChild);
		}
	}

	function updateChapterDisplay() {
		const sorted = card.sortedChapters || [];
		const useVolume = card.useVolumeSorting;
		// A manually-set chapter with no exact tracked match still shows its
		// literal number (not the fallback index's chapter, and not "Not
		// started") as long as the card is still showing that original state
		// -- once the user starts clicking +/-, they're browsing the real
		// tracked list and normal index-based display takes back over.
		const showingManualFallback = card.pendingChapterNumber != null
			&& !card.pendingHasExactMatch
			&& card.pendingIndex === card.originalIndex;
		const isNowNotStarted = (card.pendingIndex === -1) && !showingManualFallback;
		let currentHtml;
		if (showingManualFallback) {
			currentHtml = `<span class="chapter-link" title="Manually set -- not yet tracked from a source">Ch.${card.pendingChapterNumber}</span>`;
		} else if (isNowNotStarted) {
			currentHtml = `<span class="chapter-not-started">Not started</span>`;
		} else {
			const currentCh = sorted[card.pendingIndex];
			if (currentCh) {
				const label = formatChapterLabel(currentCh, useVolume);
				currentHtml = `<a href="${safeHref(currentCh.chapter_url)}" target="_blank" class="chapter-link">${label}</a>`;
			} else {
				currentHtml = `<span class="chapter-not-started">Ch.? (invalid)</span>`;
			}
		}
		let latestHtml;
		if (sorted.length === 0) {
			latestHtml = `<span class="chapter-not-started">Ch. ?</span>`;
		} else {
			const latestCh = sorted[sorted.length - 1];
			const label = formatChapterLabel(latestCh, useVolume);
			latestHtml = `<a href="${safeHref(latestCh.chapter_url)}" target="_blank" class="chapter-link">${label}</a>`;
		}
		card.querySelector('.card-chapters').innerHTML = `${currentHtml}${latestHtml}`;
		
		// FIX: Get the button container and replace the entire button
		const buttonContainer = card.querySelector('.card-buttons-bottom');
		if (!buttonContainer) return; // Safety check
		
		const oldBtn = buttonContainer.querySelector('.btn-next');
		if (!oldBtn) return; // Safety check
		
		// Create new button element
		const newBtn = document.createElement('button');
		newBtn.className = 'btn-next';

		// Avoid the autoscroll cursor on middle-click eating the auxclick
		// handlers below (added once here since it applies to both branches).
		newBtn.addEventListener('mousedown', (e) => {
			if (e.button === 1) e.preventDefault();
		});

		if (sorted.length === 0) {
			newBtn.textContent = 'No chapters';
			newBtn.disabled = true;
		} else if (isNowNotStarted) {
			const firstToRead = sorted[0];
			const label = formatChapterLabel(firstToRead, useVolume);
			newBtn.textContent = `Continue to ${label}`;
			newBtn.disabled = false;
			
			// Handle left-click
			newBtn.addEventListener('click', (e) => {
				e.preventDefault();
				if (isSafeUrl(firstToRead.chapter_url)) window.open(firstToRead.chapter_url, '_blank');
			});

			// Handle middle-click
			newBtn.addEventListener('auxclick', (e) => {
				if (e.button === 1) {
					e.preventDefault();
					if (isSafeUrl(firstToRead.chapter_url)) window.open(firstToRead.chapter_url, '_blank');
				}
			});
		} else {
			if (card.pendingIndex < sorted.length - 1) {
				const nextCh = sorted[card.pendingIndex + 1];
				const label = formatChapterLabel(nextCh, useVolume);
				newBtn.textContent = `Continue to ${label}`;
				newBtn.disabled = false;
				
				// Handle left-click
				newBtn.addEventListener('click', (e) => {
					e.preventDefault();
					if (isSafeUrl(nextCh.chapter_url)) window.open(nextCh.chapter_url, '_blank');
				});

				// Handle middle-click
				newBtn.addEventListener('auxclick', (e) => {
					if (e.button === 1) {
						e.preventDefault();
						if (isSafeUrl(nextCh.chapter_url)) window.open(nextCh.chapter_url, '_blank');
					}
				});
			} else {
				newBtn.textContent = 'No new chapter';
				newBtn.disabled = true;
			}
		}
		
		// Replace the button
		buttonContainer.replaceChild(newBtn, oldBtn);
	}

	function updateButtonState() {
		const hasChanged = card.pendingIndex !== card.originalIndex;
		btnAccept.disabled = !hasChanged;
	}

	// Hold-to-repeat functionality
	let holdInterval = null;
	let holdTimeout = null;

	function startHoldRepeat(callback, initialDelay = 500, repeatInterval = 100) {
	// Clear any existing intervals
	if (holdInterval) clearInterval(holdInterval);
	if (holdTimeout) clearTimeout(holdTimeout);
	
	// Wait for initial delay, then start repeating
	holdTimeout = setTimeout(() => {
		callback(); // Execute first time after delay
		holdInterval = setInterval(callback, repeatInterval);
	}, initialDelay);
	}

	function stopHoldRepeat() {
	if (holdInterval) {
		clearInterval(holdInterval);
		holdInterval = null;
	}
	if (holdTimeout) {
		clearTimeout(holdTimeout);
		holdTimeout = null;
	}
	}

	// REPLACE the existing btnDec.addEventListener('click', ...) with:
	btnDec.addEventListener('mousedown', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		if (card.pendingIndex === -1) {
		// Already at "Not started", do nothing
		} else if (card.pendingIndex === 0) {
		card.pendingIndex = -1;
		} else {
		card.pendingIndex--;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	// Add click handler for single clicks
	btnDec.addEventListener('click', (e) => {
	e.preventDefault();
	if (card.pendingIndex === -1) {
		// Already at "Not started", do nothing
	} else if (card.pendingIndex === 0) {
		card.pendingIndex = -1;
	} else {
		card.pendingIndex--;
	}
	updateChapterDisplay();
	updateButtonState();
	});

	btnDec.addEventListener('mouseup', stopHoldRepeat);
	btnDec.addEventListener('mouseleave', stopHoldRepeat);

	// REPLACE the existing btnInc.addEventListener('click', ...) with:
	btnInc.addEventListener('mousedown', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		const sorted = card.sortedChapters || [];
		if (sorted.length === 0) return;
		if (card.pendingIndex === -1) {
		card.pendingIndex = 0;
		} else if (card.pendingIndex < sorted.length - 1) {
		card.pendingIndex++;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	// Add click handler for single clicks
	btnInc.addEventListener('click', (e) => {
	e.preventDefault();
	const sorted = card.sortedChapters || [];
	if (sorted.length === 0) return;
	if (card.pendingIndex === -1) {
		card.pendingIndex = 0;
	} else if (card.pendingIndex < sorted.length - 1) {
		card.pendingIndex++;
	}
	updateChapterDisplay();
	updateButtonState();
	});

	btnInc.addEventListener('mouseup', stopHoldRepeat);
	btnInc.addEventListener('mouseleave', stopHoldRepeat);

	// Also add touch support for mobile devices
	btnDec.addEventListener('touchstart', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		if (card.pendingIndex === -1) {
		// Already at "Not started", do nothing
		} else if (card.pendingIndex === 0) {
		card.pendingIndex = -1;
		} else {
		card.pendingIndex--;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	btnDec.addEventListener('touchend', stopHoldRepeat);
	btnDec.addEventListener('touchcancel', stopHoldRepeat);

	btnInc.addEventListener('touchstart', (e) => {
	e.preventDefault();
	startHoldRepeat(() => {
		const sorted = card.sortedChapters || [];
		if (sorted.length === 0) return;
		if (card.pendingIndex === -1) {
		card.pendingIndex = 0;
		} else if (card.pendingIndex < sorted.length - 1) {
		card.pendingIndex++;
		}
		updateChapterDisplay();
		updateButtonState();
	}, 300, 50);
	});

	btnInc.addEventListener('touchend', stopHoldRepeat);
	btnInc.addEventListener('touchcancel', stopHoldRepeat);

	// Updates the card in place instead of reloading the whole grid - the
	// saved chapter is exactly what pendingIndex already points at, so
	// there's nothing to re-fetch, just commit it as the new baseline.
	function applyAcceptedChapter(newChapterNumber) {
		series.current_chapter = newChapterNumber;
		card.originalIndex = card.pendingIndex;
		updateUnreadBadge();
		updateChapterDisplay();
		updateButtonState();
	}

	btnAccept.addEventListener('click', () => {
		if (card.pendingIndex === -1) {
			saveChapter(series.id, -1, series.current_chapter).then(res => {
				if (res.ok) applyAcceptedChapter(-1);
			});
		} else {
			const ch = card.sortedChapters[card.pendingIndex];
			saveChapter(series.id, ch.chapter_number, series.current_chapter).then(res => {
				if (res.ok) applyAcceptedChapter(ch.chapter_number);
			});
		}
	});
	btnSet.addEventListener('click', () => openEditModal(series));
	
	// *** UPDATED: Use auxclick for proper middle-click detection ***
	// Avoid the autoscroll cursor on middle-click eating the auxclick below
	// (same fix already used for import_kenmei.html's "All" button).
	btnSearchGoogle.addEventListener('mousedown', (e) => {
		if (e.button === 1) e.preventDefault();
	});

	// Search Google button - handle left-click
	btnSearchGoogle.addEventListener('click', (e) => {
		e.preventDefault();
		const sorted = card.sortedChapters || [];
		let nextChapterNum;
		if (card.pendingIndex === -1) {
			nextChapterNum = sorted.length > 0 ? sorted[0].chapter_number : 1;
		} else if (card.pendingIndex < sorted.length - 1) {
			nextChapterNum = sorted[card.pendingIndex + 1].chapter_number;
		} else {
			nextChapterNum = sorted[sorted.length - 1].chapter_number + 1;
		}
		const query = encodeURIComponent(`${series.title} chapter ${nextChapterNum}`);
		window.open(`https://www.google.com/search?q=${query}`, '_blank');
	});
	
	// Search Google button - handle middle-click
	btnSearchGoogle.addEventListener('auxclick', (e) => {
		if (e.button === 1) { // Middle click
			e.preventDefault();
			const sorted = card.sortedChapters || [];
			let nextChapterNum;
			if (card.pendingIndex === -1) {
				nextChapterNum = sorted.length > 0 ? sorted[0].chapter_number : 1;
			} else if (card.pendingIndex < sorted.length - 1) {
				nextChapterNum = sorted[card.pendingIndex + 1].chapter_number;
			} else {
				nextChapterNum = sorted[sorted.length - 1].chapter_number + 1;
			}
			const query = encodeURIComponent(`${series.title} chapter ${nextChapterNum}`);
			window.open(`https://www.google.com/search?q=${query}`, '_blank');
		}
	});
	
	// Avoid the autoscroll cursor on middle-click eating the auxclick below.
	btnSource.addEventListener('mousedown', (e) => {
		if (e.button === 1) e.preventDefault();
	});

	// Go to Source button - handle left-click
	btnSource.addEventListener('click', (e) => {
		e.preventDefault();
		let sourceUrl = series.source_url;
		if (pendingSourceChanges.primarySourceId && currentSeriesIdForEdit === series.id) {
			const container = document.getElementById('sources-list');
			if (container) {
				const primaryEl = container.querySelector('.source-item.primary');
				if (primaryEl) {
					sourceUrl = primaryEl.querySelector('.source-url').textContent;
				}
			}
		}
		if (isSafeUrl(sourceUrl)) window.open(sourceUrl, '_blank');
	});

	// Go to Source button - handle middle-click
	btnSource.addEventListener('auxclick', (e) => {
		if (e.button === 1) { // Middle click
			e.preventDefault();
			let sourceUrl = series.source_url;
			if (pendingSourceChanges.primarySourceId && currentSeriesIdForEdit === series.id) {
				const container = document.getElementById('sources-list');
				if (container) {
					const primaryEl = container.querySelector('.source-item.primary');
					if (primaryEl) {
						sourceUrl = primaryEl.querySelector('.source-url').textContent;
					}
				}
			}
			if (isSafeUrl(sourceUrl)) window.open(sourceUrl, '_blank');
		}
	});
	
	const checkbox = card.querySelector('.series-checkbox');
	checkbox.addEventListener('click', (e) => {
		e.stopPropagation();
		toggleCardSelection(series.id);
	});
	card.addEventListener('click', (e) => {
		if (e.target.closest('button') || e.target.closest('a')) return;
		if (bulkState.isBulkMode) {
			toggleCardSelection(series.id);
		}
	});
	
	// Setup lazy loading for cover image
	const coverImg = card.querySelector('.series-cover');
	if (coverImg && coverImg.dataset.src) {
		imageObserver.observe(coverImg);
	}
	
	return card;
}

// ─── Page Loading & Pagination ───────────────────────────────
function renderPagination(current, total, status, sort) {
	const paginationTop = document.getElementById('pagination');
	const paginationBottom = document.getElementById('pagination-bottom');
	if (total <= 1) {
		paginationTop.innerHTML = '';
		paginationBottom.innerHTML = '';
		paginationTop.style.display = 'none';
		paginationBottom.style.display = 'none';
		return;
	}

	paginationTop.style.display = '';
	paginationBottom.style.display = '';

	function renderNav() {
		const nav = document.createElement('nav');
		nav.className = 'relative z-0 inline-flex shadow-sm rounded-md';
		const prevBtn = document.createElement('a');
		prevBtn.className = 'left-chevron px-3 py-1 flex items-center justify-center';
		prevBtn.href = 'javascript:void(0)';
		if (current <= 1) {
			prevBtn.classList.add('cursor-not-allowed', 'opacity-50');
		} else {
			prevBtn.addEventListener('click', () => {
				if (!isLoadingPage) {
					state.page = current - 1;
					loadPage();
				}
			});
		}
		const prevSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		prevSvg.setAttribute('viewBox', '0 0 20 20');
		prevSvg.setAttribute('fill', 'currentColor');
		prevSvg.classList.add('w-5', 'h-5');
		const prevPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
		prevPath.setAttribute('fill-rule', 'evenodd');
		prevPath.setAttribute('d', 'M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z');
		prevPath.setAttribute('clip-rule', 'evenodd');
		prevSvg.appendChild(prevPath);
		prevBtn.appendChild(prevSvg);
		nav.appendChild(prevBtn);
		const delta = 2;
		let start = Math.max(1, current - delta);
		let end = Math.min(total, current + delta);
		const pages = [];
		if (start > 1) {
			pages.push(1);
			if (start > 2) pages.push('gap');
		}
		for (let i = start; i <= end; i++) {
			pages.push(i);
		}
		if (end < total) {
			if (end < total - 1) pages.push('gap');
			pages.push(total);
		}
		pages.forEach(page => {
			if (page === 'gap') {
				const gap = document.createElement('span');
				gap.className = 'gap px-3 py-1 flex items-center justify-center';
				gap.textContent = '…';
				nav.appendChild(gap);
			} else {
				const pageBtn = document.createElement('a');
				pageBtn.className = '-ml-px px-3 py-1 flex items-center justify-center cursor-pointer';
				pageBtn.textContent = String(page);
				pageBtn.href = 'javascript:void(0)';
				if (page === current) {
					pageBtn.classList.add('page__current');
				} else {
					pageBtn.addEventListener('click', () => {
						if (!isLoadingPage) {
							state.page = page;
							loadPage();
						}
					});
				}
				nav.appendChild(pageBtn);
			}
		});
		const nextBtn = document.createElement('a');
		nextBtn.className = 'right-chevron px-3 py-1 flex items-center justify-center';
		nextBtn.href = 'javascript:void(0)';
		if (current >= total) {
			nextBtn.classList.add('cursor-not-allowed', 'opacity-50');
		} else {
			nextBtn.addEventListener('click', () => {
				if (!isLoadingPage) {
					state.page = current + 1;
					loadPage();
				}
			});
		}
		const nextSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		nextSvg.setAttribute('viewBox', '0 0 20 20');
		nextSvg.setAttribute('fill', 'currentColor');
		nextSvg.classList.add('w-5', 'h-5');
		const nextPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
		nextPath.setAttribute('fill-rule', 'evenodd');
		nextPath.setAttribute('d', 'M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z');
		nextPath.setAttribute('clip-rule', 'evenodd');
		nextSvg.appendChild(nextPath);
		nextBtn.appendChild(nextSvg);
		nav.appendChild(nextBtn);
		return nav;
	}
	paginationTop.innerHTML = '';
	paginationBottom.innerHTML = '';
	paginationTop.appendChild(renderNav());
	paginationBottom.appendChild(renderNav());
}

// Fetch and render the source-health indicator (btn-source-alert): lights
// up with a count of series that have a source stuck failing 3+ scheduler
// scans in a row.
let sourceHealthList = [];
let sourceHealthCount = 0;
const SOURCE_HEALTH_TYPE_LABELS = {
	mangadex: 'MangaDex', kagane: 'Kagane', atsu: 'Atsumaru', asura: 'AsuraScans', hive: 'HiveToons', unknown: 'Unknown'
};

async function updateSourceHealth() {
	try {
		const res = await fetch('/api/source-health');
		if (!res.ok) return;
		const { count, sources } = await res.json();
		sourceHealthList = sources || [];
		sourceHealthCount = count || 0;
		renderSourceHealthUI();
	} catch (e) {
		// silent fail
	}
}

// Re-applies the last-fetched health state to whichever badge/list elements
// currently exist in the DOM. Needed on its own (not just inside
// updateSourceHealth's fetch callback) because the mobile header is built by
// JS after this module's first DOMContentLoaded fetch already ran - without
// this, the mobile panel would stay blank until the next 30s poll.
function renderSourceHealthUI() {
	['source-health-count', 'mobile-source-health-count'].forEach(id => {
		const badge = document.getElementById(id);
		if (!badge) return;
		if (sourceHealthCount > 0) {
			badge.textContent = sourceHealthCount;
			badge.style.display = 'inline';
		} else {
			badge.style.display = 'none';
		}
	});
	renderSourceHealthPanel();
}

function renderSourceHealthPanel() {
	[
		{ listId: 'source-health-list', panelId: 'source-health-panel' },
		{ listId: 'mobile-source-health-list', panelId: 'mobile-source-health-panel' }
	].forEach(({ listId, panelId }) => renderSourceHealthList(listId, panelId));
}

function renderSourceHealthList(listId, panelId) {
	const list = document.getElementById(listId);
	if (!list) return;
	if (sourceHealthList.length === 0) {
		list.innerHTML = '<p class="source-health-empty">All sources healthy.</p>';
		return;
	}
	list.innerHTML = sourceHealthList.map(s => `
		<button type="button" class="source-health-item" data-source-id="${s.source_id}">
			<div class="source-health-item-title">${escapeHtml(s.series_title)}</div>
			<div class="source-health-item-meta">${escapeHtml(SOURCE_HEALTH_TYPE_LABELS[s.source_type] || s.source_type)} — failed ${s.consecutive_failures}x in a row</div>
			${s.last_error ? `<div class="source-health-item-error" title="${escapeHtml(s.last_error)}">${escapeHtml(s.last_error)}</div>` : ''}
		</button>
	`).join('');

	// Clicking a row surfaces that series via the existing search filter
	// (rather than duplicating Series Settings' own source-management UI
	// here) - the user picks it up from there. Also clears every other
	// filter (status, rating, tags, etc.) so a series sitting outside the
	// currently-applied status/rating doesn't stay hidden despite matching
	// the search text - status and rating are set to the series' own
	// values, everything else is cleared entirely.
	list.querySelectorAll('.source-health-item').forEach(item => {
		item.addEventListener('click', () => {
			const s = sourceHealthList.find(x => String(x.source_id) === item.dataset.sourceId);
			if (!s) return;

			applyFilterBookmarkState({
				status: s.status || 'reading',
				sort: state.sort,
				dir: state.dir,
				type: [], genre: [], rating: [], pubStatus: [], readableOn: [], customTags: []
			});

			const searchInput = document.getElementById('search-input');
			const mobileSearch = document.getElementById('mobile-search-input');
			if (searchInput) searchInput.value = s.series_title;
			if (mobileSearch) mobileSearch.value = s.series_title;

			document.getElementById(panelId)?.classList.add('hidden');
			loadPage();
		});
	});
}

// updateUnreadErrorCount() itself now lives in notifications.js (loaded on
// every page, not just the dashboard) so every page's nav badge stays in
// sync - it's still called from here (see createMobileHeader()) to reapply
// the already-known count as soon as the mobile header's badge element exists.

// ─── Load Page (Main Logic) ───────────────────────────────────
// Refreshes one series' card without reloading the whole grid - used after
// Series Settings Save, which can touch title/cover/status/chapter/tags/
// primary source all at once, so (unlike btnAccept, which already knows
// the exact new value) this re-fetches that one series fresh and either
// replaces its card or removes it if it no longer matches the active
// status tab - other filters (search/genre/etc.) aren't re-checked, so a
// card that no longer matches one of those may linger until the next
// natural reload, which is an acceptable rare edge case here.
async function refreshSeriesCardInPlace(seriesId) {
	try {
		const res = await fetch(`/api/series/${seriesId}`);
		if (!res.ok) { loadPage(); return; }
		const freshSeries = await res.json();

		if (Array.isArray(state.allSeries)) {
			const idx = state.allSeries.findIndex(s => s.id === seriesId);
			if (idx !== -1) state.allSeries[idx] = freshSeries;
		}

		const oldCard = document.querySelector(`.series-card[data-series-id="${seriesId}"]`);
		if (!oldCard) return; // not on the current page/filter view - nothing to update

		if (state.status !== 'all' && freshSeries.status !== state.status) {
			oldCard.remove();
			return;
		}

		const newCard = renderSeriesCard(freshSeries);
		oldCard.replaceWith(newCard);
	} catch (e) {
		loadPage(); // fall back to a full reload on any unexpected error
	}
}

async function loadPage() {
	// Prevent concurrent loads
	if (isLoadingPage) return;
	isLoadingPage = true;

	// Every filter/sort interaction ends up calling loadPage(), so this is
	// the one place that reliably catches "current filters no longer match
	// the active bookmark" without hooking every individual control.
	updateBookmarkUpdateButtonState();

	const { page, status, sort, dir, type, genre, rating, pubStatus, readableOn } = state;

	// FIX: Check both desktop and mobile search inputs
	const desktopSearch = document.getElementById('search-input');
	const mobileSearch = document.getElementById('mobile-search-input');
	const searchQuery = (desktopSearch?.value || mobileSearch?.value || '').trim();
	
	const iconEl = document.getElementById('sort-direction-icon');
	if (iconEl) {
		iconEl.innerHTML = SORT_ICONS[dir];
	}
	const sortBtn = document.getElementById('btn-sort-direction');
	if (sortBtn) {
		sortBtn.onclick = () => {
			state.dir = state.dir === 'asc' ? 'desc' : 'asc';
			loadPage();
		};
	}
	
	// Show skeleton cards immediately
	const seriesGrid = document.getElementById('series-grid');
	const isInitialLoad = !state.hasLoadedOnce;
	const skeletonCount = isInitialLoad ? 12 : 6;
	
	// Clear grid and show skeletons
	seriesGrid.innerHTML = '';
	for (let i = 0; i < skeletonCount; i++) {
		seriesGrid.appendChild(createSkeletonCard());
	}

	// #pagination only reserves visible space (padding) once
	// renderPagination() knows there's more than one page -- hide it
	// proactively while its total-pages count is still unknown, instead
	// of leaving whatever visibility it was left at from the previous
	// load, so skeletons don't get an extra gap above them that the
	// eventual loaded cards (single page) won't have.
	const paginationTop = document.getElementById('pagination');
	if (paginationTop) paginationTop.style.display = 'none';
	
	// Scroll to top smoothly on pagination
	if (state.page !== state.lastPage && state.lastPage !== undefined) {
		window.scrollTo({ top: 0, behavior: 'smooth' });
	}
	state.lastPage = state.page;
	
	let url = `/api/series?page=${page}&per_page=50&status=${encodeURIComponent(status)}&sort=${encodeURIComponent(sort)}&dir=${encodeURIComponent(dir)}`;
	if (searchQuery) {
		url += `&search=${encodeURIComponent(searchQuery)}`;
	}
	if (Array.isArray(state.type) && state.type.length > 0) {
		url += `&type=${encodeURIComponent(state.type.join(','))}`;
	}
	// Handle genre and rating with include/exclude modes
	if (Array.isArray(state.genre) && state.genre.length > 0) {
		const genreNames = state.genre.map(g => g.name).join(',');
		const genreModes = state.genre.map(g => g.mode).join(',');
		url += `&genre=${encodeURIComponent(genreNames)}`;
		url += `&genre_modes=${encodeURIComponent(genreModes)}`;
	}
	
	if (Array.isArray(state.rating) && state.rating.length > 0) {
		const ratingNames = state.rating.map(r => r.name).join(',');
		const ratingModes = state.rating.map(r => r.mode).join(',');
		url += `&rating=${encodeURIComponent(ratingNames)}`;
		url += `&rating_modes=${encodeURIComponent(ratingModes)}`;
	}
	if (Array.isArray(state.pubStatus) && state.pubStatus.length > 0) {
		url += `&pub_status=${encodeURIComponent(state.pubStatus.join(','))}`;
	}
	// NEW: Add readableOn filter
	if (Array.isArray(state.readableOn) && state.readableOn.length > 0) {
		url += `&readable_on=${encodeURIComponent(state.readableOn.join(','))}`;
	}
	if (Array.isArray(state.customTags) && state.customTags.length > 0) {
		url += `&custom_tags=${encodeURIComponent(state.customTags.join(','))}`;
	}

	try {
		// Fetch data (preload)
		const res = await fetch(url);
		if (!res.ok) throw new Error('Failed to load series');
		const data = await res.json();

		// Store all series for reference
		state.allSeries = data.items;
		state.hasLoadedOnce = true;
		
		// Clear grid and render new cards with stagger effect
		seriesGrid.innerHTML = '';
		
		if (data.items.length === 0) {
			seriesGrid.innerHTML = '<p>No series found.</p>';
		} else {
			// Render cards with chapters included, with slight stagger for visual polish
			data.items.forEach((series, index) => {
				const chapters = series.chapters || [];
				const card = renderSeriesCard(series, chapters);
				card.style.animationDelay = `${index * 0.03}s`;
				seriesGrid.appendChild(card);
			});
		}
		
		renderPagination(data.current_page, data.total_pages, status, sort);
	} catch (err) {
		seriesGrid.innerHTML = `
			<div style="grid-column: 1 / -1; text-align: center; padding: 60px 20px; color: #ef4444;">
				<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin: 0 auto 16px; display: block;">
					<circle cx="12" cy="12" r="10"></circle>
					<line x1="12" y1="8" x2="12" y2="12"></line>
					<line x1="12" y1="16" x2="12.01" y2="16"></line>
				</svg>
				<p style="font-size: 18px; font-weight: 600; margin-bottom: 8px; color: white;">Failed to load series</p>
				<p style="font-size: 14px; color: #94a3b8; margin-bottom: 16px;">${err.message}</p>
				<button onclick="loadPage()" style="padding: 10px 20px; background: #1665f4; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
					Retry
				</button>
			</div>
		`;
		console.error(err);
	} finally {
		// Always reset loading state
		isLoadingPage = false;
	}
}

// ─── Multi-select helpers ─────────────────────────────────────
function updateTriggerText(trigger, selected, labelMap = null, defaultText = 'Select') {
	if (selected.length === 0) {
		trigger.textContent = defaultText;
	} else if (selected.length === 1) {
		const value = selected[0];
		trigger.textContent = labelMap?.[value] || value;
	} else {
		trigger.textContent = `${selected.length} Selected`;
	}
}

function closeAllMultiSelectMenus(exceptMenu = null) {
	document.querySelectorAll('.multi-select-menu, .single-select-menu, .bookmark-select-menu').forEach(menu => {
		if (menu !== exceptMenu) {
			menu.classList.add('hidden');
		}
	});
}

function setupStaticMultiSelect(trigger, menu, checkboxes, stateKey, labelMap = null, defaultText = 'Select') {
  const isMobileDrawer = trigger.id.startsWith('mobile-');
  
  document.addEventListener('click', (e) => {
    if (!menu.contains(e.target) && e.target !== trigger) {
      menu.classList.add('hidden');
    }
  });
  
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.classList.toggle('hidden');
    if (!menu.classList.contains('hidden')) {
      closeAllMultiSelectMenus(menu);
      
	// Only set width for desktop (position handled by CSS)
      if (!isMobileDrawer) {
        const rect = trigger.getBoundingClientRect();
        menu.style.width = rect.width + 'px';
      }
    }
  });
  
  checkboxes.forEach(cb => {
    cb.addEventListener('change', () => {
      const selected = Array.from(checkboxes)
        .filter(cb => cb.checked)
        .map(cb => cb.value);
      state[stateKey] = selected;
      state.page = 1;
      loadPage();
      updateTriggerText(trigger, selected, labelMap, defaultText);
    });
  });
  
  const btnSelectAll = menu.querySelector('.btn-select-all');
  const btnSelectNone = menu.querySelector('.btn-select-none');
  if (btnSelectAll) {
    btnSelectAll.addEventListener('click', () => {
      checkboxes.forEach(cb => cb.checked = true);
      state[stateKey] = Array.from(checkboxes).map(cb => cb.value);
      state.page = 1;
      loadPage();
      menu.classList.add('hidden');
      updateTriggerText(trigger, state[stateKey], labelMap, defaultText);
    });
  }
  if (btnSelectNone) {
    btnSelectNone.addEventListener('click', () => {
      checkboxes.forEach(cb => cb.checked = false);
      state[stateKey] = [];
      state.page = 1;
      loadPage();
      menu.classList.add('hidden');
      updateTriggerText(trigger, [], labelMap, defaultText);
    });
  }
  
  checkboxes.forEach(cb => {
    cb.checked = state[stateKey].includes(cb.value);
  });
  updateTriggerText(trigger, state[stateKey], labelMap, defaultText);
}

function setupSingleSelect(trigger, menu, stateKey, labelMap, defaultValue) {
	const options = menu.querySelectorAll('.option-item');
	document.addEventListener('click', (e) => {
		if (!menu.contains(e.target) && e.target !== trigger) {
			menu.classList.add('hidden');
		}
	});
	trigger.addEventListener('click', (e) => {
		e.stopPropagation();
		menu.classList.toggle('hidden');
		if (!menu.classList.contains('hidden')) {
			closeAllMultiSelectMenus(menu);
			const rect = trigger.getBoundingClientRect();
			menu.style.width = rect.width + 'px';
		}
	});
	options.forEach(option => {
		option.addEventListener('click', () => {
			const value = option.dataset.value;
			state[stateKey] = value;
			state.page = 1;
			options.forEach(opt => opt.classList.remove('selected'));
			option.classList.add('selected');
			trigger.textContent = labelMap[value] || value;
			menu.classList.add('hidden');
			loadPage();
		});
	});
	trigger.textContent = labelMap[state[stateKey]] || labelMap[defaultValue];
	options.forEach(opt => {
		if (opt.dataset.value === state[stateKey]) {
			opt.classList.add('selected');
		}
	});
}

// ─── Initial Setup ────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
	updateSourceHealth();
	setInterval(updateSourceHealth, 30000);

	initBookmarkDropdown();
	const sourceHealthBtn = document.getElementById('btn-source-alert');
	const sourceHealthPanel = document.getElementById('source-health-panel');
	if (sourceHealthBtn && sourceHealthPanel) {
		sourceHealthBtn.addEventListener('click', (e) => {
			e.stopPropagation();
			sourceHealthPanel.classList.toggle('hidden');
		});
		document.addEventListener('click', (e) => {
			if (!sourceHealthPanel.contains(e.target) && e.target !== sourceHealthBtn) {
				sourceHealthPanel.classList.add('hidden');
			}
		});
	}

	if (window.location.search) {
		window.history.replaceState({}, '', '/');
	}
	const btnAddSeries = document.getElementById('btn-add-series');
	const addModal = document.getElementById('add-series-modal');
	const editModal = document.getElementById('edit-series-modal');
	const searchInput = document.getElementById('search-input');

	// Status
	const statusTrigger = document.getElementById('filter-status-trigger');
	const statusMenu = document.querySelector('#filter-status-container .single-select-menu');
	setupSingleSelect(statusTrigger, statusMenu, 'status', {
		'all': 'All Statuses',
		'reading': 'Reading',
		'plan_to_read': 'Plan to Read',
		'on_hold': 'On Hold',
		'dropped': 'Dropped',
		'completed': 'Completed'
	}, 'reading');

	// Sort
	const sortTrigger = document.getElementById('sort-order-trigger');
	const sortMenu = document.querySelector('#sort-order-container .single-select-menu');
	setupSingleSelect(sortTrigger, sortMenu, 'sort', {
		'unread_first': 'Unread First',
		'title': 'Title (A→Z)',
		'latest_release': 'Chapter Released',
		'last_added': 'Last Added',
		'total_chapters': 'Total Chapters',
		'available_chapters': 'Available Chapters'
	}, 'unread_first');

	// Content Type
	const typeTrigger = document.getElementById('filter-type-trigger');
	const typeMenu = document.querySelector('#filter-type-container .multi-select-menu');
	const typeCheckboxes = typeMenu.querySelectorAll('input[type="checkbox"]');
	setupStaticMultiSelect(typeTrigger, typeMenu, typeCheckboxes, 'type', {
		'manga': 'Manga',
		'manhwa': 'Manhwa',
		'manhua': 'Manhua',
		'other': 'Other'
	}, 'Content Type');

	// NEW: Readable On filter
	const readableOnTrigger = document.getElementById('filter-readable-on-trigger');
	const readableOnMenu = document.querySelector('#filter-readable-on-container .multi-select-menu');
	const readableOnCheckboxes = readableOnMenu.querySelectorAll('input[type="checkbox"]');
	setupStaticMultiSelect(readableOnTrigger, readableOnMenu, readableOnCheckboxes, 'readableOn', {
		'mangadex': 'MangaDex',
		'kagane': 'Kagane',
		'atsu': 'Atsumaru',
		'asura': 'AsuraScans',
		'hive': 'HiveToons'
	}, 'Readable On');

	// Genre (Tags) - NOW WITH CONTENT RATING INSIDE THE SAME DROPDOWN
	const genreTrigger = document.getElementById('filter-genre-trigger');
	const genreMenu = document.getElementById('filter-genre-menu');
	const genreListSection = genreMenu.querySelector('.genre-list-section');
	const ratingCheckboxes = genreMenu.querySelectorAll('.rating-checkbox');
	const clearAllBtn = document.getElementById('btn-clear-all-tags');

	// Close menu when clicking outside
	document.addEventListener('click', (e) => {
		if (!genreMenu.contains(e.target) && e.target !== genreTrigger) {
			genreMenu.classList.add('hidden');
		}
	});

	// Toggle menu on trigger click
	genreTrigger.addEventListener('click', (e) => {
		e.stopPropagation();
		genreMenu.classList.toggle('hidden');
		if (!genreMenu.classList.contains('hidden')) {
			closeAllMultiSelectMenus(genreMenu);
			const rect = genreTrigger.getBoundingClientRect();
			genreMenu.style.width = (rect.width * 1.6) + 'px';
			// Scroll to top when opening
			const scrollContainer = genreMenu.querySelector('.combined-tags-list');
			if (scrollContainer) {
				scrollContainer.scrollTop = 0;
			}
		}
	});
	
	function updateTagsTriggerText() {
		genreTrigger.textContent = formatTagsTriggerText(state.genre.length, state.rating, state.customTags.length);
	}
		
	// ADDED: Tags mode toggle button
	const tagsModeBtn = document.getElementById('btn-tags-mode');
	if (tagsModeBtn) {
		tagsModeBtn.addEventListener('click', (e) => {
			e.stopPropagation();
			const currentMode = state.tagsMode;
			const newMode = currentMode === 'include' ? 'exclude' : 'include';
			state.tagsMode = newMode;
			
			// Update button appearance
			tagsModeBtn.dataset.mode = newMode;
			const icon = tagsModeBtn.querySelector('svg');
			const text = tagsModeBtn.querySelector('span');
			
			if (newMode === 'exclude') {
				// X icon for exclude
				icon.innerHTML = '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>';
				text.textContent = 'Exclude Mode';
			} else {
				// Checkmark icon for include
				icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
				text.textContent = 'Include Mode';
			}

			// Keep the mobile toggle in sync too -- both read/write the
			// same state.tagsMode.
			const mobileTagsModeBtnSync = document.getElementById('mobile-btn-tags-mode');
			if (mobileTagsModeBtnSync) {
				mobileTagsModeBtnSync.dataset.mode = newMode;
				const mIcon = mobileTagsModeBtnSync.querySelector('svg');
				const mText = mobileTagsModeBtnSync.querySelector('span');
				mIcon.innerHTML = icon.innerHTML;
				mText.textContent = text.textContent;
			}

			// Reload if there are active filters
			if (state.genre.length > 0 || state.rating.length > 0) {
				state.page = 1;
				loadPage();
			}

			// Update trigger text
			updateTagsTriggerText();
		});
	}

	// Clear All button (clears genres + custom tags, restores ratings to their default: Mature/Explicit excluded)
	clearAllBtn.addEventListener('click', () => {
		state.genre = [];
		state.rating = getDefaultRatingState();
		state.customTags = [];
		state.page = 1;

		// Reset all checkboxes data-mode
		genreListSection.querySelectorAll('input[type="checkbox"]').forEach(cb => {
			delete cb.dataset.mode;
		});
		ratingCheckboxes.forEach(cb => {
			if (DEFAULT_EXCLUDED_RATINGS.includes(cb.value)) {
				cb.dataset.mode = 'exclude';
			} else {
				delete cb.dataset.mode;
			}
		});
		document.querySelectorAll('.custom-tags-section input[type="checkbox"]').forEach(cb => {
			cb.checked = false;
		});

		loadPage();
		updateTagsTriggerText();
	});

	// Setup rating checkboxes (3-state toggle: disabled → include → exclude → disabled)
	ratingCheckboxes.forEach(cb => {
		// Initialize state
		const existing = state.rating.find(r => r.name === cb.value);
		if (existing) {
			cb.dataset.mode = existing.mode;
		}
		
		// 3-state click handler
		cb.parentElement.addEventListener('click', (e) => {
			e.preventDefault();
			const current = state.rating.find(r => r.name === cb.value);
			
			// Remove current state
			state.rating = state.rating.filter(r => r.name !== cb.value);
			delete cb.dataset.mode;
			
			// Cycle: disabled → include → exclude → disabled
			if (!current) {
				// disabled → include
				state.rating.push({ name: cb.value, mode: 'include' });
				cb.dataset.mode = 'include';
			} else if (current.mode === 'include') {
				// include → exclude
				state.rating.push({ name: cb.value, mode: 'exclude' });
				cb.dataset.mode = 'exclude';
			}
			// else: exclude → disabled (already removed above)
			
			state.page = 1;
			loadPage();
			updateTagsTriggerText();
		});
	});

	// Reflect the default-excluded Mature/Explicit ratings in the trigger
	// label right away -- without this it shows the static "Tags" text
	// baked into the HTML until the user interacts with something, even
	// though the checkboxes themselves already render red X's for them.
	updateTagsTriggerText();

	// Load genres dynamically
	loadGenres = async function() {
		try {
			const res = await fetch('/api/genres');
			if (res.ok) {
				const genres = await res.json();
				genreListSection.innerHTML = '';
				genres.forEach(genre => {
					const label = document.createElement('label');
					const cb = document.createElement('input');
					cb.type = 'checkbox';
					cb.value = genre;
					
					// Initialize state
					const existing = state.genre.find(g => g.name === genre);
					if (existing) {
						cb.dataset.mode = existing.mode;
					}
					
					label.appendChild(cb);
					label.appendChild(document.createTextNode(genre));
					
					// 3-state click handler
					label.addEventListener('click', (e) => {
						e.preventDefault();
						const current = state.genre.find(g => g.name === genre);
						
						// Remove current state
						state.genre = state.genre.filter(g => g.name !== genre);
						delete cb.dataset.mode;
						
						// Cycle: disabled → include → exclude → disabled
						if (!current) {
							// disabled → include
							state.genre.push({ name: genre, mode: 'include' });
							cb.dataset.mode = 'include';
						} else if (current.mode === 'include') {
							// include → exclude
							state.genre.push({ name: genre, mode: 'exclude' });
							cb.dataset.mode = 'exclude';
						}
						// else: exclude → disabled (already removed above)
						
						state.page = 1;
						loadPage();
						updateTagsTriggerText();
					});
					
					genreListSection.appendChild(label);
				});
			}
		} catch (e) {
			console.error('Failed to load genres:', e);
		}
	};

	// Load custom tags (user-defined, from the Series Settings modal) into
	// the same combined dropdown, between genres and rating. Simple
	// checked/unchecked -- unlike genres this has no exclude mode.
	const customTagsSection = genreMenu.querySelector('.custom-tags-section');
	loadCustomTagsFilterSection = async function() {
		if (!customTagsSection) return;
		try {
			const res = await fetch('/api/custom-tags');
			if (!res.ok) return;
			const tags = await res.json();
			customTagsSection.innerHTML = '';
			tags.forEach(tag => {
				const label = document.createElement('label');
				const cb = document.createElement('input');
				cb.type = 'checkbox';
				cb.value = tag.id;
				cb.checked = state.customTags.includes(tag.id);

				label.appendChild(cb);
				label.appendChild(document.createTextNode(tag.name));

				label.addEventListener('click', (e) => {
					e.preventDefault();
					if (state.customTags.includes(tag.id)) {
						state.customTags = state.customTags.filter(id => id !== tag.id);
						cb.checked = false;
					} else {
						state.customTags = [...state.customTags, tag.id];
						cb.checked = true;
					}
					state.page = 1;
					loadPage();
					updateTagsTriggerText();
				});

				customTagsSection.appendChild(label);
			});
		} catch (e) {
			console.error('Failed to load custom tags:', e);
		}
	};

	// Publication Status
	const pubStatusTrigger = document.getElementById('filter-pub-status-trigger');
	const pubStatusMenu = document.querySelector('#filter-pub-status-container .multi-select-menu');
	const pubStatusCheckboxes = pubStatusMenu.querySelectorAll('input[type="checkbox"]');
	setupStaticMultiSelect(pubStatusTrigger, pubStatusMenu, pubStatusCheckboxes, 'pubStatus', {
		'reading': 'Reading',
		'completed': 'Completed',
		'on_hold': 'On Hold',
		'dropped': 'Dropped',
		'plan_to_read': 'Plan to Read'
	}, 'Publication Status');

	loadGenres();
	loadCustomTagsFilterSection();

	if (searchInput) {
	let searchTimeout;
	const clearBtn = document.getElementById('search-clear-btn'); // ADDED
	
	searchInput.addEventListener('input', () => {
		clearTimeout(searchTimeout);
		searchTimeout = setTimeout(() => {
		state.page = 1;
		loadPage();
		}, 300);
		
		// ADDED: Show/hide clear button
		if (searchInput.value.trim()) {
		clearBtn?.classList.add('show');
		} else {
		clearBtn?.classList.remove('show');
		}
	});
	
	// ADDED: Clear button handler
	if (clearBtn) {
		clearBtn.addEventListener('click', () => {
		searchInput.value = '';
		clearBtn.classList.remove('show');
		state.page = 1;
		loadPage();
		});
	}
	}

	// Modals
	if (btnAddSeries) {
		btnAddSeries.addEventListener('click', () => {
			const input = document.getElementById('new-series-url');
			if (input) input.value = '';
			resetAddSeriesModalView();
			addModal.classList.remove('hidden');
			if (input) input.focus();
		});
	}

	// ─── Add Series modal: cross-source title search view ────────
	// Same "open each source's own search page in a new tab" pattern as the
	// Kenmei import page's per-row search buttons - this isn't a real
	// aggregated search API, just a shortcut to the 5 sites' own search UIs
	// so you can find the right link to paste back into the URL field.
	const SEARCH_SITES = ['mangadex', 'atsu', 'asura', 'hive', 'kagane'];

	function addSeriesSearchUrl(site, title) {
		const q = encodeURIComponent(title).replace(/%20/g, '+');
		switch (site) {
			case 'mangadex': return `https://mangadex.org/search?q=${q}`;
			case 'atsu': return `https://atsu.moe/search?query=${q}`;
			case 'kagane': return `https://kagane.to/search?q=${q}&size=99`;
			case 'asura': return `https://asurascans.com/browse?q=${q}`;
			case 'hive': return `https://hivetoons.org/series/?searchTerm=${q}`;
		}
		return '#';
	}

	// Multiple window.open() calls from one handler get blocked by most
	// browsers except the first; simulating real <a> clicks (one per tab)
	// is treated much more leniently as long as it's still inside the
	// original user gesture.
	function openInNewTab(url) {
		const a = document.createElement('a');
		a.href = url;
		a.target = '_blank';
		a.rel = 'noopener';
		document.body.appendChild(a);
		a.click();
		a.remove();
	}

	const addSeriesUrlView = document.getElementById('add-series-url-view');
	const addSeriesSearchView = document.getElementById('add-series-search-view');
	const addSeriesSearchToggleBtn = document.getElementById('btn-add-series-search-toggle');
	const addSeriesModalTitle = document.getElementById('add-series-modal-title');
	const addSeriesSearchTitleInput = document.getElementById('add-series-search-title');
	const addSeriesSearchSubmitBtn = document.getElementById('btn-add-series-search-submit');
	const addSeriesSearchCancelBtn = document.getElementById('btn-add-search-cancel');

	const ADD_SERIES_SEARCH_ICON = addSeriesSearchToggleBtn ? addSeriesSearchToggleBtn.innerHTML : '';
	const ADD_SERIES_BACK_ICON = `
		<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
			<path d="M15 18l-6-6 6-6"/>
		</svg>
	`;

	function openAllAddSeriesSearches() {
		const title = addSeriesSearchTitleInput.value.trim();
		if (!title) return;
		SEARCH_SITES.forEach(site => openInNewTab(addSeriesSearchUrl(site, title)));
	}

	function resetAddSeriesModalView() {
		if (!addSeriesUrlView || !addSeriesSearchView) return;
		addSeriesSearchView.classList.add('hidden');
		addSeriesUrlView.classList.remove('hidden');
		addSeriesSearchToggleBtn.innerHTML = ADD_SERIES_SEARCH_ICON;
		addSeriesSearchToggleBtn.title = 'Search across sources';
		if (addSeriesModalTitle) addSeriesModalTitle.textContent = 'Add New Series';
		if (addSeriesSearchTitleInput) addSeriesSearchTitleInput.value = '';
	}

	if (addSeriesUrlView && addSeriesSearchView && addSeriesSearchToggleBtn) {
		addSeriesSearchToggleBtn.addEventListener('click', () => {
			if (addSeriesSearchView.classList.contains('hidden')) {
				addSeriesUrlView.classList.add('hidden');
				addSeriesSearchView.classList.remove('hidden');
				addSeriesSearchToggleBtn.innerHTML = ADD_SERIES_BACK_ICON;
				addSeriesSearchToggleBtn.title = 'Back to paste a URL';
				if (addSeriesModalTitle) addSeriesModalTitle.textContent = 'Search Series';
				addSeriesSearchTitleInput.focus();
			} else {
				resetAddSeriesModalView();
			}
		});

		addSeriesSearchTitleInput.addEventListener('keydown', (e) => {
			if (e.key === 'Enter') {
				e.preventDefault();
				openAllAddSeriesSearches();
			}
		});

		if (addSeriesSearchSubmitBtn) {
			addSeriesSearchSubmitBtn.addEventListener('click', openAllAddSeriesSearches);
			addSeriesSearchSubmitBtn.addEventListener('auxclick', (e) => {
				if (e.button === 1) { // middle click
					e.preventDefault();
					openAllAddSeriesSearches();
				}
			});
			addSeriesSearchSubmitBtn.addEventListener('mousedown', (e) => {
				if (e.button === 1) e.preventDefault(); // avoid the autoscroll cursor on middle-click
			});
		}

		if (addSeriesSearchCancelBtn) {
			addSeriesSearchCancelBtn.addEventListener('click', () => {
				addModal.classList.add('hidden');
			});
		}
	}

// ─── Series Settings modal: click-to-edit title ──────────────
	function enterTitleEditMode() {
		const heading = document.getElementById('edit-series-title-heading');
		const input = document.getElementById('edit-series-title-input');
		input.value = heading.textContent;
		heading.classList.add('hidden');
		input.classList.remove('hidden');
		input.focus();
	}

	function exitTitleEditMode() {
		document.getElementById('edit-series-title-input').classList.add('hidden');
		document.getElementById('edit-series-title-heading').classList.remove('hidden');
	}

	// Enter/blur only stages the new title locally (like picking a chapter
	// does) -- nothing is sent until the Save button is clicked.
	function applyPendingTitle() {
		const input = document.getElementById('edit-series-title-input');
		if (input.classList.contains('hidden')) return; // already applied/cancelled
		const newTitle = input.value.trim();
		exitTitleEditMode();
		if (newTitle) {
			document.getElementById('edit-series-title-heading').textContent = newTitle;
		}
		updateSaveButtonState();
	}

	document.getElementById('edit-series-title-heading')?.addEventListener('click', enterTitleEditMode);

	document.getElementById('edit-series-title-input')?.addEventListener('click', (e) => e.stopPropagation());
	document.getElementById('edit-series-title-input')?.addEventListener('blur', applyPendingTitle);
	document.getElementById('edit-series-title-input')?.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') {
			e.preventDefault();
			applyPendingTitle();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			exitTitleEditMode();
		}
	});

// ─── Series Settings modal: hover-reveal cover-edit menu ─────────
// Picking a source thumbnail or applying a URL only stages pendingCoverUrl
// (same convention as the title) -- nothing is sent until Save is clicked.
// Uploading is the one exception: the file has to reach the server to get a
// URL at all, so that upload happens immediately, but the series row itself
// is still untouched until Save.
	function openCoverMenu() {
		document.getElementById('settings-cover-menu')?.classList.remove('hidden');
		document.getElementById('settings-cover-col')?.classList.add('cover-menu-open');
		renderCoverSourceList();
		renderMangadexCoversList();
		renderCoverUploadsList();
	}

	function closeCoverMenu() {
		document.getElementById('settings-cover-menu')?.classList.add('hidden');
		document.getElementById('settings-cover-col')?.classList.remove('cover-menu-open');
	}

	// Stages a cover without closing the menu -- used for the auto-replace
	// that happens when the cover currently on screen gets deleted out from
	// under it, while the user may still be browsing/deleting other uploads.
	function stageCover(url) {
		if (!url) return;
		pendingCoverUrl = url;
		document.getElementById('edit-series-cover-img').src = url;
		updateSaveButtonState();
	}

	function applyPendingCover(url) {
		if (!url) return;
		stageCover(url);
		closeCoverMenu();
	}

	// Unlike stageCover, this commits straight to the server instead of
	// waiting for Save -- used when the cover just got auto-replaced because
	// the file backing it was deleted, so the DB shouldn't be left pointing
	// at a now-dead URL until the user happens to hit Save.
	async function commitCoverChange(url) {
		if (!url || !currentSeriesIdForEdit) return;
		document.getElementById('edit-series-cover-img').src = url;
		try {
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ cover_url: url })
			});
			if (res.ok) {
				if (originalSeriesValues) originalSeriesValues.cover_url = url;
				pendingCoverUrl = null;
				updateSaveButtonState();
				showNotification('Cover replaced and saved', 'read');
				loadPage();
			} else {
				showNotification('Failed to auto-save replacement cover', 'error');
				stageCover(url);
			}
		} catch (err) {
			showNotification('Failed to auto-save replacement cover', 'error');
			stageCover(url);
		}
	}

	// Picks a replacement when the cover currently staged/shown gets deleted:
	// prefer another upload, then a source's cover, then the placeholder.
	async function pickFallbackCover() {
		if (currentSeriesUploadsCache.length > 0) {
			return currentSeriesUploadsCache[0].cover_url;
		}
		const sources = currentSeriesSourcesPromise ? await currentSeriesSourcesPromise : [];
		const withCovers = sources.filter(s => s.cover_url);
		if (withCovers.length > 0) {
			const primary = withCovers.find(s => s.is_primary);
			return (primary || withCovers[0]).cover_url;
		}
		return '/static/placeholder.png';
	}

	async function renderCoverSourceList() {
		const list = document.getElementById('settings-cover-source-list');
		if (!list) return;
		list.innerHTML = '<p class="settings-cover-menu-empty">Loading…</p>';
		const sources = currentSeriesSourcesPromise ? await currentSeriesSourcesPromise : [];
		const withCovers = sources.filter(s => s.cover_url);

		if (withCovers.length === 0) {
			list.innerHTML = '<p class="settings-cover-menu-empty">No source covers saved yet.</p>';
			return;
		}

		const sourceTypeLabel = {
			mangadex: 'MangaDex', kagane: 'Kagane', atsu: 'Atsumaru',
			asura: 'AsuraScans', hive: 'HiveToons', unknown: 'Unknown'
		};

		list.innerHTML = withCovers.map(s => `
			<img src="${escapeHtml(s.cover_url)}" class="settings-cover-source-thumb"
				data-cover-url="${escapeHtml(s.cover_url)}" referrerpolicy="no-referrer"
				title="${escapeHtml(sourceTypeLabel[s.source_type] || s.source_type)}" />
		`).join('');

		list.querySelectorAll('.settings-cover-source-thumb').forEach(thumb => {
			thumb.addEventListener('click', () => applyPendingCover(thumb.dataset.coverUrl));
		});
	}

	async function renderMangadexCoversList() {
		const list = document.getElementById('settings-cover-mangadex-list');
		if (!list) return;
		list.innerHTML = '<p class="settings-cover-menu-empty">Loading…</p>';
		const covers = currentSeriesMangadexCoversPromise ? await currentSeriesMangadexCoversPromise : [];

		if (covers.length === 0) {
			list.innerHTML = '<p class="settings-cover-menu-empty">No MangaDex source linked (or its gallery hasn\'t been fetched yet).</p>';
			return;
		}

		const totalPages = Math.ceil(covers.length / MANGADEX_COVERS_PER_PAGE);
		if (mangadexCoverPage < 0 || mangadexCoverPage >= totalPages) mangadexCoverPage = 0;
		const start = mangadexCoverPage * MANGADEX_COVERS_PER_PAGE;
		const pageCovers = covers.slice(start, start + MANGADEX_COVERS_PER_PAGE);

		const thumbsHtml = pageCovers.map(c => {
			const label = c.volume ? `Vol. ${c.volume}` : 'No volume';
			return `
				<img src="${escapeHtml(c.cover_url)}" class="settings-cover-source-thumb"
					data-cover-url="${escapeHtml(c.cover_url)}" referrerpolicy="no-referrer"
					title="${escapeHtml(label)}${c.locale ? ` (${escapeHtml(c.locale)})` : ''}" />
			`;
		}).join('');

		list.innerHTML = `
			<div class="mangadex-cover-carousel">
				<div class="mangadex-cover-page">${thumbsHtml}</div>
				<button type="button" class="mangadex-cover-arrow mangadex-cover-arrow-prev" id="mangadex-cover-prev" title="Previous">‹</button>
				<button type="button" class="mangadex-cover-arrow mangadex-cover-arrow-next" id="mangadex-cover-next" title="Next">›</button>
			</div>
			${totalPages > 1 ? `<p class="mangadex-cover-counter">Page ${mangadexCoverPage + 1} / ${totalPages}</p>` : ''}
		`;

		list.querySelectorAll('.settings-cover-source-thumb').forEach(thumb => {
			thumb.addEventListener('click', () => applyPendingCover(thumb.dataset.coverUrl));
		});
		// Wraparound in both directions - left from page 1 shows the last page.
		document.getElementById('mangadex-cover-prev')?.addEventListener('click', () => {
			mangadexCoverPage = (mangadexCoverPage - 1 + totalPages) % totalPages;
			renderMangadexCoversList();
		});
		document.getElementById('mangadex-cover-next')?.addEventListener('click', () => {
			mangadexCoverPage = (mangadexCoverPage + 1) % totalPages;
			renderMangadexCoversList();
		});
	}

	async function renderCoverUploadsList() {
		const list = document.getElementById('settings-cover-uploads-list');
		if (!list) return;
		list.innerHTML = '<p class="settings-cover-menu-empty">Loading…</p>';
		const uploads = currentSeriesUploadsPromise ? await currentSeriesUploadsPromise : currentSeriesUploadsCache;

		if (uploads.length === 0) {
			list.innerHTML = '<p class="settings-cover-menu-empty">No uploads yet.</p>';
			return;
		}

		list.innerHTML = uploads.map(u => `
			<div class="settings-cover-upload-item">
				<img src="${escapeHtml(u.cover_url)}" class="settings-cover-source-thumb"
					data-cover-url="${escapeHtml(u.cover_url)}" referrerpolicy="no-referrer" title="Uploaded" />
				<button type="button" class="settings-cover-upload-delete"
					data-cover-id="${u.id}" title="Delete this upload">×</button>
			</div>
		`).join('');

		list.querySelectorAll('.settings-cover-upload-item .settings-cover-source-thumb').forEach(thumb => {
			thumb.addEventListener('click', () => applyPendingCover(thumb.dataset.coverUrl));
		});

		list.querySelectorAll('.settings-cover-upload-delete').forEach(delBtn => {
			delBtn.addEventListener('click', async (e) => {
				e.stopPropagation();
				if (!currentSeriesIdForEdit) return;
				if (!confirm('Delete this uploaded cover? This cannot be undone.')) return;

				const coverId = delBtn.dataset.coverId;
				const deletedEntry = currentSeriesUploadsCache.find(u => String(u.id) === coverId);

				try {
					const res = await fetch(`/api/series/${currentSeriesIdForEdit}/uploaded-covers/${coverId}`, {
						method: 'DELETE'
					});
					if (res.ok) {
						currentSeriesUploadsCache = currentSeriesUploadsCache.filter(u => String(u.id) !== coverId);
						currentSeriesUploadsPromise = Promise.resolve(currentSeriesUploadsCache);

						// If the cover on screen right now is the one just deleted,
						// don't leave it showing a broken image -- swap in a fallback
						// and save it immediately (the old file is already gone, so
						// there's nothing to "undo" by waiting for manual Save).
						const currentEffectiveCover = pendingCoverUrl !== null ? pendingCoverUrl : (originalSeriesValues?.cover_url || '');
						if (deletedEntry && currentEffectiveCover === deletedEntry.cover_url) {
							await commitCoverChange(await pickFallbackCover());
						}

						renderCoverUploadsList();
					} else {
						showNotification('Failed to delete cover', 'error');
					}
				} catch (err) {
					showNotification('Failed to delete cover', 'error');
				}
			});
		});
	}

	document.getElementById('settings-cover-edit-btn')?.addEventListener('click', (e) => {
		e.stopPropagation();
		const menu = document.getElementById('settings-cover-menu');
		const wasHidden = menu?.classList.contains('hidden');
		closeAllSettingsPopovers();
		if (wasHidden) openCoverMenu();
	});

	document.getElementById('settings-cover-menu')?.addEventListener('click', (e) => e.stopPropagation());

	document.addEventListener('click', (e) => {
		const menu = document.getElementById('settings-cover-menu');
		if (!menu || menu.classList.contains('hidden')) return;
		if (!menu.contains(e.target) && e.target.id !== 'settings-cover-edit-btn') {
			closeCoverMenu();
		}
	});

	document.getElementById('settings-cover-url-apply')?.addEventListener('click', () => {
		const input = document.getElementById('settings-cover-url-input');
		const url = input.value.trim();
		if (!url) return;
		applyPendingCover(url);
		input.value = '';
	});

	document.getElementById('settings-cover-url-input')?.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') {
			e.preventDefault();
			document.getElementById('settings-cover-url-apply')?.click();
		}
	});

	document.getElementById('settings-cover-upload-btn')?.addEventListener('click', () => {
		document.getElementById('settings-cover-upload-input')?.click();
	});

	document.getElementById('settings-cover-upload-input')?.addEventListener('change', async (e) => {
		const file = e.target.files[0];
		e.target.value = '';
		if (!file || !currentSeriesIdForEdit) return;

		const btn = document.getElementById('settings-cover-upload-btn');
		const originalLabel = btn.textContent;
		btn.textContent = 'Uploading…';
		btn.disabled = true;

		try {
			const formData = new FormData();
			formData.append('cover', file);
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}/cover-upload`, {
				method: 'POST',
				body: formData
			});
			const data = await res.json();
			if (res.ok && data.cover_url) {
				currentSeriesUploadsCache = [
					{ id: data.id, cover_url: data.cover_url, uploaded_at: new Date().toISOString() },
					...currentSeriesUploadsCache
				];
				currentSeriesUploadsPromise = Promise.resolve(currentSeriesUploadsCache);
				applyPendingCover(data.cover_url);
			} else {
				showNotification(data.error || 'Upload failed', 'error');
			}
		} catch (err) {
			showNotification('Upload failed', 'error');
		} finally {
			btn.textContent = originalLabel;
			btn.disabled = false;
		}
	});

// ─── Series Settings modal: manual chapter/volume entry ──────────
	document.getElementById('chapter-mode-toggle')?.addEventListener('click', (e) => {
		e.preventDefault();
		if (chapterInputMode === 'select') enterManualChapterMode();
		else enterSelectChapterMode();
	});

	document.getElementById('manual-chapter-minus')?.addEventListener('click', () => {
		manualChapterValue = Math.max(0, manualChapterValue - 1);
		formatManualStepperValues();
		updateSaveButtonState();
	});
	document.getElementById('manual-chapter-plus')?.addEventListener('click', () => {
		manualChapterValue = manualChapterValue + 1;
		formatManualStepperValues();
		updateSaveButtonState();
	});
	document.getElementById('manual-volume-minus')?.addEventListener('click', () => {
		if (manualVolumeValue === null) return;
		const n = parseInt(manualVolumeValue, 10) - 1;
		manualVolumeValue = n < 1 ? null : String(n);
		formatManualStepperValues();
		updateSaveButtonState();
	});
	document.getElementById('manual-volume-plus')?.addEventListener('click', () => {
		const n = manualVolumeValue === null ? 0 : parseInt(manualVolumeValue, 10);
		manualVolumeValue = String(n + 1);
		formatManualStepperValues();
		updateSaveButtonState();
	});

	// Typing directly into either field: track the value live so the Save
	// button reacts immediately, but only clamp/reformat on blur or Enter --
	// reformatting on every keystroke would fight the cursor mid-edit.
	document.getElementById('manual-chapter-value')?.addEventListener('input', (e) => {
		const val = parseFloat(e.target.value);
		if (!isNaN(val)) manualChapterValue = val;
		updateSaveButtonState();
	});
	document.getElementById('manual-chapter-value')?.addEventListener('blur', () => {
		manualChapterValue = Math.max(0, manualChapterValue || 0);
		formatManualStepperValues();
		updateSaveButtonState();
	});
	document.getElementById('manual-chapter-value')?.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
	});

	document.getElementById('manual-volume-value')?.addEventListener('input', (e) => {
		const raw = e.target.value.trim();
		if (raw === '') {
			manualVolumeValue = null;
		} else {
			const n = parseInt(raw, 10);
			if (!isNaN(n)) manualVolumeValue = String(n);
		}
		updateSaveButtonState();
	});
	document.getElementById('manual-volume-value')?.addEventListener('blur', () => {
		if (manualVolumeValue !== null) {
			const n = parseInt(manualVolumeValue, 10);
			manualVolumeValue = (isNaN(n) || n < 1) ? null : String(n);
		}
		formatManualStepperValues();
		updateSaveButtonState();
	});
	document.getElementById('manual-volume-value')?.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
	});

// ─── Series Settings modal: Source selector open/close + add-source form ──
	function openSourceMenu() {
		document.getElementById('settings-source-menu')?.classList.remove('hidden');
		document.getElementById('settings-source-selector')?.classList.add('open');
	}

	function closeSourceMenu() {
		document.getElementById('settings-source-menu')?.classList.add('hidden');
		document.getElementById('settings-source-selector')?.classList.remove('open');
	}

	document.getElementById('settings-source-selector')?.addEventListener('click', (e) => {
		e.stopPropagation();
		const menu = document.getElementById('settings-source-menu');
		const wasHidden = menu?.classList.contains('hidden');
		closeAllSettingsPopovers();
		if (wasHidden) openSourceMenu();
	});

	document.getElementById('settings-source-menu')?.addEventListener('click', (e) => e.stopPropagation());

	document.addEventListener('click', (e) => {
		const menu = document.getElementById('settings-source-menu');
		if (!menu || menu.classList.contains('hidden')) return;
		const selector = document.getElementById('settings-source-selector');
		if (!menu.contains(e.target) && !selector?.contains(e.target)) {
			closeSourceMenu();
		}
	});

	document.getElementById('settings-source-add-submit')?.addEventListener('click', async () => {
		const input = document.getElementById('settings-source-url-input');
		const url = input.value.trim();
		if (!url) return;
		input.value = '';
		await addSeriesSource(url);
	});

	document.getElementById('settings-source-url-input')?.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') { e.preventDefault(); document.getElementById('settings-source-add-submit')?.click(); }
	});

	// Same "open every source's search page in a new tab" shortcut as the
	// Add Series modal, but this one already knows the series' title (we're
	// inside its own Settings modal) so it searches immediately - no typing,
	// no separate search view. Reuses the Add Series modal's own
	// SEARCH_SITES/addSeriesSearchUrl/openInNewTab helpers.
	document.getElementById('settings-source-search-btn')?.addEventListener('click', () => {
		const title = document.getElementById('edit-series-title-heading')?.textContent.trim();
		if (!title) return;
		SEARCH_SITES.forEach(site => openInNewTab(addSeriesSearchUrl(site, title)));
	});

// ─── Series Settings modal: Tags picker open/close + create form ──
	function openTagsMenu() {
		// Plain left-aligned popover, full stop -- no dynamic width/position
		// juggling; left:0/width are fixed in CSS. It's meant to be allowed
		// to extend past the modal's edge, but .settings-modal has
		// overflow-y:auto, and per the CSS spec that forces overflow-x to
		// auto too (the "visible pairs with visible only" rule) -- so
		// anything that overflows horizontally gets clipped instead of
		// rendering on top. Escape that by reparenting to <body> as
		// position:fixed, positioned from the trigger's live coordinates.
		const menu = document.getElementById('settings-tags-menu');
		const trigger = document.getElementById('settings-tags-selector');
		if (!menu || !trigger) return;
		if (menu.parentElement !== document.body) {
			document.body.appendChild(menu);
			menu.classList.add('settings-tags-menu-portal');
		}
		const rect = trigger.getBoundingClientRect();
		menu.style.top = `${rect.bottom + 4}px`;
		menu.style.left = `${rect.left}px`;
		menu.classList.remove('hidden');
		// The 700px desktop modal never puts the trigger close enough to
		// the edge for this to matter, but on a narrow phone (Tags takes
		// the right half of its row) left:rect.left with the menu's fixed
		// 280px width can run straight off the right edge of the screen.
		// Measured after unhiding since offsetWidth is 0 while hidden.
		const overflowX = rect.left + menu.offsetWidth - window.innerWidth;
		if (overflowX > 0) {
			menu.style.left = `${Math.max(8, rect.left - overflowX - 8)}px`;
		}
	}

	function closeTagsMenu() {
		document.getElementById('settings-tags-menu')?.classList.add('hidden');
	}

	function openChapterSelectMenu() {
		document.getElementById('chapter-select-menu')?.classList.remove('hidden');
		document.getElementById('chapter-select-trigger')?.classList.add('open');
		const search = document.getElementById('chapter-select-search');
		if (search) {
			search.value = '';
			document.querySelectorAll('#chapter-select-list .settings-dropdown-item').forEach(item => {
				item.style.display = '';
			});
			// Auto-focusing pops the on-screen keyboard up immediately on
			// mobile, eating half the screen before the user's even seen
			// the chapter list they opened this to scroll through -- fine
			// on desktop where focusing just lets you start typing.
			if (!isMobileDevice()) search.focus();
		}
	}

	function closeChapterSelectMenu() {
		document.getElementById('chapter-select-menu')?.classList.add('hidden');
		document.getElementById('chapter-select-trigger')?.classList.remove('open');
	}

	function openStatusMenu() {
		document.getElementById('settings-status-menu')?.classList.remove('hidden');
		document.getElementById('settings-status-selector')?.classList.add('open');
	}

	function closeStatusMenu() {
		document.getElementById('settings-status-menu')?.classList.add('hidden');
		document.getElementById('settings-status-selector')?.classList.remove('open');
	}

	// All five Series Settings popovers (cover, source, tags, chapter, status)
	// are mutually exclusive. Each trigger's own stopPropagation() keeps its
	// click from ever reaching the document-level "close if clicked outside"
	// listeners the OTHERS rely on, so without this they'd stay open forever
	// once a different popover opens -- close everything before toggling.
	function closeAllSettingsPopovers() {
		closeCoverMenu();
		closeSourceMenu();
		closeTagsMenu();
		closeChapterSelectMenu();
		closeStatusMenu();
	}

	document.getElementById('settings-tags-selector')?.addEventListener('click', (e) => {
		e.stopPropagation();
		const menu = document.getElementById('settings-tags-menu');
		const wasHidden = menu?.classList.contains('hidden');
		closeAllSettingsPopovers();
		if (wasHidden) openTagsMenu();
	});

	document.getElementById('settings-tags-menu')?.addEventListener('click', (e) => e.stopPropagation());

	document.addEventListener('click', (e) => {
		const menu = document.getElementById('settings-tags-menu');
		if (!menu || menu.classList.contains('hidden')) return;
		const selector = document.getElementById('settings-tags-selector');
		if (!menu.contains(e.target) && !selector?.contains(e.target)) {
			closeTagsMenu();
		}
	});

	document.getElementById('settings-tags-new-submit')?.addEventListener('click', async () => {
		const input = document.getElementById('settings-tags-new-input');
		const name = input.value.trim();
		if (!name) return;
		input.value = '';
		await createAndApplyCustomTag(name);
	});

	document.getElementById('settings-tags-new-input')?.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') { e.preventDefault(); document.getElementById('settings-tags-new-submit')?.click(); }
	});

// ─── Series Settings modal: Chapter + Status custom dropdown open/close ──
	document.getElementById('chapter-select-trigger')?.addEventListener('click', (e) => {
		e.stopPropagation();
		const menu = document.getElementById('chapter-select-menu');
		const wasHidden = menu?.classList.contains('hidden');
		closeAllSettingsPopovers();
		if (wasHidden) openChapterSelectMenu();
	});

	document.getElementById('chapter-select-menu')?.addEventListener('click', (e) => e.stopPropagation());

	document.addEventListener('click', (e) => {
		const menu = document.getElementById('chapter-select-menu');
		if (!menu || menu.classList.contains('hidden')) return;
		const trigger = document.getElementById('chapter-select-trigger');
		if (!menu.contains(e.target) && !trigger?.contains(e.target)) {
			closeChapterSelectMenu();
		}
	});

	document.getElementById('chapter-select-search')?.addEventListener('input', (e) => {
		const query = e.target.value.toLowerCase().trim();
		document.querySelectorAll('#chapter-select-list .settings-dropdown-item').forEach(item => {
			const matches = !query || item.dataset.search.includes(query);
			item.style.display = matches ? '' : 'none';
		});
	});

	// Stages "Not started" the same way picking it from the dropdown would --
	// nothing is sent until Save, consistent with the rest of the chapter
	// controls. Switches out of manual mode first since "not started" has no
	// manual-entry equivalent (the steppers bottom out at chapter 0).
	document.getElementById('btn-reset-not-started')?.addEventListener('click', () => {
		if (chapterInputMode === 'manual') enterSelectChapterMode();
		const select = document.getElementById('edit-current-chapter');
		if (!select) return;
		select.value = '-1';
		select.dispatchEvent(new Event('change', { bubbles: true }));
		syncChapterCustomList();
	});

	document.getElementById('settings-status-selector')?.addEventListener('click', (e) => {
		e.stopPropagation();
		const menu = document.getElementById('settings-status-menu');
		const wasHidden = menu?.classList.contains('hidden');
		closeAllSettingsPopovers();
		if (wasHidden) openStatusMenu();
	});

	document.getElementById('settings-status-menu')?.addEventListener('click', (e) => e.stopPropagation());

	document.addEventListener('click', (e) => {
		const menu = document.getElementById('settings-status-menu');
		if (!menu || menu.classList.contains('hidden')) return;
		const selector = document.getElementById('settings-status-selector');
		if (!menu.contains(e.target) && !selector?.contains(e.target)) {
			closeStatusMenu();
		}
	});

	document.querySelectorAll('#settings-status-list .settings-dropdown-item').forEach(item => {
		item.addEventListener('click', () => {
			const select = document.getElementById('edit-status');
			select.value = item.dataset.value;
			select.dispatchEvent(new Event('change', { bubbles: true }));
			syncStatusCustomUI();
			closeStatusMenu();
		});
	});

// ─── Series Settings modal: Save button commits title + chapter ──
	document.getElementById('edit-current-chapter')?.addEventListener('change', updateSaveButtonState);
	document.getElementById('edit-status')?.addEventListener('change', updateSaveButtonState);

	document.getElementById('btn-save-chapter')?.addEventListener('click', async () => {
		if (!currentSeriesIdForEdit) return;

		const { chapter: newChapter, volume: newVolume } = getPendingChapterAndVolume();
		const oldChapter = originalSeriesValues?.current_chapter ?? null;
		const chapterChanged = newChapter !== oldChapter;

		const oldVolume = originalSeriesValues?.current_volume ?? null;
		const volumeChanged = chapterInputMode === 'manual' && newVolume !== oldVolume;

		const heading = document.getElementById('edit-series-title-heading');
		const newTitle = heading.textContent;
		const oldTitle = originalSeriesValues?.title || '';
		const titleChanged = newTitle !== oldTitle;

		const coverChanged = pendingCoverUrl !== null && pendingCoverUrl !== (originalSeriesValues?.cover_url || '');

		const statusSelect = document.getElementById('edit-status');
		const newStatus = statusSelect ? statusSelect.value : null;
		const oldStatus = originalSeriesValues?.status || '';
		const statusChanged = statusSelect && newStatus !== oldStatus;

		const tagsChanged = tagIdSetsDiffer(pendingSeriesTagIds, currentSeriesTagIds);
		const tagsToAdd = pendingSeriesTagIds.filter(id => !currentSeriesTagIds.includes(id));
		const tagsToRemove = currentSeriesTagIds.filter(id => !pendingSeriesTagIds.includes(id));
		const primarySourceChanged = pendingPrimarySourceId !== currentPrimarySourceId;

		if (!chapterChanged && !volumeChanged && !titleChanged && !coverChanged && !statusChanged && !tagsChanged && !primarySourceChanged) return;

		const payload = {};
		if (chapterChanged) payload.current_chapter = newChapter;
		if (volumeChanged) payload.current_volume = newVolume;
		if (titleChanged) payload.title = newTitle;
		if (coverChanged) payload.cover_url = pendingCoverUrl;
		if (statusChanged) payload.status = newStatus;

		try {
			const requests = [];
			if (Object.keys(payload).length > 0) {
				requests.push(fetch(`/api/series/${currentSeriesIdForEdit}`, {
					method: 'PATCH',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify(payload)
				}));
			}
			tagsToAdd.forEach(tagId => {
				requests.push(fetch(`/api/series/${currentSeriesIdForEdit}/custom-tags/${tagId}`, { method: 'POST' }));
			});
			tagsToRemove.forEach(tagId => {
				requests.push(fetch(`/api/series/${currentSeriesIdForEdit}/custom-tags/${tagId}`, { method: 'DELETE' }));
			});
			if (primarySourceChanged) {
				requests.push(fetch(`/api/series/${currentSeriesIdForEdit}/sources/${pendingPrimarySourceId}/primary`, { method: 'POST' }));
			}

			const results = await Promise.all(requests);
			const allOk = results.every(r => r.ok);

			if (allOk) {
				if (chapterChanged && originalSeriesValues) originalSeriesValues.current_chapter = newChapter;
				if (volumeChanged && originalSeriesValues) originalSeriesValues.current_volume = newVolume;
				if (titleChanged && originalSeriesValues) originalSeriesValues.title = newTitle;
				if (coverChanged && originalSeriesValues) {
					originalSeriesValues.cover_url = pendingCoverUrl;
					pendingCoverUrl = null;
				}
				if (statusChanged && originalSeriesValues) originalSeriesValues.status = newStatus;
				if (tagsChanged) currentSeriesTagIds = [...pendingSeriesTagIds];
				if (primarySourceChanged) currentPrimarySourceId = pendingPrimarySourceId;
				const parts = [];
				if (titleChanged) parts.push('title');
				if (chapterChanged || volumeChanged) parts.push('chapter');
				if (coverChanged) parts.push('cover');
				if (statusChanged) parts.push('status');
				if (tagsChanged) parts.push('tags');
				if (primarySourceChanged) parts.push('source');
				showNotification(`Updated ${parts.join(', ')}`, 'read');
				closeEditSeriesModal();
				refreshSeriesCardInPlace(currentSeriesIdForEdit);
			} else {
				showNotification('Failed to save changes', 'error');
			}
		} catch (e) {
			showNotification('Failed to save changes', 'error');
		}
		updateSaveButtonState();
	});

// ─── Modified Save Button Handler ─────────────────────────────
	document.getElementById('btn-edit-save')?.addEventListener('click', async () => {
		if (!currentSeriesIdForEdit) {
			alert('No series selected');
			return;
		}

		const chapterSelect = document.getElementById('edit-current-chapter');
		let currentChapterValue = chapterSelect?.value;
		let currentChapterNum = -1;
		if (currentChapterValue !== "-1") {
			currentChapterNum = parseFloat(currentChapterValue);
			if (isNaN(currentChapterNum)) {
				alert('Invalid chapter selection');
				return;
			}
		}

		const updates = {
			title: document.getElementById('edit-title')?.value || '',
			source_url: document.getElementById('edit-source-url')?.value || '',
			cover_url: document.getElementById('edit-cover-url')?.value || '',
			status: document.getElementById('edit-status')?.value || 'plan_to_read',
			current_chapter: currentChapterNum
		};

		try {
			// Save source changes first if any
			if (pendingSourceChanges.hasChanges) {
				await saveSourceChanges(currentSeriesIdForEdit);
			}

			// Then save other updates
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(updates)
			});

			if (res.ok) {
				const seriesTitle = updates.title || 'Series';
				
				// Only show notifications for actual changes (compare with original values)
				if (originalSeriesValues) {
					// Check chapter change
					if (updates.current_chapter !== originalSeriesValues.current_chapter) {
					const formatChapter = (ch) => ch === -1 ? 'Not started' : `Ch.${ch}`;
					const oldChapterText = formatChapter(originalSeriesValues.current_chapter);
					const newChapterText = formatChapter(updates.current_chapter);
					showNotification(`${seriesTitle} updated from ${oldChapterText} to ${newChapterText}`, 'read');
					}
					
					// Check status change
					if (updates.status !== originalSeriesValues.status) {
					const statusMap = {
						'reading': 'Reading',
						'plan_to_read': 'Plan to Read',
						'on_hold': 'On Hold',
						'dropped': 'Dropped',
						'completed': 'Completed'
					};
					const statusText = statusMap[updates.status] || updates.status;
					showNotification(`${seriesTitle} marked as ${statusText}`, 'edit');
					}
					
					// Check title change
					if (updates.title !== originalSeriesValues.title) {
					showNotification(`${seriesTitle} updated`, 'edit');
					}
					
					// Check cover change
					if (updates.cover_url !== originalSeriesValues.cover_url) {
					showNotification(`${seriesTitle} cover image updated`, 'edit');
					}
				}
				
				closeEditSeriesModal();
				loadPage();
			} else {
				const err = await res.json().catch(() => ({}));
				showNotification('Save failed: ' + (err.error || 'Unknown error'), 'error'); // CHANGED
			}
		} catch (e) {
			console.error('Save error:', e);
			showNotification('Network error: ' + e.message, 'error');
		}
	});

	// ─── Modified Cancel Button Handler ─────────────────────────────
	document.getElementById('btn-edit-cancel')?.addEventListener('click', () => {
		if (pendingSourceChanges.hasChanges) {
			if (!confirm('You have unsaved source changes. Discard them?')) {
				return;
			}
		}
		closeEditSeriesModal();
	});

	// ─── Modified Modal Close Handler ─────────────────────────────
	editModal?.addEventListener('click', (e) => {
		if (e.target === editModal) {
			if (pendingPrimarySourceId !== currentPrimarySourceId) {
				if (!confirm('You have an unsaved source change. Discard it?')) {
					return;
				}
			}
			closeEditSeriesModal();
		}
	});

	// UPDATED: Reset with new readableOn field and tagsMode
	document.getElementById('btn-reset-filters')?.addEventListener('click', () => {
		state.status = 'reading';
		state.sort = 'unread_first';
		state.dir = 'asc';
		state.type = [];
		state.genre = [];
		state.rating = getDefaultRatingState();
		state.pubStatus = [];
		state.readableOn = [];
		state.customTags = [];
		state.tagsMode = 'include';  // ADDED: Reset to include mode
		state.page = 1;
		
		// ADDED: Reset tags mode button
		const tagsModeBtn = document.getElementById('btn-tags-mode');
		if (tagsModeBtn) {
			tagsModeBtn.dataset.mode = 'include';
			const icon = tagsModeBtn.querySelector('svg');
			const text = tagsModeBtn.querySelector('span');
			icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
			text.textContent = 'Include Mode';
		}
		if (searchInput) searchInput.value = '';
		document.getElementById('filter-status-trigger').textContent = 'Reading';
		document.getElementById('sort-order-trigger').textContent = 'Unread First';
		document.querySelectorAll('.single-select-menu .option-item').forEach(opt => {
			opt.classList.remove('selected');
			if ((opt.dataset.value === 'reading' && opt.closest('#filter-status-container')) ||
				(opt.dataset.value === 'unread_first' && opt.closest('#sort-order-container'))) {
				opt.classList.add('selected');
			}
		});
		// Clear all checkboxes
		document.querySelectorAll(`
#filter-type-container input[type="checkbox"],
#filter-genre-container input[type="checkbox"],
#filter-genre-container .rating-checkbox,
#filter-pub-status-container input[type="checkbox"],
#filter-readable-on-container input[type="checkbox"]
`).forEach(cb => cb.checked = false);
		// Genre checkboxes are also 3-state (data-mode driven, not .checked) --
		// clearing .checked above does nothing for them, which left the
		// red X / gray check styling stuck on screen after a reset.
		document.querySelectorAll('#filter-genre-container .genre-list-section input[type="checkbox"]').forEach(cb => {
			delete cb.dataset.mode;
		});
		// Rating checkboxes are 3-state (data-mode driven, not .checked) — restore Mature/Explicit to excluded
		document.querySelectorAll('#filter-genre-container .rating-checkbox').forEach(cb => {
			if (DEFAULT_EXCLUDED_RATINGS.includes(cb.value)) {
				cb.dataset.mode = 'exclude';
			} else {
				delete cb.dataset.mode;
			}
		});
		document.getElementById('filter-type-trigger').textContent = 'Content Type';
		// Not a flat 'Tags' -- Mature/Explicit go back to their default
		// excluded (red X) state, which formatTagsTriggerText reflects as
		// "+2 Selected" rather than implying nothing is selected.
		document.getElementById('filter-genre-trigger').textContent = formatTagsTriggerText(state.genre.length, state.rating, state.customTags.length);
		document.getElementById('filter-pub-status-trigger').textContent = 'Publication Status';
		document.getElementById('filter-readable-on-trigger').textContent = 'Readable On';  // NEW
		document.querySelectorAll('.multi-select-menu, .single-select-menu').forEach(menu => {
			menu.classList.add('hidden');
		});
		markDefaultBookmarkActive();
		updateSourceHealth();
		loadPage();
	});

	// Refresh
	document.getElementById('btn-refresh')?.addEventListener('click', async () => {
	if (isLoadingPage) return; // Prevent refresh during load

	const btn = document.getElementById('btn-refresh');

	// Add refreshing class to trigger animation
	btn.classList.add('refreshing');

	// Disable button during refresh
	btn.disabled = true;

	try {
		updateSourceHealth();
		await loadPage();
	} finally {
		// Remove animation class after page loads
		// Use timeout to ensure animation completes
		setTimeout(() => {
		btn.classList.remove('refreshing');
		btn.disabled = false;
		}, 600); // Match animation duration
	}
	});

	// Add Series
	let isAdding = false;
	document.getElementById('btn-add-submit')?.addEventListener('click', async () => {
		if (isAdding) return;
		const btn = document.getElementById('btn-add-submit');
		const originalText = btn.textContent;
		const urlInput = document.getElementById('new-series-url');
		// Multiple URLs can be pasted at once, comma-separated - the first
		// becomes the primary source, the rest get attached once the series
		// exists (same pattern as the Kenmei import page's per-row field).
		const urls = (urlInput?.value || '').split(',').map(u => u.trim()).filter(Boolean);
		const [url, ...extraUrls] = urls;
		const statusSelect = document.getElementById('new-series-status');
		const selectedStatus = statusSelect?.value || 'reading';
		if (!url) {
			alert('Please enter a URL');
			return;
		}
		isAdding = true;
		btn.disabled = true;
		btn.textContent = 'Adding...';
		urlInput.disabled = true;
		statusSelect.disabled = true;
		try {
			const res = await fetch('/api/series', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ source_url: url, status: selectedStatus })
			});
			if (!res.ok) {
				const err = await res.json().catch(() => ({}));
				const errorMsg = err.error || 'Unknown error';
				showNotification(`Failed to add series: ${errorMsg}`, 'error');
				isAdding = false;
				loadPage();
				return; // Don't throw, just return
			}
			const { task_id } = await res.json();
			document.getElementById('add-series-modal').classList.add('hidden');
			let attempt = 0;
			const maxAttempts = 60;
			const poll = async () => {
				if (attempt >= maxAttempts) {
					alert('⚠️ Add timed out. It may still be processing in the background.');
					loadPage();
					isAdding = false;
					return;
				}
				try {
					const statusRes = await fetch(`/api/series/add-status/${task_id}`);
					const statusData = await statusRes.json();
					if (statusData.status === 'pending') {
						attempt++;
						setTimeout(poll, 1000);
					} else {
						if (!statusData.success) {
							// ADDED: Show error notification
							showNotification('Failed to add series: ' + (statusData.error || 'Unknown error'), 'error');
						} else if (statusData.duplicate) {
							// ADDED: Show duplicate notification
							showNotification('Series already exists in your library', 'error');
						} else {
							// Success - series was added
							const status = statusData.series?.status || document.getElementById('new-series-status')?.value || 'plan_to_read';
							const statusMap = {
								'reading': 'Reading',
								'plan_to_read': 'Plan to Read',
								'on_hold': 'On Hold',
								'dropped': 'Dropped',
								'completed': 'Completed'
							};
							const statusText = statusMap[status] || status;

							if (extraUrls.length && statusData.id) {
								const results = await Promise.all(extraUrls.map(u =>
									fetch(`/api/series/${statusData.id}/sources`, {
										method: 'POST',
										headers: { 'Content-Type': 'application/json' },
										body: JSON.stringify({ source_url: u })
									}).then(r => r.json().catch(() => ({}))).then(data => ({ ok: !data.error, error: data.error }))
								));
								const failedCount = results.filter(r => !r.ok).length;
								const extraMsg = failedCount === 0
									? `, +${results.length} source${results.length > 1 ? 's' : ''} added`
									: `, ${results.length - failedCount}/${results.length} extra source(s) added`;
								showNotification(`Series added to ${statusText}${extraMsg}`, 'added');
							} else {
								showNotification(`Series added to ${statusText}`, 'added');
							}
							loadGenres();
						}
						loadPage();
					}
				} catch (e) {
					if (attempt >= maxAttempts - 1) {
						alert('Network error during add. Please check logs.');
						loadPage();
					}
					isAdding = false;
				}
			};
			poll();
		} catch (e) {
			showNotification('Connection error - please try again', 'error');
			isAdding = false;
			loadPage();
		} finally {
			setTimeout(() => {
				btn.disabled = false;
				btn.textContent = originalText;
				if (urlInput) urlInput.disabled = false;
				if (statusSelect) statusSelect.disabled = false;
				isAdding = false;
			}, 500);
		}
	});

	// Cancel button handler
	document.getElementById('btn-add-cancel')?.addEventListener('click', () => {
	document.getElementById('add-series-modal').classList.add('hidden');
	});

	// Click-outside-to-close handler for Add Series modal
	document.getElementById('add-series-modal')?.addEventListener('click', (e) => {
	if (e.target.id === 'add-series-modal') {
		document.getElementById('add-series-modal').classList.add('hidden');
	}
	});

	// Delete
	document.getElementById('btn-delete-series')?.addEventListener('click', () => {
		const seriesTitle = document.getElementById('edit-series-title-heading')?.textContent || 'this series';
		document.getElementById('series-delete-title').innerHTML =
			`This will permanently delete <strong>${escapeHtml(seriesTitle)}</strong>. This action cannot be undone.`;
		document.getElementById('series-delete-modal').classList.remove('hidden');
	});
	document.getElementById('series-delete-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'series-delete-modal') {
			document.getElementById('series-delete-modal').classList.add('hidden');
		}
	});
	document.getElementById('btn-series-delete-cancel')?.addEventListener('click', () => {
		document.getElementById('series-delete-modal').classList.add('hidden');
	});
	document.getElementById('btn-series-delete-confirm')?.addEventListener('click', async () => {
		document.getElementById('series-delete-modal').classList.add('hidden');
		try {
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}`, {
				method: 'DELETE',
				headers: { 'Content-Type': 'application/json' }
			});
			if (res.ok) {
				const seriesTitle = document.getElementById('edit-series-title-heading')?.textContent || 'Series';
				showNotification(`${seriesTitle} deleted`, 'delete');

				closeEditSeriesModal();
				loadPage();
				loadGenres();
			} else {
				showNotification('Failed to delete series', 'error');
			}
		} catch (e) {
			showNotification('Network error: ' + e.message, 'error');
		}
	});


	// Check Now
	document.getElementById('btn-check-now-modal')?.addEventListener('click', async () => {
		const btn = document.getElementById('btn-check-now-modal');
		btn.disabled = true;
		btn.textContent = 'Checking...';
		try {
			const res = await fetch(`/api/series/${currentSeriesIdForEdit}/check-now`, { method: 'POST' });
			if (res.ok) {
				btn.textContent = '✓ Done';
				setTimeout(() => {
					btn.textContent = 'Check Now';
					btn.disabled = false;
					loadPage();
				}, 1500);
			} else {
				btn.textContent = 'Error';
				setTimeout(() => { btn.disabled = false; btn.textContent = 'Check Now'; }, 1500);
			}
		} catch (e) {
			btn.textContent = 'Fail';
			setTimeout(() => { btn.disabled = false; btn.textContent = 'Check Now'; }, 1500);
		}
	});

	const backToTopBtn = document.getElementById('back-to-top');
	window.addEventListener('scroll', () => {
		if (window.scrollY > 300) {
			backToTopBtn.classList.add('show');
		} else {
			backToTopBtn.classList.remove('show');
		}
	});
	backToTopBtn.addEventListener('click', () => {
		window.scrollTo({ top: 0, behavior: 'smooth' });
	});

	// ─── Bulk Actions ───────────────────────────────────────────────
	// Bulk Read
	document.getElementById('btn-bulk-read')?.addEventListener('click', () => {
		const count = bulkState.selectedIds.size;
		document.getElementById('bulk-read-count').innerHTML =
			`This will mark <strong>${count} series</strong> as caught up to the latest chapter.`;
		document.getElementById('bulk-read-modal').classList.remove('hidden');
	});
	document.getElementById('btn-bulk-read-cancel')?.addEventListener('click', () => {
		document.getElementById('bulk-read-modal').classList.add('hidden');
	});
	document.getElementById('btn-bulk-read-confirm')?.addEventListener('click', async () => {
		const ids = Array.from(bulkState.selectedIds);
		document.getElementById('bulk-read-modal').classList.add('hidden');
		const bulkId = 'bulk_' + Date.now();  // Generate unique bulk ID
		for (const id of ids) {
			try {
				const res = await fetch(`/api/series/${id}/chapters`);
				const chapters = await res.json();
				if (chapters.length > 0) {
					const latestChapter = Math.max(...chapters.map(ch => ch.chapter_number));
					await fetch(`/api/series/${id}`, {
						method: 'PATCH',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({
							current_chapter: latestChapter,
							_bulk_id: bulkId,
							_is_bulk: true
						})
					});
				}
			} catch (e) {
				console.error(`Failed to update series ${id}:`, e);
			}
		}
		// ADDED: Show notification for bulk read
		const count = ids.length;
		showNotification(`${count} series marked as read`, 'read');
		exitBulkMode();
		loadPage();
	});

	// ─── Bulk Edit — REPLACED WITH MODAL FLOW ─────────────────────
	document.getElementById('btn-bulk-edit')?.addEventListener('click', () => {
		document.getElementById('bulk-edit-modal').classList.remove('hidden');
	});
	document.getElementById('btn-bulk-edit-cancel')?.addEventListener('click', () => {
		document.getElementById('bulk-edit-modal').classList.add('hidden');
	});
	document.querySelector('.bulk-edit-option[data-action="change-status"]')?.addEventListener('click', () => {
		document.getElementById('bulk-edit-modal').classList.add('hidden');
		document.getElementById('bulk-status-modal').classList.remove('hidden');
	});
	document.querySelectorAll('.status-option').forEach(option => {
		option.addEventListener('click', async () => {
			const newStatus = option.dataset.value;
			const ids = Array.from(bulkState.selectedIds);
			document.getElementById('bulk-status-modal').classList.add('hidden');
			const bulkId = 'bulk_' + Date.now();  // Generate unique bulk ID
			for (const id of ids) {
				try {
					await fetch(`/api/series/${id}`, {
						method: 'PATCH',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({
							status: newStatus,
							_bulk_id: bulkId,
							_is_bulk: true
						})
					});
				} catch (e) {
					console.error(`Failed to update series ${id}:`, e);
				}
			}
			// ADDED: Show notification for bulk status change
			const count = ids.length;
			const statusMap = {
			'reading': 'Reading',
			'plan_to_read': 'Plan to Read',
			'on_hold': 'On Hold',
			'dropped': 'Dropped',
			'completed': 'Completed'
			};
			const statusText = statusMap[newStatus] || newStatus;
			showNotification(`${count} series status changed to ${statusText}`, 'edit');
			exitBulkMode();
			loadPage();
		});
	});
	document.getElementById('btn-status-back')?.addEventListener('click', () => {
		document.getElementById('bulk-status-modal').classList.add('hidden');
		document.getElementById('bulk-edit-modal').classList.remove('hidden');
	});

	// Close modals when clicking outside
	document.getElementById('bulk-edit-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-edit-modal') {
			document.getElementById('bulk-edit-modal').classList.add('hidden');
		}
	});
	document.getElementById('bulk-status-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-status-modal') {
			document.getElementById('bulk-status-modal').classList.add('hidden');
		}
	});
	document.getElementById('bulk-read-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-read-modal') {
			document.getElementById('bulk-read-modal').classList.add('hidden');
		}
	});
	document.getElementById('bulk-delete-modal')?.addEventListener('click', (e) => {
		if (e.target.id === 'bulk-delete-modal') {
			document.getElementById('bulk-delete-modal').classList.add('hidden');
		}
	});

	// Bulk Delete
	document.getElementById('btn-bulk-delete')?.addEventListener('click', () => {
		const count = bulkState.selectedIds.size;
		document.getElementById('bulk-delete-count').innerHTML =
			`This will permanently delete <strong>${count} series</strong>. This action cannot be undone.`;
		document.getElementById('bulk-delete-modal').classList.remove('hidden');
	});
	document.getElementById('btn-bulk-delete-cancel')?.addEventListener('click', () => {
		document.getElementById('bulk-delete-modal').classList.add('hidden');
	});
	document.getElementById('btn-bulk-delete-confirm')?.addEventListener('click', async () => {
		const ids = Array.from(bulkState.selectedIds);
		document.getElementById('bulk-delete-modal').classList.add('hidden');
		const bulkId = 'bulk_' + Date.now();  // Generate unique bulk ID
		for (const id of ids) {
			try {
				await fetch(`/api/series/${id}?bulk_id=${encodeURIComponent(bulkId)}`, {
					method: 'DELETE'
				});
			} catch (e) {
				console.error(`Failed to delete series ${id}:`, e);
			}
		}
		// ADDED: Show notification for bulk delete
		const count = ids.length;
		showNotification(`${count} series deleted`, 'delete');
		exitBulkMode();
		loadPage();
		loadGenres();
	});

	// Initial load
	loadPage();
});

// ================================
// MOBILE UI FUNCTIONALITY
// Add this to the end of dashboard.js
// ================================

// Mobile state
const mobileState = {
  menuOpen: false,
  filterDrawerOpen: false,
  bottomSheetOpen: false,
  currentSeries: null,
  lastScrollY: 0,
  pendingChapter: null  // ADDED: Track unsaved chapter changes
};

// Bulk edit mode state
const bulkEditMobileState = {
  isMenuOpen: false,
  scrollY: 0
};

// ================================
// MOBILE DETECTION & INITIALIZATION
// ================================
function isMobileDevice() {
  return window.innerWidth <= 768;
}

// Helper function to format timestamps as "X time ago"
function formatTimeAgo(dateString) {
  if (!dateString) return 'Unknown';
  
  try {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    
    // Calculate time differences
    const diffMinutes = Math.floor(diffMs / (1000 * 60));
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    const diffMonths = Math.floor(diffDays / 30);
    const diffYears = Math.floor(diffDays / 365);
    
    // Return appropriate format with pluralization
    if (diffMinutes < 1) {
      return 'Just now';
    } else if (diffMinutes < 60) {
      return diffMinutes === 1 ? '1 minute ago' : `${diffMinutes} minutes ago`;
    } else if (diffHours < 24) {
      return diffHours === 1 ? '1 hour ago' : `${diffHours} hours ago`;
    } else if (diffDays < 30) {
      return diffDays === 1 ? '1 day ago' : `${diffDays} days ago`;
    } else if (diffMonths < 12) {
      return diffMonths === 1 ? '1 month ago' : `${diffMonths} months ago`;
    } else {
      return diffYears === 1 ? '1 year ago' : `${diffYears} years ago`;
    }
  } catch (e) {
    console.error('Failed to parse date:', dateString, e);
    return 'Unknown';
  }
}

function initMobile() {
  if (!isMobileDevice()) return;

  // Create mobile header
  createMobileHeader();

  // Create mobile menu
  createMobileMenu();

  // Create filter drawer
  createFilterDrawer();

  // Create bottom sheet
  createBottomSheet();

  // Create bulk toolbar
  createBulkToolbar();

  // Create FAB buttons
  createFABButtons();

  // **ADD THIS LINE:**
  setupMobileControlPanel();

  // Setup scroll behavior
  setupMobileScroll();

  console.log('[Mobile] UI initialized');
}

// ================================
// MOBILE HEADER
// ================================
function createMobileHeader() {
  const header = document.querySelector('.header-full');
  if (!header) return;

  const navContainer = header.querySelector('.nav-container');
  if (!navContainer) return;

  navContainer.innerHTML = `
    <div class="mobile-header">
      <button class="hamburger-btn" id="mobile-menu-btn">
        <svg class="icon-menu" viewBox="0 0 24 24">
          <line x1="3" y1="12" x2="21" y2="12"></line>
          <line x1="3" y1="6" x2="21" y2="6"></line>
          <line x1="3" y1="18" x2="21" y2="18"></line>
        </svg>
        <span id="mobile-menu-btn-badge" class="hamburger-btn-dot" style="display:none;"></span>
      </button>
      <h1 style="font-size: 18px; font-weight: 600; margin: 0;">Manga Tracker</h1>
      <div class="mobile-source-health-wrap">
        <button class="hamburger-btn" id="mobile-btn-source-alert" title="Source Alerts">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 30" width="18" height="18">
            <path fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" d="M25,27H7c-2.2,0-4-1.8-4-4V9c0-2.2,1.8-4,4-4h18c2.2,0,4,1.8,4,4v14C29,25.2,27.2,27,25,27z"/>
            <polyline fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round" points="3,10 16,18 29,10 "/>
          </svg>
          <span id="mobile-source-health-count" class="source-health-badge" style="display:none;"></span>
        </button>
        <div id="mobile-source-health-panel" class="source-health-panel mobile-source-health-panel hidden">
          <p class="source-health-panel-title">Sources needing attention</p>
          <div id="mobile-source-health-list"></div>
        </div>
      </div>
    </div>
  `;

  document.getElementById('mobile-menu-btn')?.addEventListener('click', toggleMobileMenu);

  const mobileSourceHealthBtn = document.getElementById('mobile-btn-source-alert');
  const mobileSourceHealthPanel = document.getElementById('mobile-source-health-panel');
  if (mobileSourceHealthBtn && mobileSourceHealthPanel) {
    mobileSourceHealthBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      mobileSourceHealthPanel.classList.toggle('hidden');
    });
    document.addEventListener('click', (e) => {
      if (!mobileSourceHealthPanel.contains(e.target) && e.target !== mobileSourceHealthBtn) {
        mobileSourceHealthPanel.classList.add('hidden');
      }
    });
  }

  // The header (and its source-health elements) is built after the page's
  // first fetch may have already run - reapply whatever state is already
  // known instead of leaving the mobile panel blank until the next 30s poll.
  renderSourceHealthUI();
  updateUnreadErrorCount();
}

// ================================
// HAMBURGER MENU
// ================================
function createMobileMenu() {
  const existingMenu = document.getElementById('mobile-menu-overlay');
  if (existingMenu) return;

  const menuHTML = `
    <div class="mobile-menu-overlay" id="mobile-menu-overlay"></div>
    <div class="mobile-menu" id="mobile-menu">
      <div class="mobile-menu-header">
        <h2>Menu</h2>
        <button class="hamburger-btn" id="mobile-menu-close">
          <svg class="icon-close" viewBox="0 0 24 24">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="mobile-menu-links">
        <a href="/dashboard">Dashboard</a>
        <a href="https://kenmei.co/discovery" target="_blank">
          Discovery
          <svg style="width:16px;height:16px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3"/>
          </svg>
        </a>
        <a href="/scheduler">Scheduler</a>
        <a href="/errors">
          Errors
          <span id="mobile-error-badge" style="display:none;background:#e53e3e;color:white;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:10px;"></span>
        </a>
        <a href="/backups">Backups</a>
        <a href="/logs">Activity Log</a>
        <a href="/stats">Stats</a>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', menuHTML);

  document.getElementById('mobile-menu-overlay')?.addEventListener('click', closeMobileMenu);
  document.getElementById('mobile-menu-close')?.addEventListener('click', closeMobileMenu);
}

function toggleMobileMenu() {
  if (mobileState.menuOpen) {
    closeMobileMenu();
  } else {
    openMobileMenu();
  }
}

function openMobileMenu() {
  mobileState.menuOpen = true;
  document.getElementById('mobile-menu-overlay')?.classList.add('active');
  document.getElementById('mobile-menu')?.classList.add('active');
  document.body.style.overflow = 'hidden';
}

function closeMobileMenu() {
  mobileState.menuOpen = false;
  document.getElementById('mobile-menu-overlay')?.classList.remove('active');
  document.getElementById('mobile-menu')?.classList.remove('active');
	document.body.style.overflow = '';
	document.body.style.position = '';
	document.body.style.width = '';
	document.body.style.top = '';
	window.scrollTo(0, mobileState.scrollY || 0);

}

// ================================
// FILTER DRAWER
// ================================
function createFilterDrawer() {
  const existingDrawer = document.getElementById('filter-drawer-overlay');
  if (existingDrawer) return;

  const drawerHTML = `
    <div class="filter-drawer-overlay" id="filter-drawer-overlay"></div>
    <div class="filter-drawer" id="filter-drawer">
      <div class="filter-drawer-header">
        <h2 style="font-size:18px;font-weight:600;margin:0;">Filters</h2>
        <button class="hamburger-btn" id="filter-drawer-close">
          <svg class="icon-close" viewBox="0 0 24 24">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="filter-drawer-content">
        
        <!-- Content Type -->
        <div class="filter-section">
          <h3>Content Type</h3>
          <div class="multi-select" id="mobile-filter-type-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-type-trigger" style="width: 100%;">
              Content Type
            </button>
            <div class="multi-select-menu hidden">
              <label><input type="checkbox" value="manga"> Manga</label>
              <label><input type="checkbox" value="manhwa"> Manhwa</label>
              <label><input type="checkbox" value="manhua"> Manhua</label>
              <label><input type="checkbox" value="other"> Other</label>
              <button class="btn-select-all">Select All</button>
              <button class="btn-select-none">Clear</button>
            </div>
          </div>
        </div>

		<!-- Tags (Genres) -->
        <div class="filter-section">
          <h3>Tags</h3>
          <div class="multi-select" id="mobile-filter-genre-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-genre-trigger" style="width: 100%;">
              Tags
            </button>
			<div class="multi-select-menu hidden" id="mobile-filter-genre-menu">
			<!-- ADDED: Mobile tags mode toggle -->
			<div class="tags-mode-toggle">
				<button class="btn-tags-mode" id="mobile-btn-tags-mode" data-mode="include">
				<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
					<path d="M20 6L9 17l-5-5"/>
				</svg>
				<span>Include Mode</span>
				</button>
			</div>
			
			<div class="combined-tags-list">
				<div class="genre-list-section"></div>
				<div style="border-top: 1px solid #334155; margin: 8px 0;"></div>
				<div class="custom-tags-section"></div>
				<div style="border-top: 1px solid #334155; margin: 8px 0;"></div>
				<div class="rating-section">
				<label><input type="checkbox" value="safe" class="rating-checkbox"> Safe</label>
				<label><input type="checkbox" value="mild" class="rating-checkbox"> Suggestive</label>
				<label><input type="checkbox" value="mature" class="rating-checkbox"> Mature</label>
				<label><input type="checkbox" value="explicit" class="rating-checkbox"> Explicit</label>
				</div>
			</div>
			<button class="btn-select-none" id="mobile-btn-clear-all-tags">Clear</button>
			</div>
          </div>
        </div>

        <!-- Publication Status -->
        <div class="filter-section">
          <h3>Publication Status</h3>
          <div class="multi-select" id="mobile-filter-pub-status-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-pub-status-trigger" style="width: 100%;">
              Publication Status
            </button>
            <div class="multi-select-menu hidden">
              <label><input type="checkbox" value="reading"> Reading</label>
              <label><input type="checkbox" value="completed"> Completed</label>
              <label><input type="checkbox" value="on_hold"> On Hold</label>
              <label><input type="checkbox" value="dropped"> Dropped</label>
              <button class="btn-select-all">Select All</button>
              <button class="btn-select-none">Clear</button>
            </div>
          </div>
        </div>

        <!-- Readable On -->
        <div class="filter-section">
          <h3>Readable On</h3>
          <div class="multi-select" id="mobile-filter-readable-on-container">
            <button class="control-input multi-select-trigger" id="mobile-filter-readable-on-trigger" style="width: 100%;">
              Readable On
            </button>
            <div class="multi-select-menu hidden">
              <label><input type="checkbox" value="mangadex"> MangaDex</label>
              <label><input type="checkbox" value="kagane"> Kagane</label>
              <label><input type="checkbox" value="atsu"> Atsumaru</label>
              <label><input type="checkbox" value="asura"> AsuraScans</label>
              <label><input type="checkbox" value="hive"> HiveToons</label>
              <button class="btn-select-all">Select All</button>
              <button class="btn-select-none">Clear</button>
            </div>
          </div>
        </div>

        <!-- Bookmark (saved filter/sort views) -->
        <div class="filter-section">
          <h3>Saved Views</h3>
          <div class="single-select bookmark-select" id="mobile-filter-bookmark-container">
            <button class="control-input single-select-trigger bookmark-select-trigger" id="mobile-filter-bookmark-trigger" style="width: 100%;">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
              </svg>
              <span id="mobile-filter-bookmark-trigger-text">Default</span>
            </button>
            <div class="bookmark-select-menu hidden" id="mobile-filter-bookmark-menu">
              <div id="mobile-bookmark-menu-main">
                <input type="text" class="bookmark-search-input" id="mobile-bookmark-search-input" placeholder="Search views..." />
                <div class="bookmark-list" id="mobile-bookmark-list"></div>
                <div class="bookmark-menu-divider"></div>
                <button type="button" class="bookmark-action-btn" id="mobile-bookmark-update-btn" disabled>
                  <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                    <polyline points="17 21 17 13 7 13 7 21"/>
                    <polyline points="7 3 7 8 15 8"/>
                  </svg>
                  Update current view
                </button>
                <button type="button" class="bookmark-action-btn" id="mobile-bookmark-new-btn">
                  <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="12" y1="5" x2="12" y2="19"/>
                    <line x1="5" y1="12" x2="19" y2="12"/>
                  </svg>
                  Save as new view
                </button>
              </div>
              <div class="bookmark-new-view-form hidden" id="mobile-bookmark-new-view-form">
                <input type="text" class="bookmark-search-input" id="mobile-bookmark-new-view-name" placeholder="View name..." maxlength="60" />
                <div class="bookmark-new-view-form-actions">
                  <button type="button" class="bookmark-form-btn bookmark-form-cancel" id="mobile-bookmark-new-view-cancel">Cancel</button>
                  <button type="button" class="bookmark-form-btn bookmark-form-confirm" id="mobile-bookmark-new-view-confirm">Save</button>
                </div>
              </div>
              <button type="button" class="bookmark-action-btn" id="mobile-bookmark-manage-btn">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="3"/>
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                </svg>
                Manage views
              </button>
            </div>
          </div>
        </div>

      </div>
      <div class="filter-drawer-footer">
        <button class="btn-reset-filters-mobile" id="mobile-reset-filters">Reset All Filters</button>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', drawerHTML);

  // Setup filter drawer events
  document.getElementById('filter-drawer-overlay')?.addEventListener('click', closeFilterDrawer);
  document.getElementById('filter-drawer-close')?.addEventListener('click', closeFilterDrawer);
  document.getElementById('mobile-reset-filters')?.addEventListener('click', resetMobileFilters);

  // Setup multi-select dropdowns (same as desktop)
  
  // Content Type
  const mobileTypeTrigger = document.getElementById('mobile-filter-type-trigger');
  const mobileTypeMenu = document.querySelector('#mobile-filter-type-container .multi-select-menu');
  const mobileTypeCheckboxes = mobileTypeMenu.querySelectorAll('input[type="checkbox"]');
  setupStaticMultiSelect(mobileTypeTrigger, mobileTypeMenu, mobileTypeCheckboxes, 'type', {
    'manga': 'Manga',
    'manhwa': 'Manhwa',
    'manhua': 'Manhua',
    'other': 'Other'
  }, 'Content Type');

  // Genres + Rating (Combined Tags)
  const mobileGenreTrigger = document.getElementById('mobile-filter-genre-trigger');
  const mobileGenreMenu = document.getElementById('mobile-filter-genre-menu');
  const mobileGenreListSection = mobileGenreMenu.querySelector('.genre-list-section');
  const mobileCustomTagsSection = mobileGenreMenu.querySelector('.custom-tags-section');
  const mobileRatingCheckboxes = mobileGenreMenu.querySelectorAll('.rating-checkbox');
  const mobileClearAllBtn = document.getElementById('mobile-btn-clear-all-tags');

  // Close menu when clicking outside
  document.addEventListener('click', (e) => {
    if (!mobileGenreMenu.contains(e.target) && e.target !== mobileGenreTrigger) {
      mobileGenreMenu.classList.add('hidden');
    }
  });

  // Toggle menu on trigger click
  mobileGenreTrigger.addEventListener('click', (e) => {
    e.stopPropagation();
    mobileGenreMenu.classList.toggle('hidden');
    if (!mobileGenreMenu.classList.contains('hidden')) {
      closeAllMultiSelectMenus(mobileGenreMenu);
      const scrollContainer = mobileGenreMenu.querySelector('.combined-tags-list');
      if (scrollContainer) {
        scrollContainer.scrollTop = 0;
      }
    }
  });

	// Update trigger text based on genres, ratings, and custom tags
	function updateMobileTagsTriggerText() {
		mobileGenreTrigger.textContent = formatTagsTriggerText(state.genre.length, state.rating, state.customTags.length);
	}

	// Tags mode toggle button -- found during a mobile-filter audit that
	// this button rendered and updated its own icon/label on reset, but
	// never actually had a click handler wired up, so tapping it did
	// nothing (the desktop version, #btn-tags-mode, has always worked).
	const mobileTagsModeBtn = document.getElementById('mobile-btn-tags-mode');
	if (mobileTagsModeBtn) {
		mobileTagsModeBtn.addEventListener('click', (e) => {
			e.stopPropagation();
			const newMode = state.tagsMode === 'include' ? 'exclude' : 'include';
			state.tagsMode = newMode;

			mobileTagsModeBtn.dataset.mode = newMode;
			const icon = mobileTagsModeBtn.querySelector('svg');
			const text = mobileTagsModeBtn.querySelector('span');
			if (newMode === 'exclude') {
				icon.innerHTML = '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>';
				text.textContent = 'Exclude Mode';
			} else {
				icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
				text.textContent = 'Include Mode';
			}

			// Keep the desktop toggle in sync too -- both read/write the
			// same state.tagsMode.
			const desktopTagsModeBtn = document.getElementById('btn-tags-mode');
			if (desktopTagsModeBtn) {
				desktopTagsModeBtn.dataset.mode = newMode;
				const desktopIcon = desktopTagsModeBtn.querySelector('svg');
				const desktopText = desktopTagsModeBtn.querySelector('span');
				desktopIcon.innerHTML = icon.innerHTML;
				desktopText.textContent = text.textContent;
			}

			if (state.genre.length > 0 || state.rating.length > 0) {
				state.page = 1;
				loadPage();
			}
		});
	}

	// Clear All button (clears genres + custom tags, restores ratings to their default: Mature/Explicit excluded)
  mobileClearAllBtn.addEventListener('click', () => {
    state.genre = [];
    state.rating = getDefaultRatingState();
    state.customTags = [];
    state.page = 1;

    // Reset all checkboxes data-mode
    mobileGenreListSection.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      delete cb.dataset.mode;
    });
    mobileCustomTagsSection?.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.checked = false;
    });
    mobileRatingCheckboxes.forEach(cb => {
      if (DEFAULT_EXCLUDED_RATINGS.includes(cb.value)) {
        cb.dataset.mode = 'exclude';
      } else {
        delete cb.dataset.mode;
      }
    });

    loadPage();
    updateMobileTagsTriggerText();
  });

	// Setup rating checkboxes (3-state toggle: disabled → include → exclude → disabled)
  mobileRatingCheckboxes.forEach(cb => {
    // Initialize state
    const existing = state.rating.find(r => r.name === cb.value);
    if (existing) {
      cb.dataset.mode = existing.mode;
    }
    
    // 3-state click handler
    cb.parentElement.addEventListener('click', (e) => {
      e.preventDefault();
      const current = state.rating.find(r => r.name === cb.value);
      
      // Remove current state
      state.rating = state.rating.filter(r => r.name !== cb.value);
      delete cb.dataset.mode;
      
      // Cycle: disabled → include → exclude → disabled
      if (!current) {
        // disabled → include
        state.rating.push({ name: cb.value, mode: 'include' });
        cb.dataset.mode = 'include';
      } else if (current.mode === 'include') {
        // include → exclude
        state.rating.push({ name: cb.value, mode: 'exclude' });
        cb.dataset.mode = 'exclude';
      }
      // else: exclude → disabled (already removed above)
      
      state.page = 1;
      loadPage();
      updateMobileTagsTriggerText();
    });
  });

  // Reflect the default-excluded Mature/Explicit ratings in the trigger
  // label right away, same as the desktop dropdown -- see the matching
  // comment there for why.
  updateMobileTagsTriggerText();

// Load genres dynamically (mobile version)
  loadMobileGenres = async function() {
    try {
      const res = await fetch('/api/genres');
      if (res.ok) {
        const genres = await res.json();
        mobileGenreListSection.innerHTML = '';
        genres.forEach(genre => {
          const label = document.createElement('label');
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.value = genre;
          
          // Initialize state
          const existing = state.genre.find(g => g.name === genre);
          if (existing) {
            cb.dataset.mode = existing.mode;
          }
          
          label.appendChild(cb);
          label.appendChild(document.createTextNode(genre));
          
          // 3-state click handler
          label.addEventListener('click', (e) => {
            e.preventDefault();
            const current = state.genre.find(g => g.name === genre);
            
            // Remove current state
            state.genre = state.genre.filter(g => g.name !== genre);
            delete cb.dataset.mode;
            
            // Cycle: disabled → include → exclude → disabled
            if (!current) {
              // disabled → include
              state.genre.push({ name: genre, mode: 'include' });
              cb.dataset.mode = 'include';
            } else if (current.mode === 'include') {
              // include → exclude
              state.genre.push({ name: genre, mode: 'exclude' });
              cb.dataset.mode = 'exclude';
            }
            // else: exclude → disabled (already removed above)
            
            state.page = 1;
            loadPage();
            updateMobileTagsTriggerText();
          });
          
          mobileGenreListSection.appendChild(label);
        });
      }
    } catch (e) {
      console.error('Failed to load genres:', e);
    }
  };

  loadMobileGenres();

  // Load custom tags dynamically (mobile version, mirrors the desktop
  // loadCustomTagsFilterSection)
  loadMobileCustomTagsFilterSection = async function() {
    if (!mobileCustomTagsSection) return;
    try {
      const res = await fetch('/api/custom-tags');
      if (!res.ok) return;
      const tags = await res.json();
      mobileCustomTagsSection.innerHTML = '';
      tags.forEach(tag => {
        const label = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = tag.id;
        cb.checked = state.customTags.includes(tag.id);

        label.appendChild(cb);
        label.appendChild(document.createTextNode(tag.name));

        label.addEventListener('click', (e) => {
          e.preventDefault();
          if (state.customTags.includes(tag.id)) {
            state.customTags = state.customTags.filter(id => id !== tag.id);
            cb.checked = false;
          } else {
            state.customTags = [...state.customTags, tag.id];
            cb.checked = true;
          }
          state.page = 1;
          loadPage();
          updateMobileTagsTriggerText();
        });

        mobileCustomTagsSection.appendChild(label);
      });
    } catch (e) {
      console.error('Failed to load custom tags:', e);
    }
  };

  loadMobileCustomTagsFilterSection();

  // Publication Status
  const mobilePubStatusTrigger = document.getElementById('mobile-filter-pub-status-trigger');
  const mobilePubStatusMenu = document.querySelector('#mobile-filter-pub-status-container .multi-select-menu');
  const mobilePubStatusCheckboxes = mobilePubStatusMenu.querySelectorAll('input[type="checkbox"]');
  setupStaticMultiSelect(mobilePubStatusTrigger, mobilePubStatusMenu, mobilePubStatusCheckboxes, 'pubStatus', {
    'reading': 'Reading',
    'completed': 'Completed',
    'on_hold': 'On Hold',
    'dropped': 'Dropped',
    'plan_to_read': 'Plan to Read'
  }, 'Publication Status');

  // Readable On
  const mobileReadableOnTrigger = document.getElementById('mobile-filter-readable-on-trigger');
  const mobileReadableOnMenu = document.querySelector('#mobile-filter-readable-on-container .multi-select-menu');
  const mobileReadableOnCheckboxes = mobileReadableOnMenu.querySelectorAll('input[type="checkbox"]');
  setupStaticMultiSelect(mobileReadableOnTrigger, mobileReadableOnMenu, mobileReadableOnCheckboxes, 'readableOn', {
    'mangadex': 'MangaDex',
    'kagane': 'Kagane',
    'atsu': 'Atsumaru',
    'asura': 'AsuraScans',
    'hive': 'HiveToons'
  }, 'Readable On');
}

function setupMobileControlPanel() {
  if (!isMobileDevice()) return;

  const row2 = document.querySelector('.control-row:nth-child(2)');
  if (!row2) return;

  // Check if mobile row already exists
  if (document.querySelector('.mobile-control-row-2')) return;

  // Create mobile row 2 container
  const mobileRow2 = document.createElement('div');
  mobileRow2.className = 'mobile-control-row-2';
  mobileRow2.style.cssText = 'display: flex; gap: 8px; width: 100%;';

  // Filter button
  const filterBtn = document.createElement('button');
  filterBtn.id = 'mobile-filter-btn';
  filterBtn.className = 'control-button square-button';
  filterBtn.innerHTML = `
    <svg class="icon-filter" viewBox="0 0 24 24" style="width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;">
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>
    </svg>
  `;
  filterBtn.addEventListener('click', openFilterDrawer);

  // Sort direction button (clone from row 1)
  const sortDirBtn = document.createElement('button');
  sortDirBtn.id = 'mobile-sort-direction';
  sortDirBtn.className = 'control-button square-button';
  sortDirBtn.innerHTML = `
    <svg width="16" height="16" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2.5">
      ${SORT_ICONS[state.dir]}
    </svg>
  `;
  sortDirBtn.addEventListener('click', () => {
    state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    sortDirBtn.querySelector('svg').innerHTML = SORT_ICONS[state.dir];
    loadPage();
  });

	// Search input
	const searchContainer = document.createElement('div');
	searchContainer.className = 'control-search';
	searchContainer.style.flex = '1';
	searchContainer.innerHTML = `
	<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
		<circle cx="11" cy="11" r="8"></circle>
		<path d="m21 21-4.35-4.35"></path>
	</svg>
	<input type="text" id="mobile-search-input" placeholder="Search..." />
	<!-- ADDED: Clear button for mobile search -->
	<button class="search-clear-btn" id="mobile-search-clear-btn" title="Clear search">
		<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" class="h-5 w-5 text-gray-400 hover_text-gray-500"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 0 1 1.414 0L10 8.586l4.293-4.293a1 1 0 1 1 1.414 1.414L11.414 10l4.293 4.293a1 1 0 0 1-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 0 1-1.414-1.414L8.586 10 4.293 5.707a1 1 0 0 1 0-1.414" clip-rule="evenodd"/></svg>
	</button>
	`;

	// Setup search functionality
	const searchInput = searchContainer.querySelector('input');
	const mobileClearBtn = searchContainer.querySelector('.search-clear-btn'); // ADDED
	let searchTimeout;

	searchInput.addEventListener('input', () => {
	clearTimeout(searchTimeout);
	searchTimeout = setTimeout(() => {
		state.page = 1;
		loadPage();
	}, 300);
	
	// ADDED: Show/hide clear button
	if (searchInput.value.trim()) {
		mobileClearBtn?.classList.add('show');
	} else {
		mobileClearBtn?.classList.remove('show');
	}
	});

	// ADDED: Mobile clear button handler
	if (mobileClearBtn) {
	mobileClearBtn.addEventListener('click', () => {
		searchInput.value = '';
		mobileClearBtn.classList.remove('show');
		
		// ADDED: Also clear desktop search to keep them synced
		const desktopSearch = document.getElementById('search-input');
		if (desktopSearch) {
		desktopSearch.value = '';
		document.getElementById('search-clear-btn')?.classList.remove('show');
		}
		
		state.page = 1;
		loadPage();
	});
	}

  // Add elements to mobile row
  mobileRow2.appendChild(filterBtn);
  mobileRow2.appendChild(sortDirBtn);
  mobileRow2.appendChild(searchContainer);

  // Insert mobile row at the start of row 2
  row2.insertBefore(mobileRow2, row2.firstChild);

  console.log('[Mobile] Control panel configured');
}

// Add this to initMobile() function:
// setupMobileControlPanel();

// Also update the sort icon when state changes
function updateMobileSortIcon() {
  const mobileSortBtn = document.getElementById('mobile-sort-direction');
  if (mobileSortBtn && isMobileDevice()) {
    mobileSortBtn.querySelector('svg').innerHTML = SORT_ICONS[state.dir];
  }
}


async function loadMobileGenres() {
  try {
    const res = await fetch('/api/genres');
    if (res.ok) {
      const genres = await res.json();
      const dropdown = document.getElementById('mobile-tags');
      if (dropdown) {
        // Add genres as options
        genres.forEach(genre => {
          const option = document.createElement('option');
          option.value = genre;
          option.textContent = genre;
          dropdown.appendChild(option);
        });
      }
    }
  } catch (e) {
    console.error('Failed to load genres:', e);
  }
}

function syncMobileFilters(type) {
  // Not needed anymore - dropdowns handle this directly in their change events
}

function openFilterDrawer() {
  mobileState.filterDrawerOpen = true;
  document.getElementById('filter-drawer-overlay')?.classList.add('active');
  document.getElementById('filter-drawer')?.classList.add('active');
  document.body.style.overflow = 'hidden';

  // Sync dropdowns with current state
  const typeDropdown = document.getElementById('mobile-content-type');
  if (typeDropdown) {
    typeDropdown.value = state.type.length > 0 ? state.type[0] : '';
  }

  const tagsDropdown = document.getElementById('mobile-tags');
  if (tagsDropdown) {
    tagsDropdown.value = state.genre.length > 0 ? state.genre[0] : '';
  }

  const ratingDropdown = document.getElementById('mobile-rating');
  if (ratingDropdown) {
    ratingDropdown.value = state.rating.length > 0 ? state.rating[0] : '';
  }

  const pubStatusDropdown = document.getElementById('mobile-pub-status');
  if (pubStatusDropdown) {
    pubStatusDropdown.value = state.pubStatus.length > 0 ? state.pubStatus[0] : '';
  }

  const readableOnDropdown = document.getElementById('mobile-readable-on');
  if (readableOnDropdown) {
    readableOnDropdown.value = state.readableOn.length > 0 ? state.readableOn[0] : '';
  }
}

function closeFilterDrawer() {
  mobileState.filterDrawerOpen = false;
  document.getElementById('filter-drawer-overlay')?.classList.remove('active');
  document.getElementById('filter-drawer')?.classList.remove('active');
	document.body.style.overflow = '';
	document.body.style.position = '';
	document.body.style.width = '';
	document.body.style.top = '';
	window.scrollTo(0, mobileState.scrollY || 0);
}

function resetMobileFilters() {
  // Reset ALL state (including Status and Sort from control panel)
  state.status = 'reading';
  state.sort = 'unread_first';
  state.dir = 'asc';
  state.type = [];
  state.genre = [];
  state.rating = getDefaultRatingState();
  state.pubStatus = [];
  state.readableOn = [];
  state.customTags = [];
  state.tagsMode = 'include';  // ADDED: Reset tags mode
  state.page = 1;
  
  // ADDED: Reset desktop tags mode button
  const tagsModeBtn = document.getElementById('btn-tags-mode');
  if (tagsModeBtn) {
    tagsModeBtn.dataset.mode = 'include';
    const icon = tagsModeBtn.querySelector('svg');
    const text = tagsModeBtn.querySelector('span');
    icon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
    text.textContent = 'Include Mode';
  }
  
  // ADDED: Reset mobile tags mode button
  const mobileTagsBtn = document.getElementById('mobile-btn-tags-mode');  // CHANGED: Different variable name
  if (mobileTagsBtn) {
    mobileTagsBtn.dataset.mode = 'include';
    const mobileIcon = mobileTagsBtn.querySelector('svg');
    const mobileText = mobileTagsBtn.querySelector('span');
    mobileIcon.innerHTML = '<path d="M20 6L9 17l-5-5"/>';
    mobileText.textContent = 'Include Mode';
  }
  
  // Reset ALL multi-select checkboxes in mobile drawer
  document.querySelectorAll(`
    #mobile-filter-type-container input[type="checkbox"],
    #mobile-filter-genre-container input[type="checkbox"],
    #mobile-filter-genre-container .rating-checkbox,
    #mobile-filter-pub-status-container input[type="checkbox"],
    #mobile-filter-readable-on-container input[type="checkbox"]
  `).forEach(cb => cb.checked = false);
  // Genre checkboxes are also 3-state (data-mode driven, not .checked) --
  // clearing .checked above does nothing for them, which left the red X /
  // gray check styling stuck on screen after a reset.
  document.querySelectorAll('#mobile-filter-genre-container .genre-list-section input[type="checkbox"]').forEach(cb => {
    delete cb.dataset.mode;
  });
  // Rating checkboxes are 3-state (data-mode driven, not .checked) — restore Mature/Explicit to excluded
  document.querySelectorAll('#mobile-filter-genre-container .rating-checkbox').forEach(cb => {
    if (DEFAULT_EXCLUDED_RATINGS.includes(cb.value)) {
      cb.dataset.mode = 'exclude';
    } else {
      delete cb.dataset.mode;
    }
  });

  // Reset trigger texts
  document.getElementById('mobile-filter-type-trigger').textContent = 'Content Type';
  // Not a flat 'Tags' -- Mature/Explicit go back to their default excluded
  // (red X) state, which formatTagsTriggerText reflects as "+2 Selected"
  // rather than implying nothing is selected. Using the same shared
  // helper as desktop instead of a different, out-of-sync hardcoded value
  // is what was causing this to only "sometimes" show +2.
  document.getElementById('mobile-filter-genre-trigger').textContent = formatTagsTriggerText(state.genre.length, state.rating, state.customTags.length);
  document.getElementById('mobile-filter-pub-status-trigger').textContent = 'Publication Status';
  document.getElementById('mobile-filter-readable-on-trigger').textContent = 'Readable On';
  
  // Reset desktop search if it exists
  const desktopSearch = document.getElementById('search-input');
  const mobileSearch = document.getElementById('mobile-search-input');
  if (desktopSearch) desktopSearch.value = '';
  if (mobileSearch) mobileSearch.value = '';
  
  // Update desktop UI elements (this function also runs from the mobile
  // "Reset All Filters" button, but the desktop elements still exist in
  // the DOM underneath the mobile layout and share the same state)
  const statusTrigger = document.getElementById('filter-status-trigger');
  const sortTrigger = document.getElementById('sort-order-trigger');
  const desktopTypeTrigger = document.getElementById('filter-type-trigger');
  const desktopGenreTrigger = document.getElementById('filter-genre-trigger');
  const desktopPubStatusTrigger = document.getElementById('filter-pub-status-trigger');
  const desktopReadableOnTrigger = document.getElementById('filter-readable-on-trigger');
  if (statusTrigger) statusTrigger.textContent = 'Reading';
  if (sortTrigger) sortTrigger.textContent = 'Unread First';
  if (desktopTypeTrigger) desktopTypeTrigger.textContent = 'Content Type';
  if (desktopGenreTrigger) desktopGenreTrigger.textContent = formatTagsTriggerText(state.genre.length, state.rating, state.customTags.length);
  if (desktopPubStatusTrigger) desktopPubStatusTrigger.textContent = 'Publication Status';
  if (desktopReadableOnTrigger) desktopReadableOnTrigger.textContent = 'Readable On';
  
  // Update sort direction icon
  const mobileSortBtn = document.getElementById('mobile-sort-direction');
  if (mobileSortBtn) {
    mobileSortBtn.querySelector('svg').innerHTML = SORT_ICONS['asc'];
  }
  
  // Update desktop status/sort selected states
  document.querySelectorAll('.single-select-menu .option-item').forEach(opt => {
    opt.classList.remove('selected');
    if ((opt.dataset.value === 'reading' && opt.closest('#filter-status-container')) ||
        (opt.dataset.value === 'unread_first' && opt.closest('#sort-order-container'))) {
      opt.classList.add('selected');
    }
  });
  
  // Close all menus
  document.querySelectorAll('.multi-select-menu, .single-select-menu').forEach(menu => {
    menu.classList.add('hidden');
  });
  
  markDefaultBookmarkActive();
  updateSourceHealth();
  loadPage();
  closeFilterDrawer();
}

// ================================
// BOTTOM SHEET
// ================================
function createBottomSheet() {
  const existingSheet = document.getElementById('bottom-sheet-overlay');
  if (existingSheet) return;

  const sheetHTML = `
    <div class="bottom-sheet-overlay" id="bottom-sheet-overlay"></div>
    <div class="bottom-sheet" id="bottom-sheet">
      <div class="bottom-sheet-handle"></div>
      <div class="bottom-sheet-content">
		<div class="bottom-sheet-header">
		<h2 id="sheet-title"></h2>
		<div class="settings-wrapper">
			<button class="btn-sheet-settings" id="sheet-settings-btn">
				<svg class="icon-settings" viewBox="0 0 24 24">
				<circle cx="12" cy="12" r="1"></circle>
				<circle cx="12" cy="5" r="1"></circle>
				<circle cx="12" cy="19" r="1"></circle>
				</svg>
			</button>
			<div class="sheet-settings-menu" id="sheet-settings-menu">
			<button class="sheet-settings-option" data-action="edit">
				<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
				<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
				</svg>
				Edit
			</button>

			<button class="sheet-settings-option" data-action="go-to-source">
				<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
				</svg>
				Go to Source
			</button>
			
			<button class="sheet-settings-option" data-action="copy-name">
				<svg width="20" height="20" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor">
				<path d="M2.5 1C1.676 1 1 1.676 1 2.5v8c0 .824.676 1.5 1.5 1.5H4v.5c0 .824.676 1.5 1.5 1.5h8c.824 0 1.5-.676 1.5-1.5v-8c0-.824-.676-1.5-1.5-1.5H12v-.5c0-.824-.676-1.5-1.5-1.5Zm0 1h8c.281 0 .5.219.5.5v8c0 .281-.219.5-.5.5h-8a.494.494 0 0 1-.5-.5v-8c0-.281.219-.5.5-.5M12 4h1.5c.281 0 .5.219.5.5v8c0 .281-.219.5-.5.5h-8a.494.494 0 0 1-.5-.5V12h5.5c.824 0 1.5-.676 1.5-1.5Z" transform="translate(.56 1.275)scale(1.43)"/>
				</svg>
				Copy Name
			</button>
			
			<button class="sheet-settings-option danger" data-action="delete">
				<svg width="20" height="20" viewBox="0 0 64 64" stroke-width="3" stroke="currentColor" fill="none">
				<path d="M45.49,54.87h-27a1,1,0,0,1-1-1l-2-36H48.46l-2,36A1,1,0,0,1,45.49,54.87Z"/>
				<path d="M51,17.86H13c-.28,0-.5-.16-.5-.35l.93-4.35a.49.49,0,0,1,.5-.3H50.07a.49.49,0,0,1,.5.3l.93,4.35C51.5,17.7,51.28,17.86,51,17.86Z"/>
				<line x1="24" y1="23.44" x2="24" y2="48.44"/>
				<line x1="32" y1="23.44" x2="32" y2="48.44"/>
				<line x1="40" y1="23.44" x2="40" y2="48.44"/>
				<path d="M25.73,12.86V7.57a1,1,0,0,1,1-1H37.27a1,1,0,0,1,1,1v5.29"/>
				</svg>
				Delete
			</button>
			</div>
		</div>
		</div>
        
		<div class="sheet-progress-section">
		<div class="sheet-chapter-row">
			<div class="sheet-chapter-value">
			<button class="btn-chapter-adjust" id="sheet-chapter-minus">
				<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;">
				<line x1="5" y1="12" x2="19" y2="12"></line>
				</svg>
			</button>
			<span id="sheet-current-chapter">Ch.45</span>
			<span style="color:#64748b;margin:0 4px;">/</span>
			<span id="sheet-latest-chapter">Ch.50</span>
			<button class="btn-chapter-adjust" id="sheet-chapter-plus">
				<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;">
				<line x1="12" y1="5" x2="12" y2="19"></line>
				<line x1="5" y1="12" x2="19" y2="12"></line>
				</svg>
			</button>
			</div>
		</div>
          
          <div class="sheet-progress-bar">
            <div class="sheet-progress-fill" id="sheet-progress-fill"></div>
          </div>
          
          <div class="sheet-metadata">
            <span id="sheet-updated">Updated: 2d ago</span>
            <span id="sheet-behind">5 behind</span>
          </div>
        </div>
        
        <div class="sheet-actions">
          <button class="btn-sheet-action secondary" id="sheet-search-btn">
            Search Ch.<span id="sheet-search-chapter">46</span>
          </button>
          <button class="btn-sheet-action primary" id="sheet-continue-btn">
            Continue to Ch.<span id="sheet-search-chapter">46</span>
          </button>
        </div>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', sheetHTML);

	document.getElementById('bottom-sheet-overlay')?.addEventListener('click', (e) => {
	e.stopPropagation();
	closeBottomSheet();
	});
	
	// CHANGED: Toggle settings menu instead of opening edit modal
	document.getElementById('sheet-settings-btn')?.addEventListener('click', (e) => {
	e.stopPropagation();
	const menu = document.getElementById('sheet-settings-menu');
	menu?.classList.toggle('active');
	});

	// ADDED: Close settings menu when clicking outside
	document.addEventListener('click', (e) => {
	const menu = document.getElementById('sheet-settings-menu');
	const settingsBtn = document.getElementById('sheet-settings-btn');
	
	if (menu && !menu.contains(e.target) && e.target !== settingsBtn && !settingsBtn?.contains(e.target)) {
		menu.classList.remove('active');
	}
	});

	// ADDED: Settings menu actions
	document.addEventListener('click', async (e) => {
	const option = e.target.closest('.sheet-settings-option');
	if (!option || option.classList.contains('disabled')) return;

	const action = option.dataset.action;
	const series = mobileState.currentSeries;
	if (!series) return;

	// CHANGED: Only close menu for certain actions (not copy-name)
	if (action !== 'copy-name') {
	document.getElementById('sheet-settings-menu')?.classList.remove('active');
	}

	switch (action) {
		case 'edit':
		// Opens the same Series Settings modal as desktop's pencil-icon
		// Edit -- this used to be two separate, each-incomplete mobile
		// modals (a quick "Edit" for chapter/status/source, and a bare
		// "Settings" for just title/cover). One shared implementation
		// already has everything: cover editing, chapters, source,
		// status, tags, actions, and delete.
		openEditModal(series);
		// Close the bottom sheet WITHOUT unlocking scroll -- the settings
		// modal reuses the same lock and closeEditSeriesModal() (see its
		// close/cancel/save handlers) restores it properly when done.
		{
			const bottomSheet = document.getElementById('bottom-sheet');
			const overlay = document.getElementById('bottom-sheet-overlay');
			bottomSheet?.classList.remove('active');
			overlay?.classList.remove('active');
			if (bottomSheet) bottomSheet.style.transform = '';
			mobileState.bottomSheetOpen = false;
		}
		break;

		case 'go-to-source':		// CHANGED: Use pre-fetched URL
		const sourceUrl = mobileState.primarySourceUrl || series.source_url;
		if (isSafeUrl(sourceUrl)) window.open(sourceUrl, '_blank');
		break;

		case 'copy-name':
		const option = e.target.closest('.sheet-settings-option');
		const originalHTML = option.innerHTML;

		// CHANGED: Try multiple copy methods with fallback
		let copySuccess = false;

		// Method 1: Modern Clipboard API
		if (navigator.clipboard && navigator.clipboard.writeText) {
		try {
			await navigator.clipboard.writeText(series.title);
			copySuccess = true;
		} catch (err) {
			console.log('Clipboard API failed, trying fallback:', err);
		}
		}

		// Method 2: Fallback for older browsers/mobile
		if (!copySuccess) {
		try {
			const textArea = document.createElement('textarea');
			textArea.value = series.title;
			textArea.style.position = 'fixed';
			textArea.style.left = '-999999px';
			textArea.style.top = '-999999px';
			document.body.appendChild(textArea);
			textArea.focus();
			textArea.select();
			
			const successful = document.execCommand('copy');
			document.body.removeChild(textArea);
			
			if (successful) {
			copySuccess = true;
			}
		} catch (err) {
			console.error('Fallback copy failed:', err);
		}
		}

		if (copySuccess) {
		option.innerHTML = `
		<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<path d="M20 6L9 17l-5-5"/>
		</svg>
		Copied!
		`;

		setTimeout(() => {
			option.innerHTML = originalHTML;
		}, 1500);
		} else {
		alert('Failed to copy to clipboard');
		}

		// ADDED: Prevent menu from closing by stopping event propagation
		e.stopPropagation();
		return; // ADDED: Exit early without closing menu
		break;
		
		case 'delete':
		if (confirm(`Delete "${series.title}"? This action cannot be undone.`)) {
			try {
			const res = await fetch(`/api/series/${series.id}`, {
				method: 'DELETE'
			});
			
			if (res.ok) {
				// ADDED: Show notification
				showNotification(`${series.title} deleted`, 'delete');
				
				closeBottomSheet();
				loadPage();
			if (typeof loadGenres === 'function') {
					loadGenres();
				}
			} else {
				showNotification('Failed to delete series', 'error'); // CHANGED
			}
			} catch (err) {
			console.error('Delete error:', err);
			showNotification('Error: ' + err.message, 'error'); 
			}
		}
		break;
	}
	});

  // Add swipe-down gesture with velocity detection
  let touchStartY = 0;
  let touchStartTime = 0;
  let lastTouchY = 0;
  let lastTouchTime = 0;
  let currentTranslateY = 0;
  let isDragging = false;
  const sheet = document.getElementById('bottom-sheet');
  
  sheet?.addEventListener('touchstart', (e) => {
    // Don't drag if touching a button
    if (e.target.closest('button')) {
      return;
    }
    
    // Allow dragging from anywhere in the sheet
    touchStartY = e.touches[0].clientY;
    touchStartTime = Date.now();
    lastTouchY = touchStartY;
    lastTouchTime = touchStartTime;
    currentTranslateY = 0;
    isDragging = true;
    sheet.style.transition = 'none'; // Disable transition during drag
  }, { passive: true });

  sheet?.addEventListener('touchmove', (e) => {
    if (!isDragging) return;
    
    const touchY = e.touches[0].clientY;
    const now = Date.now();
    const diff = touchY - touchStartY;
    
    // Track for velocity calculation
    lastTouchY = touchY;
    lastTouchTime = now;
    
    // Only allow downward movement
    if (diff > 0) {
      currentTranslateY = diff;
      sheet.style.transform = `translateY(${diff}px)`;
      
      // Add resistance effect when pulling far
      if (diff > 200) {
        const resistance = 200 + (diff - 200) * 0.3;
        sheet.style.transform = `translateY(${resistance}px)`;
      }
    }
  }, { passive: true });

  sheet?.addEventListener('touchend', (e) => {
    if (!isDragging) return;
    
    isDragging = false;
    sheet.style.transition = ''; // Re-enable transition
    
    // Calculate velocity (pixels per millisecond)
    const timeDiff = Date.now() - lastTouchTime;
    const distance = lastTouchY - touchStartY;
    const velocity = timeDiff > 0 ? distance / timeDiff : 0;
    
    // Close if:
    // 1. Dragged more than 100px, OR
    // 2. Fast swipe down (velocity > 0.5 px/ms) with at least 30px movement
    const shouldClose = currentTranslateY > 100 || (velocity > 0.5 && distance > 30);
    
    if (shouldClose) {
      closeBottomSheet();
    } else {
      // Snap back to original position
      sheet.style.transform = '';
    }
    
    currentTranslateY = 0;
  }, { passive: true });
  
  sheet?.addEventListener('touchcancel', (e) => {
    if (!isDragging) return;
    
    isDragging = false;
    sheet.style.transition = '';
    sheet.style.transform = '';
    currentTranslateY = 0;
  }, { passive: true });

}

function openBottomSheet(series) {
  mobileState.bottomSheetOpen = true;
  mobileState.currentSeries = series;
  mobileState.pendingChapter = series.current_chapter;
  
  // ADDED: Fetch and store primary source URL
  fetch(`/api/series/${series.id}/sources`)
    .then(res => res.json())
    .then(data => {
      const primarySource = data.sources.find(s => s.is_primary);
      mobileState.primarySourceUrl = primarySource ? primarySource.source_url : series.source_url;
    })
    .catch(err => {
      console.error('Failed to get sources:', err);
      mobileState.primarySourceUrl = series.source_url;
    });

  // ADDED: Lock scrolling - MORE AGGRESSIVE (same as bulk edit menu)
  if (!mobileState.scrollY) { // CHANGED: Only save if not already saved
    mobileState.scrollY = window.scrollY;
  }
  document.body.style.overflow = 'hidden';
  document.body.style.position = 'fixed';
  document.body.style.width = '100%';
  document.body.style.top = `-${mobileState.scrollY}px`;
  document.getElementById('sheet-title').textContent = series.title;

  document.getElementById('sheet-current-chapter').textContent = 
  series.current_chapter === -1 ? 'Not started' : `Ch.${series.current_chapter}`;

  // ADDED: Set latest chapter
  document.getElementById('sheet-latest-chapter').textContent = 
  series.latest_chapter ? `Ch.${series.latest_chapter}` : 'Ch.?';
  
	// CHANGED: Format time as "X time ago" with proper pluralization
	const timeAgo = formatTimeAgo(series.latest_release);
	document.getElementById('sheet-updated').textContent = `Updated: ${timeAgo}`;

	// FIXED: Calculate behind count with proper rounding for floating-point precision
	const behindElement = document.getElementById('sheet-behind');
	let behindCount = 0;

	if (series.current_chapter === -1) {
	behindCount = series.latest_chapter || 0;
	} else {
	// Round to 1 decimal place to fix floating-point errors
	const rawDiff = (series.latest_chapter || 0) - series.current_chapter;
	behindCount = Math.round(rawDiff * 10) / 10;
	}

	if (behindCount === 0 && series.current_chapter !== -1) {
	// Show green "Up to date" with checkmark
	behindElement.innerHTML = `
		<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;margin-right:4px;">
		<path d="M20 6 9 17l-5-5"/>
		</svg>
		<span style="color:#4ade80;font-weight:500;">Up to date</span>
	`;
	} else {
	// Format the number nicely (remove .0 if it's a whole number)
	const displayCount = behindCount % 1 === 0 ? Math.floor(behindCount) : behindCount;
	const chapterWord = behindCount === 1 ? 'chapter' : 'chapters';
	behindElement.textContent = `${displayCount} ${chapterWord} behind`;
	}


  const progress = series.current_chapter === -1 ? 0 : (series.current_chapter / series.latest_chapter) * 100;
  const progressFill = document.getElementById('sheet-progress-fill');
  progressFill.style.width = `${Math.min(progress, 100)}%`;

  // ADDED: Change color to green when caught up
  if (series.unread_count === 0 && series.current_chapter !== -1) {
    progressFill.style.background = '#4ade80';
  } else {
    progressFill.style.background = '#3b82f6';
  }

  // FIXED: Calculate nextChapter BEFORE passing to setupBottomSheetButtons
  const nextChapter = series.current_chapter === -1 ? 1 : series.current_chapter + 1;
  document.getElementById('sheet-search-chapter').textContent = nextChapter;

  // FIXED: Now pass the calculated nextChapter value
  setupBottomSheetButtons(series, nextChapter);

  document.getElementById('bottom-sheet-overlay')?.classList.add('active');
  document.getElementById('bottom-sheet')?.classList.add('active');
  document.body.style.overflow = 'hidden';
}

function setupBottomSheetButtons(series, nextChapter) {
  // Search button - opens Google search for next chapter
	const searchBtn = document.getElementById('sheet-search-btn');
	if (searchBtn) {
	// Remove old listeners by cloning
	const newSearchBtn = searchBtn.cloneNode(true);
	searchBtn.parentNode.replaceChild(newSearchBtn, searchBtn);
	
	// Always show search button with appropriate chapter number
	newSearchBtn.style.display = 'flex';
	const searchChapterSpan = newSearchBtn.querySelector('#sheet-search-chapter');
	if (searchChapterSpan) {
		searchChapterSpan.textContent = nextChapter || 1;
	}
	
	newSearchBtn.addEventListener('click', () => {
		const chapterNum = searchChapterSpan ? searchChapterSpan.textContent : (nextChapter || 1);
		const query = encodeURIComponent(`${series.title} chapter ${chapterNum}`);
		window.open(`https://www.google.com/search?q=${query}`, '_blank');
	});
	}

  // Continue button - opens next chapter URL or first chapter if not started
  const continueBtn = document.getElementById('sheet-continue-btn');
  if (continueBtn) {
    // Remove old listeners by cloning
    const newContinueBtn = continueBtn.cloneNode(true);
    continueBtn.parentNode.replaceChild(newContinueBtn, continueBtn);
    
    // Fetch chapters to get the correct URL
    fetch(`/api/series/${series.id}/chapters`)
      .then(r => r.json())
      .then(chapters => {
        if (chapters.length === 0) {
          newContinueBtn.textContent = 'No chapters';
          newContinueBtn.disabled = true;
          // ADDED: Search button still works for chapter 1
          if (searchBtn) {
            const searchChapterSpan = searchBtn.querySelector('#sheet-search-chapter');
            if (searchChapterSpan) {
              searchChapterSpan.textContent = '1';
            }
          }
          return;
        }

        // Sort chapters (reuse existing logic)
        const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
        const useVolume = !hasAnyNullVolume;
        const comparator = useVolume ? compareChapters : (a, b) => a.chapter_number - b.chapter_number;
        const sorted = [...chapters].sort(comparator);

        let targetChapter = null;

        if (series.current_chapter === -1) {
          // Not started - go to first chapter
          targetChapter = sorted[0];
          newContinueBtn.textContent = `Continue to Ch.${targetChapter.chapter_number}`;
        } else {
          // Find current chapter index
          const currentIndex = sorted.findIndex(ch => ch.chapter_number === series.current_chapter);
          
          if (currentIndex >= 0 && currentIndex < sorted.length - 1) {
            // Go to next chapter
            targetChapter = sorted[currentIndex + 1];
            newContinueBtn.textContent = `Continue to Ch.${targetChapter.chapter_number}`;
          } else {
            // Already caught up
            newContinueBtn.textContent = 'All caught up';
            newContinueBtn.disabled = true;
          }
        }

        if (targetChapter) {
          newContinueBtn.disabled = false;
          newContinueBtn.addEventListener('click', () => {
            if (isSafeUrl(targetChapter.chapter_url)) window.open(targetChapter.chapter_url, '_blank');
          });
        }
      })
      .catch(err => {
        console.error('Failed to load chapters:', err);
        newContinueBtn.textContent = 'Error loading';
        newContinueBtn.disabled = true;
      });
  }

// Hold-to-repeat variables
let holdInterval = null;
let holdTimeout = null;

function startHoldRepeat(callback, initialDelay = 300, repeatInterval = 50) {
  // Clear any existing intervals
  if (holdInterval) clearInterval(holdInterval);
  if (holdTimeout) clearTimeout(holdTimeout);
  
  // Execute immediately on first press
  callback();
  
  // Wait for initial delay, then start repeating
  holdTimeout = setTimeout(() => {
    holdInterval = setInterval(callback, repeatInterval);
  }, initialDelay);
}

function stopHoldRepeat() {
  if (holdInterval) {
    clearInterval(holdInterval);
    holdInterval = null;
  }
  if (holdTimeout) {
    clearTimeout(holdTimeout);
    holdTimeout = null;
  }
}

	// Fetch and store chapters for navigation
	let sortedChapters = [];
	let currentChapterIndex = -1;
	let currentChapterOriginalIndex = -1;
	// See the desktop card's identical pendingChapterNumber/pendingHasExactMatch:
	// tracks the literal manually-set chapter number so it can still be shown
	// even when no tracked chapter matches it exactly.
	let currentChapterNumber = null;
	let currentChapterHasExactMatch = false;

	fetch(`/api/series/${series.id}/chapters`)
	.then(r => r.json())
	.then(chapters => {
		// Sort chapters using same logic as desktop
		const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
		const useVolume = !hasAnyNullVolume;
		const comparator = useVolume ? compareChapters : (a, b) => a.chapter_number - b.chapter_number;
		sortedChapters = [...chapters].sort(comparator);

		// Find current chapter index
		if (mobileState.pendingChapter === -1) {
		currentChapterIndex = -1;
		} else {
		currentChapterNumber = mobileState.pendingChapter;
		const matches = sortedChapters
			.map((ch, idx) => ({ ch, idx }))
			.filter(item => item.ch.chapter_number === mobileState.pendingChapter);

		if (matches.length === 0) {
			// See the desktop card's identical fallback: fall back to the
			// highest tracked chapter at or below the manually-set target so
			// this reads as "caught up" instead of "not started".
			let fallbackIdx = -1;
			let fallbackChapterNum = -Infinity;
			sortedChapters.forEach((ch, idx) => {
				if (ch.chapter_number <= mobileState.pendingChapter && ch.chapter_number >= fallbackChapterNum) {
					fallbackChapterNum = ch.chapter_number;
					fallbackIdx = idx;
				}
			});
			currentChapterIndex = fallbackIdx;
		} else if (matches.length === 1) {
			currentChapterIndex = matches[0].idx;
			currentChapterHasExactMatch = true;
		} else {
			currentChapterHasExactMatch = true;
			// Multiple matches - pick highest volume if using volumes
			if (useVolume) {
			const best = matches.reduce((a, b) => {
				const volA = a.ch.volume ? parseFloat(a.ch.volume) || 0 : 0;
				const volB = b.ch.volume ? parseFloat(b.ch.volume) || 0 : 0;
				return volB > volA ? b : a;
			});
			currentChapterIndex = best.idx;
			} else {
			currentChapterIndex = matches[0].idx;
			}
		}
		}
		currentChapterOriginalIndex = currentChapterIndex;

		// Update chapter display format
		updateChapterDisplay();
	})
	.catch(err => {
		console.error('Failed to load chapters:', err);
	});

	function updateChapterDisplay() {
	const currentChapterEl = document.getElementById('sheet-current-chapter');
	const showingManualFallback = currentChapterNumber != null
		&& !currentChapterHasExactMatch
		&& currentChapterIndex === currentChapterOriginalIndex;
	if (showingManualFallback) {
		currentChapterEl.textContent = `Ch.${currentChapterNumber}`;
	} else if (currentChapterIndex === -1 || sortedChapters.length === 0) {
		currentChapterEl.textContent = 'Not started';
	} else {
		const ch = sortedChapters[currentChapterIndex];
		if (ch.is_oneshot) {
		currentChapterEl.textContent = 'Oneshot';
		} else if (ch.volume) {
		currentChapterEl.textContent = `Vol.${ch.volume} Ch.${ch.chapter_number}`;
		} else {
		currentChapterEl.textContent = `Ch.${ch.chapter_number}`;
		}
	}

	// ADDED: Update Search and Continue buttons
	updateSheetButtons();
	}

	function updateSheetButtons() {
	const searchBtn = document.getElementById('sheet-search-btn');
	const continueBtn = document.getElementById('sheet-continue-btn');
	const searchChapterSpan = document.getElementById('sheet-search-chapter');
	
	// Determine next chapter to read
	let nextChapterNumber = null;
	let nextChapterUrl = null;
	
	if (sortedChapters.length === 0) {
		// No chapters available
		if (continueBtn) {
		continueBtn.textContent = 'No chapters';
		continueBtn.disabled = true;
		}
		// CHANGED: Always show search button, default to chapter 1
		if (searchBtn && searchChapterSpan) {
		searchBtn.style.display = 'flex';
		searchChapterSpan.textContent = '1';
		}
		return;
	}
	
	if (currentChapterIndex === -1) {
		// Not started - next is first chapter
		nextChapterNumber = sortedChapters[0].chapter_number;
		nextChapterUrl = sortedChapters[0].chapter_url;
	} else if (currentChapterIndex < sortedChapters.length - 1) {
		// Next chapter exists
		nextChapterNumber = sortedChapters[currentChapterIndex + 1].chapter_number;
		nextChapterUrl = sortedChapters[currentChapterIndex + 1].chapter_url;
	} else {
		// Already at last chapter
		if (continueBtn) {
		continueBtn.textContent = 'All caught up';
		continueBtn.disabled = true;
		}
		if (searchBtn) {
		searchBtn.style.display = 'flex';
		// Search for next chapter number (current + 1)
		const searchNext = sortedChapters[currentChapterIndex].chapter_number + 1;
		searchChapterSpan.textContent = searchNext;
		}
		return;
	}
	
	// Update Search button
	if (searchBtn && searchChapterSpan) {
		searchBtn.style.display = 'flex';
		searchChapterSpan.textContent = nextChapterNumber;
	}
	
	// Update Continue button
	if (continueBtn) {
		continueBtn.textContent = `Continue to Ch.${nextChapterNumber}`;
		continueBtn.disabled = false;
		
		// Update click handler
		const newContinueBtn = continueBtn.cloneNode(true);
		continueBtn.parentNode.replaceChild(newContinueBtn, continueBtn);
		
		newContinueBtn.addEventListener('click', () => {
		if (isSafeUrl(nextChapterUrl)) window.open(nextChapterUrl, '_blank');
		});
	}
	}

	// Chapter - button - decrement (NO SAVE, with hold-to-repeat)
	const minusBtn = document.getElementById('sheet-chapter-minus');
	if (minusBtn) {
	const newMinusBtn = minusBtn.cloneNode(true);
	minusBtn.parentNode.replaceChild(newMinusBtn, minusBtn);
	
	const decrementChapter = () => {
		if (currentChapterIndex === -1) {
		stopHoldRepeat();
		return; // Already at "Not started"
		}
		
		if (currentChapterIndex === 0) {
		// Go to "Not started"
		currentChapterIndex = -1;
		mobileState.pendingChapter = -1;
		} else {
		// Go to previous chapter in list
		currentChapterIndex--;
		mobileState.pendingChapter = sortedChapters[currentChapterIndex].chapter_number;
		}
		
		updateChapterDisplay();
		updateSheetProgress(series);
	};
	
	// Mouse events
	newMinusBtn.addEventListener('mousedown', (e) => {
		e.preventDefault();
		startHoldRepeat(decrementChapter);
	});
	newMinusBtn.addEventListener('mouseup', stopHoldRepeat);
	newMinusBtn.addEventListener('mouseleave', stopHoldRepeat);
	
	// Touch events
	newMinusBtn.addEventListener('touchstart', (e) => {
		e.preventDefault();
		startHoldRepeat(decrementChapter);
	});
	newMinusBtn.addEventListener('touchend', stopHoldRepeat);
	newMinusBtn.addEventListener('touchcancel', stopHoldRepeat);
	}

	// Chapter + button - increment (NO SAVE, with hold-to-repeat)
	const plusBtn = document.getElementById('sheet-chapter-plus');
	if (plusBtn) {
	const newPlusBtn = plusBtn.cloneNode(true);
	plusBtn.parentNode.replaceChild(newPlusBtn, plusBtn);
	
	const incrementChapter = () => {
		if (sortedChapters.length === 0) {
		stopHoldRepeat();
		return; // No chapters available
		}
		
		if (currentChapterIndex === -1) {
		// Go to first chapter
		currentChapterIndex = 0;
		mobileState.pendingChapter = sortedChapters[0].chapter_number;
		} else if (currentChapterIndex < sortedChapters.length - 1) {
		// Go to next chapter in list
		currentChapterIndex++;
		mobileState.pendingChapter = sortedChapters[currentChapterIndex].chapter_number;
		} else {
		// Already at last chapter
		stopHoldRepeat();
		return;
		}
		
		updateChapterDisplay();
		updateSheetProgress(series);
	};
	
	// Mouse events
	newPlusBtn.addEventListener('mousedown', (e) => {
		e.preventDefault();
		startHoldRepeat(incrementChapter);
	});
	newPlusBtn.addEventListener('mouseup', stopHoldRepeat);
	newPlusBtn.addEventListener('mouseleave', stopHoldRepeat);
	
	// Touch events
	newPlusBtn.addEventListener('touchstart', (e) => {
		e.preventDefault();
		startHoldRepeat(incrementChapter);
	});
	newPlusBtn.addEventListener('touchend', stopHoldRepeat);
	newPlusBtn.addEventListener('touchcancel', stopHoldRepeat);
	}

}

function updateSheetProgress(series) {
  const progress = mobileState.pendingChapter === -1 ? 0 : (mobileState.pendingChapter / series.latest_chapter) * 100;
  const progressFill = document.getElementById('sheet-progress-fill');
  progressFill.style.width = `${Math.min(progress, 100)}%`;

  // FIXED: Calculate unread using subtraction but round to fix floating-point precision
  let unreadCount = 0;
  if (mobileState.pendingChapter === -1) {
    // Not started - use latest chapter as count
    unreadCount = series.latest_chapter || 0;
  } else {
    // Calculate difference and round to 1 decimal place to fix floating-point errors
    const rawDiff = series.latest_chapter - mobileState.pendingChapter;
    unreadCount = Math.round(rawDiff * 10) / 10;
  }
  
  // Change color to green when caught up
  if (unreadCount === 0 && mobileState.pendingChapter !== -1) {
    progressFill.style.background = '#4ade80';
  } else {
    progressFill.style.background = '#3b82f6';
  }
  
  // Update "behind" text
  const behindElement = document.getElementById('sheet-behind');
  if (unreadCount === 0 && mobileState.pendingChapter !== -1) {
    behindElement.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;margin-right:4px;">
        <path d="M20 6 9 17l-5-5"/>
      </svg>
      <span style="color:#4ade80;font-weight:500;">Up to date</span>
    `;
  } else {
    // Format the number nicely (remove .0 if it's a whole number)
    const displayCount = unreadCount % 1 === 0 ? Math.floor(unreadCount) : unreadCount;
    const chapterWord = unreadCount === 1 ? 'chapter' : 'chapters';
    behindElement.textContent = `${displayCount} ${chapterWord} behind`;
  }
}

async function closeBottomSheet(keepScrollLocked = false) {
  // ADDED: Save pending chapter changes before closing
  if (mobileState.currentSeries && mobileState.pendingChapter !== null &&
      mobileState.pendingChapter !== mobileState.currentSeries.current_chapter) {
    try {
		await saveChapter(mobileState.currentSeries.id, mobileState.pendingChapter, mobileState.currentSeries.current_chapter);
		refreshSeriesCardInPlace(mobileState.currentSeries.id); // Update just this card, same as desktop's btnAccept
    } catch (err) {
      console.error('Failed to save chapter:', err);
      showNotification('Failed to save chapter', 'error');
      return; // Don't close if save failed
    }
  }
  
  mobileState.bottomSheetOpen = false;
  mobileState.currentSeries = null;
  mobileState.pendingChapter = null;

  const bottomSheet = document.getElementById('bottom-sheet');
  const overlay = document.getElementById('bottom-sheet-overlay');
  
  overlay?.classList.remove('active');
  bottomSheet?.classList.remove('active');
  if (bottomSheet) bottomSheet.style.transform = '';
  
  // FIXED: Only unlock scrolling if not keeping it locked
  if (!keepScrollLocked) {
    const savedScrollY = mobileState.scrollY || 0;
    
    // Clear the saved scroll position
    mobileState.scrollY = 0;
    
    // Unlock body styles
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    
    // Force reflow to ensure styles are applied
    document.body.offsetHeight;
    
    // Restore scroll position
    window.scrollTo(0, savedScrollY);
  }
}

// ================================
// MOBILE EDIT MODAL
// ================================
let mobilePendingSourceChanges = {
  hasChanges: false,
  primarySourceId: null,
  originalPrimaryId: null
};

async function openMobileEditModal(series) {
  // ADDED: Store series in mobileState for notifications
  mobileState.currentSeries = series;
  
  const modal = document.getElementById('mobile-edit-modal');
  document.getElementById('mobile-edit-series-id').value = series.id;
  document.getElementById('mobile-edit-status').value = series.status || 'plan_to_read';
  
  // Load sources
  await loadMobileSeriesSources(series.id);
  
  // Load chapters
  fetch(`/api/series/${series.id}/chapters`)
    .then(r => r.json())
    .then(chapters => {
      const select = document.getElementById('mobile-edit-current-chapter');
      select.innerHTML = '<option value="-1">Not started</option>';
      
      const hasAnyNullVolume = chapters.some(ch => ch.volume == null || ch.volume === '');
      const useVolumeLabels = !hasAnyNullVolume;
      const comparator = useVolumeLabels ? compareChapters : (a, b) => a.chapter_number - b.chapter_number;
      const sortedChapters = [...chapters].sort(comparator);
      const numeric = sortedChapters.filter(ch => !ch.is_oneshot).reverse();
      const oneshots = sortedChapters.filter(ch => ch.is_oneshot);
      
      function formatLabel(ch) {
        if (ch.is_oneshot) {
          return oneshots.length === 1 ? "Oneshot" : `Oneshot ${oneshots.indexOf(ch) + 1}`;
        }
        if (useVolumeLabels && ch.volume) {
          return `Vol.${ch.volume} Ch.${ch.chapter_number}`;
        }
        return `Ch.${ch.chapter_number}`;
      }
      
      numeric.forEach(ch => {
        const opt = document.createElement('option');
        opt.value = ch.chapter_number;
        opt.textContent = formatLabel(ch);
        if (ch.chapter_number === parseFloat(series.current_chapter)) {
          opt.selected = true;
        }
        select.appendChild(opt);
      });
      
      oneshots.forEach(ch => {
        const opt = document.createElement('option');
        opt.value = ch.chapter_number;
        opt.textContent = formatLabel(ch);
        if (ch.chapter_number === parseFloat(series.current_chapter)) {
          opt.selected = true;
        }
        select.appendChild(opt);
      });
    })
    .catch(err => console.error('Chapter load error:', err));
  
  // FIXED: Lock scrolling - SAME AS BOTTOM SHEET
  mobileState.scrollY = window.scrollY;
  document.body.style.overflow = 'hidden';
  document.body.style.position = 'fixed';
  document.body.style.width = '100%';
  document.body.style.top = `-${mobileState.scrollY}px`;
  
  modal.classList.remove('hidden');
  
  // Scroll modal to top
  const modalContent = modal.querySelector('.modal-content');
  if (modalContent) {
    modalContent.scrollTop = 0;
  }
}

async function loadMobileSeriesSources(seriesId) {
  try {
    const res = await fetch(`/api/series/${seriesId}/sources`);
    if (!res.ok) throw new Error('Failed to load sources');
    
    const data = await res.json();
    const container = document.getElementById('mobile-sources-list');
    
    if (data.sources.length === 0) {
      container.innerHTML = '<p class="loading-text">No sources found.</p>';
      return;
    }
    
    const primarySource = data.sources.find(s => s.is_primary);
    mobilePendingSourceChanges.originalPrimaryId = primarySource ? primarySource.id : null;
    mobilePendingSourceChanges.primarySourceId = mobilePendingSourceChanges.originalPrimaryId;
    mobilePendingSourceChanges.hasChanges = false;
    
    renderMobileSources(data.sources);
    initializeMobileDragAndDrop();
  } catch (err) {
    console.error('Failed to load sources:', err);
    document.getElementById('mobile-sources-list').innerHTML = '<p class="loading-text">Error loading sources.</p>';
  }
}

function renderMobileSources(sources) {
  const container = document.getElementById('mobile-sources-list');
  
  const sortedSources = [...sources].sort((a, b) => {
    if (a.id === mobilePendingSourceChanges.primarySourceId) return -1;
    if (b.id === mobilePendingSourceChanges.primarySourceId) return 1;
    return 0;
  });
    
  container.innerHTML = sortedSources.map(source => {    
	// FIXED: Always use source.source_type directly (don't read from DOM)
	const sourceTypeLabel = {
      'mangadex': 'MangaDex',
      'kagane': 'Kagane',
      'atsu': 'Atsumaru',
      'asura': 'AsuraScans',
      'hive': 'HiveToons',
      'unknown': 'Unknown'
    }[source.source_type.toLowerCase()] || source.source_type;
    
    const isPrimary = source.id === mobilePendingSourceChanges.primarySourceId;
    
    return `
      <div class="source-item ${isPrimary ? 'primary' : ''}" data-source-id="${source.id}" data-source-type="${escapeHtml(source.source_type)}" draggable="true">
        <div class="source-drag-handle">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="9" cy="5" r="1.5"/>
            <circle cx="9" cy="12" r="1.5"/>
            <circle cx="9" cy="19" r="1.5"/>
            <circle cx="15" cy="5" r="1.5"/>
            <circle cx="15" cy="12" r="1.5"/>
            <circle cx="15" cy="19" r="1.5"/>
          </svg>
        </div>
        <div class="source-info">
          <div class="source-type">
            ${escapeHtml(sourceTypeLabel)}
            ${isPrimary ? '<span class="primary-badge">PRIMARY</span>' : ''}
          </div>
          <div class="source-url">${escapeHtml(source.source_url)}</div>
        </div>
        <div class="source-actions">
          <button class="btn-icon" data-action="open" data-url="${escapeHtml(source.source_url)}" title="Open Source">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
            </svg>
          </button>
          ${!isPrimary && sortedSources.length > 1 ? `
            <button class="btn-icon danger" onclick="removeMobileSource(${document.getElementById('mobile-edit-series-id').value}, ${source.id})" title="Remove Source">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
              </svg>
            </button>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('[data-action="open"]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (isSafeUrl(btn.dataset.url)) window.open(btn.dataset.url, '_blank');
    });
  });
}

function initializeMobileDragAndDrop() {
  const container = document.getElementById('mobile-sources-list');
  if (!container) return;
  
  let draggedElement = null;
  
  // ADDED: Touch support for mobile drag-and-drop
  let touchStartY = 0;
  let touchCurrentY = 0;
  let isDragging = false;
  let draggedTouchElement = null;
  
  container.addEventListener('touchstart', (e) => {
    const sourceItem = e.target.closest('.source-item');
    if (!sourceItem) return;
    
    // Don't drag if touching a button
    if (e.target.closest('button')) return;
    
    draggedTouchElement = sourceItem;
    touchStartY = e.touches[0].clientY;
    isDragging = true;
    
    sourceItem.classList.add('dragging');
  }, { passive: true });
  
  container.addEventListener('touchmove', (e) => {
    if (!isDragging || !draggedTouchElement) return;
    
    touchCurrentY = e.touches[0].clientY;
    const afterElement = getDragAfterElement(container, touchCurrentY);
    
    if (afterElement == null) {
      container.appendChild(draggedTouchElement);
    } else {
      container.insertBefore(draggedTouchElement, afterElement);
    }
  }, { passive: true });
  
  container.addEventListener('touchend', (e) => {
    if (!isDragging || !draggedTouchElement) return;
    
    draggedTouchElement.classList.remove('dragging');
    isDragging = false;
    
    // Update primary source after drag
    const sourceItems = container.querySelectorAll('.source-item:not(.sources-help-text)');
    if (sourceItems.length > 0) {
      const newPrimaryId = parseInt(sourceItems[0].dataset.sourceId);
      
      if (newPrimaryId !== mobilePendingSourceChanges.primarySourceId) {
        mobilePendingSourceChanges.primarySourceId = newPrimaryId;
        mobilePendingSourceChanges.hasChanges = true;
        
        const currentSources = Array.from(sourceItems).map(item => ({
          id: parseInt(item.dataset.sourceId),
          source_type: item.dataset.sourceType, // CHANGED: Read from data attribute
          source_url: item.querySelector('.source-url').textContent,
          is_primary: parseInt(item.dataset.sourceId) === newPrimaryId
        }));
        
        renderMobileSources(currentSources);
        initializeMobileDragAndDrop();
      }
    }
    
    draggedTouchElement = null;
  }, { passive: true });
  
  container.addEventListener('touchcancel', () => {
    if (draggedTouchElement) {
      draggedTouchElement.classList.remove('dragging');
    }
    isDragging = false;
    draggedTouchElement = null;
  }, { passive: true });
  
  // Mouse drag events (for desktop testing)
  container.addEventListener('dragstart', (e) => {
    if (e.target.classList.contains('source-item')) {
      draggedElement = e.target;
      e.target.classList.add('dragging');
    }
  });
  
  container.addEventListener('dragend', (e) => {
    if (e.target.classList.contains('source-item')) {
      e.target.classList.remove('dragging');
      draggedElement = null;
    }
  });
  
  container.addEventListener('dragover', (e) => {
    e.preventDefault();
    const afterElement = getDragAfterElement(container, e.clientY);
    const dragging = document.querySelector('.dragging');
    
    if (afterElement == null) {
      container.appendChild(dragging);
    } else {
      container.insertBefore(dragging, afterElement);
    }
  });
  
  container.addEventListener('drop', (e) => {
    e.preventDefault();
    
    const sourceItems = container.querySelectorAll('.source-item:not(.sources-help-text)');
    if (sourceItems.length > 0) {
      const newPrimaryId = parseInt(sourceItems[0].dataset.sourceId);
      
      if (newPrimaryId !== mobilePendingSourceChanges.primarySourceId) {
        mobilePendingSourceChanges.primarySourceId = newPrimaryId;
        mobilePendingSourceChanges.hasChanges = true;
        
        const currentSources = Array.from(sourceItems).map(item => ({
          id: parseInt(item.dataset.sourceId),
          source_type: item.dataset.sourceType, // CHANGED: Read from data attribute
          source_url: item.querySelector('.source-url').textContent,
          is_primary: parseInt(item.dataset.sourceId) === newPrimaryId
        }));
        
        renderMobileSources(currentSources);
        initializeMobileDragAndDrop();
      }
    }
  });
}

async function removeMobileSource(seriesId, sourceId) {
  if (!confirm('Remove this source? Chapters from this source will remain but won\'t be updated.')) {
    return;
  }
  
  try {
    const res = await fetch(`/api/series/${seriesId}/sources/${sourceId}`, {
      method: 'DELETE'
    });
    
    if (res.ok) {
      // Get series title - use same approach as saveMobileEditModal
      let series = null;
      
      // Try mobileState.currentSeries first
      if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
        series = mobileState.currentSeries;
      }
      
      // Fallback: Fetch from API to get accurate series data
      if (!series) {
        const tempRes = await fetch(`/api/series?page=1&per_page=9999`);
        const tempData = await tempRes.json();
        series = tempData.items.find(s => s.id == seriesId);
      }
      
      const seriesTitle = series?.title || 'Series';
      showNotification(`Source removed from ${seriesTitle}`, 'source_removed');
      
      await loadMobileSeriesSources(seriesId);
    } else {
      const data = await res.json();
      showNotification('Failed to remove source: ' + (data.error || 'Unknown error'), 'error'); // CHANGED

    }
  } catch (err) {
    console.error('Failed to remove source:', err);
    showNotification('Error: ' + err.message, 'error');
  }
}

function showMobileAddSourceForm() {
  document.getElementById('mobile-add-source-form').classList.remove('hidden');
  document.getElementById('mobile-new-source-url').focus();
}

function hideMobileAddSourceForm() {
  document.getElementById('mobile-add-source-form').classList.add('hidden');
  document.getElementById('mobile-new-source-url').value = '';
}

async function addMobileNewSource() {
  const url = document.getElementById('mobile-new-source-url').value.trim();
  const seriesId = document.getElementById('mobile-edit-series-id').value;
  
  if (!url) {
    alert('Please enter a source URL');
    return;
  }
  
  if (!url.startsWith('https://mangadex.org/') && !url.startsWith('https://kagane.to/') && !url.startsWith('https://kagane.org/') && !url.startsWith('https://atsu.moe/') && !url.startsWith('https://asurascans.com/comics/') && !url.startsWith('https://hivetoons.org/series/')) {
    alert('This source is not supported');
    return;
  }
  
  // Check if source already exists in the UI
  const existingSources = document.querySelectorAll('#mobile-sources-list .source-item');
  for (const sourceItem of existingSources) {
    const existingUrl = sourceItem.querySelector('.source-url')?.textContent?.trim();
    if (existingUrl === url) {
      showNotification('Source already exists', 'error');
      return;
    }
  }
  
  try {
    const res = await fetch(`/api/series/${seriesId}/sources`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_url: url })
    });
    
if (res.ok) {
      // Get series title - use same approach as saveMobileEditModal
      let series = null;
      
      // Try mobileState.currentSeries first
      if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
        series = mobileState.currentSeries;
      }
      
      // Fallback: Fetch from API to get accurate series data
      if (!series) {
        const tempRes = await fetch(`/api/series?page=1&per_page=9999`);
        const tempData = await tempRes.json();
        series = tempData.items.find(s => s.id == seriesId);
      }
      
      const seriesTitle = series?.title || 'Series';
      showNotification(`Source added to ${seriesTitle}`, 'source_added');
      
      hideMobileAddSourceForm();
      await loadMobileSeriesSources(seriesId);

    } else {
      const data = await res.json().catch(() => ({}));
      const errorMsg = data.error || 'Unknown error';
      
      if (res.status === 500) {
        showNotification('Failed to add source - please check if it already exists or try again', 'error');
      } else if (errorMsg.toLowerCase().includes('already exists') || errorMsg.toLowerCase().includes('duplicate')) {
        showNotification('Source already exists', 'error');
      } else {
        showNotification(errorMsg, 'error');
      }
    }
  } catch (err) {
    console.error('Failed to add source:', err);
    showNotification('Network error - please try again', 'error');
  }
}

// Mobile Edit Save/Cancel handlers
document.getElementById('mobile-btn-edit-save')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-edit-series-id').value;
  if (!seriesId) {
    alert('No series selected');
    return;
  }
  
  const chapterSelect = document.getElementById('mobile-edit-current-chapter');
  let currentChapterValue = chapterSelect?.value;
  let currentChapterNum = -1;
  if (currentChapterValue !== "-1") {
    currentChapterNum = parseFloat(currentChapterValue);
    if (isNaN(currentChapterNum)) {
      alert('Invalid chapter selection');
      return;
    }
  }
  
  const updates = {
    status: document.getElementById('mobile-edit-status')?.value || 'plan_to_read',
    current_chapter: currentChapterNum
  };
  
  try {
    // FIXED: Fetch specific series by ID to get accurate current values
    // Don't use page list API as it may be filtered by status
    let series = null;
    
    // First try to get from mobileState if it has fresh data
    if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
      series = mobileState.currentSeries;
    }
    
    // Fallback: Fetch ALL series without status filter to find this one
    if (!series) {
      const tempRes = await fetch(`/api/series?page=1&per_page=9999&status=all`);
      const tempData = await tempRes.json();
      series = tempData.items.find(s => s.id == seriesId);
    }
    
    const seriesTitle = series?.title || 'Series';
    const originalStatus = series?.status;
    const originalChapter = series?.current_chapter;
    
    // ADDED: Save source changes first and show notification
    if (mobilePendingSourceChanges.hasChanges) {
      const res = await fetch(`/api/series/${seriesId}/sources/${mobilePendingSourceChanges.primarySourceId}/primary`, {
        method: 'POST'
      });
      
      if (res.ok) {
        showNotification(`Primary source changed for ${seriesTitle}`, 'edit');
        mobilePendingSourceChanges.hasChanges = false;
        mobilePendingSourceChanges.originalPrimaryId = mobilePendingSourceChanges.primarySourceId;
      }
    }
    
	const res = await fetch(`/api/series/${seriesId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
    
	if (res.ok) {
		// ADDED: Show notifications ONLY for actual changes
		let hasChanges = false;
		
		// Check status change
		if (originalStatus !== undefined && updates.status !== originalStatus) {
			const statusMap = {
			'reading': 'Reading',
			'plan_to_read': 'Plan to Read',
			'on_hold': 'On Hold',
			'dropped': 'Dropped',
			'completed': 'Completed'
			};
			const statusText = statusMap[updates.status] || updates.status;
			showNotification(`${seriesTitle} marked as ${statusText}`, 'edit');
			hasChanges = true;
		}
		
		// Check chapter change
		if (originalChapter !== undefined && updates.current_chapter !== originalChapter) {
			const formatChapter = (ch) => ch === -1 ? 'Not started' : `Ch.${ch}`;
			const oldChapterText = formatChapter(originalChapter);
			const newChapterText = formatChapter(updates.current_chapter);
			showNotification(`${seriesTitle} updated from ${oldChapterText} to ${newChapterText}`, 'read');
			hasChanges = true;
		}
		
		// ADDED: Update mobileState.currentSeries with new values to prevent stale data
		if (mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
			mobileState.currentSeries.status = updates.status;
			mobileState.currentSeries.current_chapter = updates.current_chapter;
		}
		
		document.getElementById('mobile-edit-modal').classList.add('hidden');
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
		window.scrollTo(0, mobileState.scrollY || 0);
		loadPage();
	} else {
		const err = await res.json().catch(() => ({}));
		showNotification('Save failed: ' + (err.error || 'Unknown error'), 'error'); // CHANGED
	}
  } catch (e) {
    console.error('Save error:', e);
    showNotification('Network error: ' + e.message, 'error'); // CHANGED
  }
});

document.getElementById('mobile-btn-edit-cancel')?.addEventListener('click', () => {
  if (mobilePendingSourceChanges.hasChanges) {
    if (!confirm('You have unsaved source changes. Discard them?')) {
      return;
    }
  }
  document.getElementById('mobile-edit-modal').classList.add('hidden');
  // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.width = '';
  document.body.style.top = '';
  window.scrollTo(0, mobileState.scrollY || 0);
});

document.getElementById('mobile-btn-reset-not-started')?.addEventListener('click', () => {
  const seriesId = document.getElementById('mobile-edit-series-id').value;
  if (seriesId && mobileState.currentSeries) {
    saveChapter(seriesId, -1, mobileState.currentSeries.current_chapter).then(() => {
      document.getElementById('mobile-edit-modal').classList.add('hidden');
      // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
      document.body.style.overflow = '';
      document.body.style.position = '';
      document.body.style.width = '';
      document.body.style.top = '';
      window.scrollTo(0, mobileState.scrollY || 0);
      loadPage();
    });
  }
});

// Click outside to close Edit modal
document.getElementById('mobile-edit-modal')?.addEventListener('click', (e) => {
  if (e.target.id === 'mobile-edit-modal') {
    if (mobilePendingSourceChanges.hasChanges) {
      if (!confirm('You have unsaved source changes. Discard them?')) {
        return;
      }
    }
    document.getElementById('mobile-edit-modal').classList.add('hidden');
    // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    window.scrollTo(0, mobileState.scrollY || 0);
  }
});

// ================================
// MOBILE SETTINGS MODAL
// ================================
async function openMobileSettingsModal(series) {
  const modal = document.getElementById('mobile-settings-modal');
  document.getElementById('mobile-settings-series-id').value = series.id;
  document.getElementById('mobile-settings-title').value = series.title || '';
  document.getElementById('mobile-settings-cover-url').value = series.cover_url || '';
  
  // FIXED: Check if already locked (coming from bottom sheet)
  const isAlreadyLocked = document.body.style.overflow === 'hidden';
  
  if (!isAlreadyLocked) {
    // Save current scroll position if not already saved
    mobileState.scrollY = window.scrollY;
    
    // Lock scrolling - SIMPLE VERSION
    document.body.style.overflow = 'hidden';
    document.body.style.position = 'fixed';
    document.body.style.width = '100%';
    document.body.style.top = `-${mobileState.scrollY}px`;
  }
  // If already locked, keep existing scroll lock from bottom sheet
  
  modal.classList.remove('hidden');
  
  // Scroll modal to top
  const modalContent = modal.querySelector('.modal-content');
  if (modalContent) {
    modalContent.scrollTop = 0;
  }
}

// Mobile Settings Save/Cancel handlers
document.getElementById('mobile-btn-settings-save')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-settings-series-id').value;
  if (!seriesId) {
    alert('No series selected');
    return;
  }
  
  const updates = {
    title: document.getElementById('mobile-settings-title')?.value || '',
    cover_url: document.getElementById('mobile-settings-cover-url')?.value || ''
  };
  
  try {
    // CHANGED: Fetch current series data to get accurate original values
    let series = null;
    
    if (state.allSeries) {
      series = state.allSeries.find(s => s.id == seriesId);
    }
    
    if (!series && mobileState.currentSeries && mobileState.currentSeries.id == seriesId) {
      series = mobileState.currentSeries;
    }
    
    if (!series) {
      const tempRes = await fetch(`/api/series?page=1&per_page=9999`);
      const tempData = await tempRes.json();
      series = tempData.items.find(s => s.id == seriesId);
    }
    
    const originalTitle = series?.title || '';
    const originalCover = series?.cover_url || '';
    
    const res = await fetch(`/api/series/${seriesId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
    
	if (res.ok) {
		// CHANGED: Show notifications ONLY for actual changes
		const seriesTitle = updates.title || originalTitle || 'Series';
		
		// Check title change - only if actually different
		if (originalTitle && updates.title && updates.title !== originalTitle) {
			showNotification(`${seriesTitle} updated`, 'edit');
		}
		
		// Check cover change - only if actually different
		if (updates.cover_url !== originalCover) {
			showNotification(`${seriesTitle} cover image updated`, 'edit');
		}
		
		const savedScrollY = mobileState.scrollY || 0;
		document.getElementById('mobile-settings-modal').classList.add('hidden');
		
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
		
		setTimeout(() => {
			window.scrollTo(0, savedScrollY);
		}, 0);
		
		loadPage();
	} else {
		const err = await res.json().catch(() => ({}));
		showNotification('Save failed: ' + (err.error || 'Unknown error'), 'error'); // CHANGED
	}  
  } catch (e) {
    console.error('Save error:', e);
    showNotification('Network error: ' + e.message, 'error');
  }
});

document.getElementById('mobile-btn-settings-cancel')?.addEventListener('click', () => {
  const savedScrollY = mobileState.scrollY || 0; // CHANGED: Save before clearing
  document.getElementById('mobile-settings-modal').classList.add('hidden');
  
  // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.width = '';
  document.body.style.top = '';
  
  // CHANGED: Restore scroll after unlocking
  setTimeout(() => {
    window.scrollTo(0, savedScrollY);
  }, 0);
});

document.getElementById('mobile-btn-check-now')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-settings-series-id').value;
  const btn = document.getElementById('mobile-btn-check-now');
  btn.disabled = true;
  btn.textContent = 'Checking...';
  
  try {
    const res = await fetch(`/api/series/${seriesId}/check-now`, { method: 'POST' });
    if (res.ok) {
      btn.textContent = '✓ Done';
      setTimeout(() => {
        btn.textContent = 'Check Now';
        btn.disabled = false;
        loadPage();
      }, 1500);
    } else {
      btn.textContent = 'Error';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Check Now'; }, 1500);
    }
  } catch (e) {
    btn.textContent = 'Fail';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Check Now'; }, 1500);
  }
});

document.getElementById('mobile-btn-delete-series')?.addEventListener('click', async () => {
  const seriesId = document.getElementById('mobile-settings-series-id').value;
  const title = document.getElementById('mobile-settings-title').value;
  
  if (!confirm(`Delete "${title}"? This action cannot be undone.`)) return;
  
  try {
    const res = await fetch(`/api/series/${seriesId}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' }
    });
    
	if (res.ok) {
		// ADDED: Show notification
		showNotification(`${title} deleted`, 'delete');
	
		const savedScrollY = mobileState.scrollY || 0;
		document.getElementById('mobile-settings-modal').classList.add('hidden');
	
		document.body.style.overflow = '';
		document.body.style.position = '';
		document.body.style.width = '';
		document.body.style.top = '';
	
		setTimeout(() => {
			window.scrollTo(0, savedScrollY);
		}, 0);
	
		loadPage();
		if (typeof loadGenres === 'function') {
			loadGenres();
		}
	} else {
		showNotification('Failed to delete series', 'error'); // CHANGED
	}
  } catch (e) {
    showNotification('Network error: ' + e.message, 'error');
  }
});

// Click outside to close Settings modal
document.getElementById('mobile-settings-modal')?.addEventListener('click', (e) => {
  if (e.target.id === 'mobile-settings-modal') {
    const savedScrollY = mobileState.scrollY || 0; // CHANGED: Save before clearing
    document.getElementById('mobile-settings-modal').classList.add('hidden');
    
    // FIXED: Unlock scrolling - SAME AS BOTTOM SHEET
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    
    // CHANGED: Restore scroll after unlocking
    setTimeout(() => {
      window.scrollTo(0, savedScrollY);
    }, 0);
  }
});

// ================================
// BULK TOOLBAR
// ================================
function createBulkToolbar() {
  const existingToolbar = document.getElementById('bulk-toolbar-overlay');
  if (existingToolbar) return;

  const toolbarHTML = `
    <div class="bulk-toolbar-overlay" id="bulk-toolbar-overlay"></div>
    <div class="bulk-toolbar" id="bulk-toolbar">
      <div class="bulk-toolbar-header">
        <button class="btn-close-bulk" id="close-bulk-toolbar">
          <svg class="icon-close" viewBox="0 0 24 24">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="bulk-toolbar-actions">
        <button class="btn-bulk-action read" id="mobile-bulk-read">
          <svg viewBox="0 0 24 24" style="width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;">
            <polyline points="9 11 12 14 22 4"></polyline>
            <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"></path>
          </svg>
          Mark Read (<span id="mobile-bulk-count">0</span>)
        </button>
        <button class="btn-bulk-action edit" id="mobile-bulk-edit">
          <svg class="icon-edit" viewBox="0 0 24 24" style="width:20px;height:20px;">
            <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"></path>
            <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"></path>
          </svg>
          Edit
        </button>
        <button class="btn-bulk-action delete" id="mobile-bulk-delete">
          <svg class="icon-trash" viewBox="0 0 24 24" style="width:20px;height:20px;">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"></path>
          </svg>
          Delete
        </button>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', toolbarHTML);

  document.getElementById('bulk-toolbar-overlay')?.addEventListener('click', exitMobileBulkMode);
  document.getElementById('close-bulk-toolbar')?.addEventListener('click', exitMobileBulkMode);

  // Connect to existing bulk action handlers
  document.getElementById('mobile-bulk-read')?.addEventListener('click', () => {
    document.getElementById('btn-bulk-read')?.click();
  });
  document.getElementById('mobile-bulk-edit')?.addEventListener('click', () => {
    document.getElementById('btn-bulk-edit')?.click();
  });
  document.getElementById('mobile-bulk-delete')?.addEventListener('click', () => {
    document.getElementById('btn-bulk-delete')?.click();
  });
}

// ================================
// BULK EDIT OVERLAY
// ================================
function createBulkEditOverlay() {
  const existingOverlay = document.getElementById('bulk-edit-overlay');
  if (existingOverlay) return;

  const overlayHTML = `
    <div class="bulk-edit-overlay" id="bulk-edit-overlay"></div>
  `;

  document.body.insertAdjacentHTML('beforeend', overlayHTML);

  // Close when clicking overlay
  document.getElementById('bulk-edit-overlay')?.addEventListener('click', closeBulkEditMenu);
}


function openBulkEditMenu() {
  bulkEditMobileState.isMenuOpen = true;
  
  // Save current scroll position
  bulkEditMobileState.scrollY = window.scrollY;
  
  const fabBtn = document.getElementById('fab-bulk-edit');
  const overlay = document.getElementById('bulk-edit-overlay');
  
  // Change FAB to X icon
  if (fabBtn) {
    fabBtn.classList.add('active');
    fabBtn.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="18" y1="6" x2="6" y2="18"></line>
        <line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    `;
  }
  
  // Show overlay
  if (overlay) {
    overlay.classList.add('active');
  }
  
  // Show action FABs with stagger animation (bottom to top)
  setTimeout(() => {
    document.getElementById('fab-bulk-delete-action')?.classList.add('show');
  }, 50);
  setTimeout(() => {
    document.getElementById('fab-bulk-edit-action')?.classList.add('show');
  }, 100);
  setTimeout(() => {
    document.getElementById('fab-bulk-read')?.classList.add('show');
  }, 150);
  
  // Lock scrolling - MORE AGGRESSIVE
  document.body.style.overflow = 'hidden';
  document.body.style.position = 'fixed';
  document.body.style.width = '100%';
  document.body.style.top = `-${bulkEditMobileState.scrollY}px`;
}

function closeBulkEditMenu() {
  bulkEditMobileState.isMenuOpen = false;
  
  const fabBtn = document.getElementById('fab-bulk-edit');
  const overlay = document.getElementById('bulk-edit-overlay');
  
  // Hide action FABs
  document.getElementById('fab-bulk-read')?.classList.remove('show');
  document.getElementById('fab-bulk-edit-action')?.classList.remove('show');
  document.getElementById('fab-bulk-delete-action')?.classList.remove('show');
  
  // Change X back to edit icon
  if (fabBtn) {
    fabBtn.classList.remove('active');
    fabBtn.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
        <path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
      </svg>
    `;
  }
  
  // Hide overlay
  if (overlay) {
    overlay.classList.remove('active');
  }
  
  // Unlock scrolling and restore position
  document.body.style.overflow = '';
  document.body.style.position = '';
  document.body.style.width = '';
  document.body.style.top = '';
  
  // Restore scroll position
  window.scrollTo(0, bulkEditMobileState.scrollY);
}

function toggleBulkEditMenu() {
  if (bulkEditMobileState.isMenuOpen) {
    closeBulkEditMenu();
  } else {
    openBulkEditMenu();
  }
}


function showBulkToolbar() {
  document.getElementById('bulk-toolbar-overlay')?.classList.add('active');
  document.getElementById('bulk-toolbar')?.classList.add('active');
  document.getElementById('mobile-bulk-count').textContent = bulkState.selectedIds.size;
}

function hideBulkToolbar() {
  document.getElementById('bulk-toolbar-overlay')?.classList.remove('active');
  document.getElementById('bulk-toolbar')?.classList.remove('active');
}

function exitMobileBulkMode() {
  exitBulkMode();
  hideBulkToolbar();
}

// Override enterBulkMode for mobile
const originalEnterBulkMode = window.enterBulkMode;
window.enterBulkMode = function() {
  if (isMobileDevice()) {
    bulkState.isBulkMode = true;
    document.querySelectorAll('.series-card').forEach(card => {
      card.classList.add('bulk-mode');
    });
    
    // JUST SWAP FAB BUTTONS
    const fabContainer = document.getElementById('fab-container');
    const bulkFAB = document.getElementById('fab-bulk-edit');
    
    if (fabContainer) fabContainer.classList.add('hidden');
    if (bulkFAB) bulkFAB.style.display = 'flex';
    
    // NEW: Check if we should show back-to-top
    if (window.scrollY > 300) {
      document.getElementById('back-to-top')?.classList.add('show-bulk');
    }
    
  } else {
    originalEnterBulkMode();
  }
};

// Override exitBulkMode for mobile
const originalExitBulkMode = window.exitBulkMode;
window.exitBulkMode = function() {
  if (isMobileDevice()) {
    // Close bulk edit menu if open
    if (bulkEditMobileState.isMenuOpen) {
      closeBulkEditMenu();
    }
    
    bulkState.isBulkMode = false;
    bulkState.selectedIds.clear();
    document.querySelectorAll('.series-card').forEach(card => {
      card.classList.remove('bulk-mode', 'selected');
    });
    
    // JUST SWAP BACK FAB BUTTONS
    const fabContainer = document.getElementById('fab-container');
    const bulkFAB = document.getElementById('fab-bulk-edit');
    
    if (fabContainer) fabContainer.classList.remove('hidden');
    if (bulkFAB) bulkFAB.style.display = 'none';
    
    // NEW: Hide back-to-top when exiting bulk mode
    document.getElementById('back-to-top')?.classList.remove('show-bulk');
    
  } else {
    originalExitBulkMode();
  }
};


// ================================
// FAB BUTTONS
// ================================
function createFABButtons() {
  const existingFAB = document.getElementById('fab-container');
  if (existingFAB) return;

  const fabHTML = `
    <div class="fab-container" id="fab-container">
      <button class="fab-secondary" id="fab-secondary">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:20px;height:20px;">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
        </svg>
      </button>
      <button class="fab-primary" id="fab-primary">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:24px;height:24px;">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4"></path>
        </svg>
      </button>
    </div>
    
    <!-- Bulk Edit FABs (hidden by default) -->
	<button class="fab-bulk-action" id="fab-bulk-read" data-label="Set chapter to last read">
	<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
		<path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path>
	</svg>
	</button>

	<button class="fab-bulk-action" id="fab-bulk-edit-action" data-label="Edit">
	<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
		<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
		<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
	</svg>
	</button>

	<button class="fab-bulk-action" id="fab-bulk-delete-action" data-label="Delete">
	<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
		<path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>
	</svg>
	</button>

	<button class="fab-bulk-edit" id="fab-bulk-edit" style="display: none;">
	<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
		<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
		<path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>
	</svg>
	</button>
  `;

  document.body.insertAdjacentHTML('beforeend', fabHTML);

  document.getElementById('fab-primary')?.addEventListener('click', () => {
    document.getElementById('btn-add-series')?.click();
  });

  // UPDATED: Refresh with rotation animation
  document.getElementById('fab-secondary')?.addEventListener('click', async () => {
    const fabSecondary = document.getElementById('fab-secondary');
    
    if (window.scrollY > 300) {
      // Back to top behavior
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      // Refresh behavior with animation
      if (fabSecondary.disabled) return;
      
      fabSecondary.classList.add('refreshing');
      fabSecondary.disabled = true;

      try {
        updateSourceHealth();
        await loadPage();
      } finally {
        setTimeout(() => {
          fabSecondary.classList.remove('refreshing');
          fabSecondary.disabled = false;
        }, 600);
      }
    }
  });

  // Bulk edit FAB click handler
  document.getElementById('fab-bulk-edit')?.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    toggleBulkEditMenu();
  });

  // Bulk action FAB handlers
  document.getElementById('fab-bulk-read')?.addEventListener('click', () => {
    closeBulkEditMenu();
    document.getElementById('btn-bulk-read')?.click();
  });

  document.getElementById('fab-bulk-edit-action')?.addEventListener('click', () => {
    closeBulkEditMenu();
    document.getElementById('btn-bulk-edit')?.click();
  });

  document.getElementById('fab-bulk-delete-action')?.addEventListener('click', () => {
    closeBulkEditMenu();
    document.getElementById('btn-bulk-delete')?.click();
  });
  
  // Create overlay when FAB is created
  createBulkEditOverlay();
}


function updateFABIcon() {
  const fabSecondary = document.getElementById('fab-secondary');
  if (!fabSecondary) return;

  if (window.scrollY > 300) {
    fabSecondary.innerHTML = `
      <svg class="icon-chevron-up" viewBox="0 0 24 24" style="width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;">
        <polyline points="18 15 12 9 6 15"></polyline>
      </svg>
    `;
  } else {
    fabSecondary.innerHTML = `
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" style="width:20px;height:20px;">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
      </svg>
    `;
  }
}


function hideFABButtons() {
  document.getElementById('fab-container')?.classList.add('hidden');
}

function showFABButtons() {
  document.getElementById('fab-container')?.classList.remove('hidden');
}

// ================================
// SCROLL BEHAVIOR
// ================================
function setupMobileScroll() {
  let ticking = false;

  window.addEventListener('scroll', () => {
    if (!ticking) {
      window.requestAnimationFrame(() => {
        handleMobileScroll();
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });
}

function handleMobileScroll() {
  const currentScrollY = window.scrollY;
  const header = document.querySelector('.header-full');
  const backToTop = document.getElementById('back-to-top');

  // Hide header when scrolling down past 300px
  if (currentScrollY > 300 && currentScrollY > mobileState.lastScrollY) {
    header?.classList.add('hidden-mobile');
  } else if (currentScrollY < 50) {
    header?.classList.remove('hidden-mobile');
  }

  // Update FAB icon
  updateFABIcon();

  // NEW: Show back-to-top in bulk mode when scrolled
  if (bulkState.isBulkMode && currentScrollY > 300) {
    backToTop?.classList.add('show-bulk');
  } else {
    backToTop?.classList.remove('show-bulk');
  }

  mobileState.lastScrollY = currentScrollY;
}


// MOBILE CARD TAP BEHAVIOR - COMPLETE REPLACEMENT
// Add this to dashboard.js around line 1800

// ================================
// CARD TAP BEHAVIOR
// ================================

// Override renderSeriesCard to add mobile tap behavior
const originalRenderSeriesCard = window.renderSeriesCard;
window.renderSeriesCard = function(series) {
  const card = originalRenderSeriesCard(series);
  
  if (isMobileDevice()) {
    // Hide checkbox completely on mobile
    const checkbox = card.querySelector('.series-checkbox');
    if (checkbox) {
      checkbox.style.display = 'none';
    }

    // Long-press variables
    let pressTimer = null;
    let touchStartTime = 0;
    let touchStartY = 0;
    const longPressDuration = 500;
    let touchMoved = false;
    let longPressTriggered = false;

    card.addEventListener('touchstart', (e) => {
      if (e.target.closest('button') || e.target.closest('a')) return;
      
      touchStartTime = Date.now();
      touchStartY = e.touches[0].clientY;
      touchMoved = false;
      longPressTriggered = false;
      
      pressTimer = setTimeout(() => {
        if (!touchMoved) {
          longPressTriggered = true;
          toggleCardSelection(series.id);
          
          card.style.transform = 'scale(0.95)';
          setTimeout(() => {
            card.style.transform = '';
          }, 100);
        }
      }, longPressDuration);
    });

    card.addEventListener('touchmove', (e) => {
      const touchY = e.touches[0].clientY;
      const deltaY = Math.abs(touchY - touchStartY);
      
      if (deltaY > 10) {
        touchMoved = true;
        clearTimeout(pressTimer);
      }
    });

    card.addEventListener('touchend', (e) => {
      clearTimeout(pressTimer);
      
      const touchDuration = Date.now() - touchStartTime;
      
      // If long-press was triggered, don't do anything on touchend
      if (longPressTriggered) {
        longPressTriggered = false;
        return;
      }
      
      // Short tap - only if not moved and wasn't a long press
      if (touchDuration < longPressDuration && !touchMoved) {
        if (e.target.closest('button') || e.target.closest('a')) return;
        
        e.preventDefault();
        
        if (bulkState.isBulkMode) {
          toggleCardSelection(series.id);
        } else {
          openBottomSheet(series);
        }
      }
    });

    card.addEventListener('touchcancel', () => {
      clearTimeout(pressTimer);
      touchMoved = false;
      longPressTriggered = false;
    });
  }
  
  return card;
};

// ================================
// INITIALIZATION
// ================================
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initMobile);
} else {
  initMobile();
}

// Re-initialize on window resize
let resizeTimeout;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimeout);
  resizeTimeout = setTimeout(() => {
    if (isMobileDevice()) {
      initMobile();
    }
  }, 250);
});

console.log('[Mobile] Script loaded');