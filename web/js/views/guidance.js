import { h, confirmDialog } from '../dom.js';
import { describe } from '../ui.js';

// Read and edit hosts are independent: acknowledging or reloading never destroys
// an unsaved guidance/onboarding draft. All document text is a text node.
export function guidancePanel(ctx, project) {
  const pid = project.id;
  const panel = h('section', { class: 'panel stack', 'aria-label': 'Guidance and onboarding' });
  const guidanceRead = h('div'), onboardingRead = h('div'), roster = h('div');
  const guidanceEdit = h('div'), onboardingEdit = h('div');
  const message = h('div', { role: 'status' });
  let writing = false, pending = null, guidanceEditor = false, onboardingEditor = false;
  let guidanceRevision = 0, onboardingRevision = 0, rosterRevision = 0;

  function failure(error) {
    message.replaceChildren(h('p', { role: 'alert' }, describe(error)));
    if (!error.retry) return;
    pending = error.retry;
    message.append(h('p', null, 'The outcome is unknown. Keep this page open and retry the identical request before making another change.'));
    message.append(h('button', { type: 'button', onclick: async (event) => {
      if (writing) return;
      const retry = pending;
      await perform(event.currentTarget, retry, true);
    } }, 'Retry the same request'));
  }
  async function perform(button, work, retry = false) {
    if (writing || (pending && !retry)) return;
    writing = true; button.disabled = true;
    try {
      const result = await work(); pending = null;
      message.replaceChildren(h('p', null, result?.operator_text_kept_as
        ? 'Saved. The operator document is retained beside ONBOARDING.md as ' + result.operator_text_kept_as + '.' : 'Saved.'));
      await Promise.all([readGuidance(), readOnboarding()]);
    } catch (error) {
      if (retry) pending = null;
      failure(error);
    } finally { writing = false; button.disabled = false; }
  }
  function writeButton(label, work, confirm) {
    return h('button', { type: 'button', onclick: async (event) => {
      if (writing || pending) return;
      if (confirm && !await confirmDialog({ title: label, body: confirm, confirmLabel: label, danger: true })) return;
      await perform(event.currentTarget, work);
    } }, label);
  }
  function showGuidanceEditor() {
    if (guidanceEditor || project.archived) return;
    guidanceEditor = true;
    const input = h('textarea', { rows: 6, 'aria-label': 'New guidance text', maxlength: 8000 });
    guidanceEdit.replaceChildren(h('details', null, h('summary', null, 'Set or clear guidance'),
      h('p', null, 'Set guidance deliberately. Members and agents must explicitly acknowledge the new exact version. Text is stored using the existing guidance limits.'),
      input, writeButton('Set guidance', () => ctx.api.setGuidance(pid, input.value)),
      writeButton('Clear guidance', () => ctx.api.clearGuidance(pid), 'Remove current guidance and its acknowledgement record?')));
  }
  async function showOnboardingEditor() {
    if (onboardingEditor || project.archived) return;
    onboardingEditor = true;
    const input = h('textarea', { rows: 6, 'aria-label': 'Project onboarding text', maxlength: 8000 });
    const hint = h('p', null, 'Every project member and agent can read the whole onboarding document, including operator text. Replacing operator text retains ONBOARDING.operator-copy.md beside ONBOARDING.md. The stored header and line marks count toward the 8000-byte limit.');
    onboardingEdit.replaceChildren(h('details', null, h('summary', null, 'Edit project onboarding'), hint, input,
      writeButton('Save onboarding', () => ctx.api.setOnboarding(pid, input.value), 'Replace the current project onboarding document? An operator document is retained as ONBOARDING.operator-copy.md.'),
      writeButton('Clear web onboarding', () => ctx.api.clearOnboarding(pid), 'Remove web-written onboarding? An operator document cannot be cleared here.')));
    // This distinct edit read withholds operator text. Do not insert the member
    // document (with its stored header/marks) into the editable field.
    let touched = false; input.addEventListener('input', () => { touched = true; });
    try {
      const value = await ctx.api.onboarding(pid);
      if (!touched && value.source === 'web') input.value = value.text || '';
    } catch (error) { hint.append(' Existing editable text could not be loaded: ' + describe(error)); }
  }
  async function readGuidance() {
    const revision = ++guidanceRevision;
    if (!ctx.api.guidance) return;
    try {
      const value = await ctx.api.guidance(pid);
      if (revision !== guidanceRevision) return;
      const readable = value.present && !value.unreadable && !value.unbound
        && typeof value.text === 'string' && typeof value.version === 'string';
      guidanceRead.replaceChildren(h('div', null, h('h3', null, 'Current guidance'),
        value.warning ? h('p', { role: 'alert' }, value.warning) : null,
        value.next_action ? h('p', null, value.next_action) : null,
        readable ? h('div', null, h('p', null, 'Set by ' + value.set_by + ' at ' + value.set_at),
          h('p', null, 'Version: ' + value.version), h('pre', null, value.text),
          h('p', null, value.acknowledged ? 'You acknowledged this version.' : 'You have not acknowledged this version.'),
          !value.acknowledged && !project.archived
            ? writeButton('Acknowledge displayed version', () => ctx.api.ackGuidance(pid, value.version)) : null)
          : h('p', null, value.present || value.unreadable || value.unbound ? 'Guidance text is withheld until repaired.' : 'No guidance is set.'),
        h('button', { type: 'button', onclick: readGuidance }, 'Read guidance again')));
      if (value.can_coordinate) {
        showGuidanceEditor();
        if (!project.archived && !roster.childNodes.length) roster.replaceChildren(h('button', { type: 'button', onclick: () => readRoster(0) }, 'Who acknowledged guidance?'));
      } else { guidanceEdit.replaceChildren(); guidanceEditor = false; roster.replaceChildren(); }
    } catch (error) {
      if (revision !== guidanceRevision) return;
      guidanceRead.replaceChildren(h('h3', null, 'Current guidance'), h('p', { role: 'alert' }, describe(error)),
        h('button', { type: 'button', onclick: readGuidance }, 'Read guidance again'));
    }
  }
  async function readRoster(offset) {
    const revision = ++rosterRevision;
    try {
      const value = await ctx.api.guidanceStatus(pid, offset, 50);
      if (revision !== rosterRevision) return;
      roster.replaceChildren(h('div', null, h('h3', null, 'Guidance acknowledgements'),
        h('p', null, value.retention || 'At most 500 acknowledgements are retained, for current and previous versions. Compaction and eviction remove history; this is not a complete history.'),
        h('ul', null, (value.acknowledged || []).map(row => h('li', null,
          row.actor + ': ' + (row.current ? 'current' : 'older version') + ' (' + row.version + ') at ' + row.acknowledged_at))),
        h('p', null, 'Showing ' + (value.acknowledged || []).length + ' of ' + value.acknowledged_total + ' retained project acknowledgements.'),
        offset > 0 ? h('button', { type: 'button', onclick: () => readRoster(Math.max(0, offset - 50)) }, 'Previous acknowledgements') : null,
        value.next_offset != null ? h('button', { type: 'button', onclick: () => readRoster(value.next_offset) }, 'More acknowledgements') : null,
        h('button', { type: 'button', onclick: () => readRoster(offset) }, 'Refresh acknowledgements')));
    } catch (error) {
      if (revision === rosterRevision) roster.replaceChildren(h('p', { role: 'alert' }, describe(error)),
        h('button', { type: 'button', onclick: () => readRoster(offset) }, 'Read acknowledgements again'));
    }
  }
  async function readOnboarding() {
    const revision = ++onboardingRevision;
    if (!ctx.api.memberOnboarding) return;
    try {
      const value = await ctx.api.memberOnboarding(pid);
      if (revision !== onboardingRevision) return;
      onboardingRead.replaceChildren(h('div', null, h('h3', null, 'Project onboarding'),
        h('p', null, value.source === 'operator' ? 'Written by an operator on the server.' : value.source === 'web' ? 'Written in the web interface on project authority.' : 'No project onboarding is set.'),
        typeof value.text === 'string' ? h('pre', null, value.text) : null,
        h('button', { type: 'button', onclick: readOnboarding }, 'Read onboarding again')));
      if (value.can_coordinate) await showOnboardingEditor();
      else { onboardingEdit.replaceChildren(); onboardingEditor = false; }
    } catch (error) {
      if (revision !== onboardingRevision) return;
      onboardingRead.replaceChildren(h('h3', null, 'Project onboarding'), h('p', { role: 'alert' }, describe(error)),
        h('button', { type: 'button', onclick: readOnboarding }, 'Read onboarding again'));
    }
  }
  panel.append(h('h2', null, 'Guidance and onboarding'), guidanceRead, guidanceEdit, roster, onboardingRead, onboardingEdit, message);
  readGuidance(); readOnboarding();
  return panel;
}
