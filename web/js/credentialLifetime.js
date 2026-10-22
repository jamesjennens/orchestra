// Shared secret-free expiry display and explicit extension control.
import { h, confirmDialog } from './dom.js';
import { act } from './ui.js';

export function expiryLine(credential) {
  const date = new Date(credential.expires_at);
  const label = Number.isNaN(date.getTime()) ? (credential.expires_at || 'Unknown expiry') : date.toLocaleString();
  return h('span', { class: 'small', 'data-expiry': credential.id },
    credential.revoked ? 'Revoked; expiry ' : credential.working === false ? 'Expired; expiry ' : 'Expires ',
    h('time', { datetime: credential.expires_at, title: credential.expires_at }, label),
    credential.expires_soon && credential.working !== false && !credential.revoked ?
      h('span', { class: 'chip warn' }, 'Within seven days') : null);
}

export function extensionButton(agent, credential, extend, refresh) {
  return h('button', { type: 'button', 'data-extend': credential.id,
    'aria-label': 'Extend existing credential ' + credential.id + ' for ' + agent.name,
    onclick: async (event) => {
      if (!(await confirmDialog({ title: 'Extend this existing credential?',
        body: 'Its existing secret keeps working. This extends the expiry without issuing or showing a secret, changing scopes, or reviving an expired or revoked key.',
        confirmLabel: 'Extend existing credential' }))) return;
      try { await act(event.currentTarget, extend, { success: 'Existing credential extended' }); }
      catch { return; } // Retain the row and same intent for a refused or uncertain retry.
      refresh();
    } }, 'Extend existing credential');
}
