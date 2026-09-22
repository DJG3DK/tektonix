'use strict';

/** The states a badge can be in, and the label each one shows. */
const STATES = {
  ok: 'Passing',
  warn: 'Needs attention',
  error: 'Failed',
};

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/**
 * Render a status badge.
 * Unknown states fall back to `ok` rather than rendering nothing.
 */
function renderBadge(state, { label } = {}) {
  const known = Object.prototype.hasOwnProperty.call(STATES, state) ? state : 'ok';
  const text = label === undefined ? STATES[known] : label;
  return `<span class="badge badge--${known}">${escapeHtml(text)}</span>`;
}

module.exports = { renderBadge, STATES };
