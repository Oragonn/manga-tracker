// web/static/js/tag_activity.js
//
// How tag rule events (merge / unmerge / ban / unban) read in the UI. Shared by
// the Activity Log page and the Fixes page's Tag tab so both word them the same
// way. Everything returned here is PLAIN TEXT (tag names are scraped or typed
// input) -- callers must set it with textContent or run it through escapeHtml
// before it goes into innerHTML.

const TAG_ACTIVITY_TYPES = ['tag_merged', 'tag_unmerged', 'tag_banned', 'tag_unbanned'];

const TAG_ACTIVITY_ICONS = {
  tag_merged: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/></svg>',
  tag_unmerged: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/></svg>',
  tag_banned: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M6 6l12 12"/></svg>',
  tag_unbanned: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M6 6l12 12"/></svg>'
};

function isTagActivity(actionType) {
  return TAG_ACTIVITY_TYPES.includes(actionType);
}

// "a", "b" and 3 more
function formatTagNames(names, max = 5) {
  const list = (names || []).map(name => `"${name}"`);
  return list.length <= max
    ? list.join(', ')
    : `${list.slice(0, max).join(', ')} and ${list.length - max} more`;
}

function parseLogValue(raw) {
  try {
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

// One-line description of a tag entry from /api/logs, e.g.
// Merged "Elf", "elves" into "Elven"
function tagActivityText(log) {
  const oldVal = parseLogValue(log.old_value);
  const newVal = parseLogValue(log.new_value);
  switch (log.action_type) {
    case 'tag_merged':
      return `Merged ${formatTagNames(newVal?.sources)} into "${newVal?.target ?? ''}"`;
    case 'tag_unmerged':
      return `Unmerged ${formatTagNames(oldVal?.tags)} from "${oldVal?.target ?? ''}"`;
    case 'tag_banned':
      return `Banned ${formatTagNames(newVal?.tags)}`;
    case 'tag_unbanned':
      return `Unbanned "${oldVal?.tag ?? ''}"`;
    default:
      return '';
  }
}

// What undoing a tag entry just did, for the confirmation toast. `name` is the
// entry's title (the merge target, or the tag name(s)).
function tagUndoMessage(actionType, name) {
  switch (actionType) {
    case 'tag_merged': return `Undid merge into "${name}"`;
    case 'tag_unmerged': return `Restored merge into "${name}"`;
    case 'tag_banned': return `Unbanned ${name}`;
    case 'tag_unbanned': return `Banned "${name}" again`;
    default: return `Undone: ${name}`;
  }
}
