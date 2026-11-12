// Review standards register, gate switch, rule-owner edits, and draft decisions (kittrial-5bb.570).
import { h, time } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, describe, errorState, actorWords } from '../ui.js';
import { canWrite, canApprove, crumbs } from './project.js';

export function gateForm(ctx, pid, gate, onSaved) {
  const currentMode = (gate && gate.mode) || 'warn';
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true, 'data-form': 'standards-gate' },
    status,
    h('div', { class: 'form-row' },
      field({ id: 'sg-mode', label: 'Gate mode', type: 'select', value: currentMode, options: [
        ['warn', 'warn — record unresolved must rules without refusing approval'],
        ['block', 'block — refuse approval when applicable must rules are unresolved'],
      ] }),
      field({ id: 'sg-reason', label: 'Reason', required: true, maxlength: 500, placeholder: 'Why the gate mode is changing' })),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, 'Save gate mode')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const reason = (v['sg-reason'] || '').trim();
    if (!reason) return setFieldError(form, 'sg-reason', 'Enter a reason for switching the gate mode.');
    setFieldError(form, 'sg-reason', '');
    status.hidden = true;
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.setStandardsGate(pid, { mode: v['sg-mode'], reason }), {
        success: `Standards gate set to ${v['sg-mode']}`,
        onError: (error) => { status.replaceChildren(describe(error)); status.hidden = false; return true; },
      });
    } catch { return; }
    if (status.hidden && onSaved) onSaved();
  });
  return form;
}

export function ruleEditForm(ctx, pid, rule, expectedVersion, onSaved) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const initialJson = JSON.stringify({
    statement: rule.statement,
    rationale: rule.rationale,
    severity: rule.severity,
    applicability: rule.applicability || { all_changes: true },
    check: rule.check || { kind: 'judgement' },
    references: rule.references && rule.references.length ? rule.references : undefined,
  }, null, 2);
  const form = h('form', { class: 'form', novalidate: true, 'data-form': 'standards-rule-edit', 'data-rule-id': rule.id },
    status,
    field({ id: `re-json-${rule.id}`, label: `Rule ${rule.id} fields (JSON)`, type: 'textarea', rows: 6, value: initialJson }),
    field({ id: `re-reason-${rule.id}`, label: 'Reason for rule update', required: true, maxlength: 500 }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, `Update ${rule.id}`)));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const reason = (v[`re-reason-${rule.id}`] || '').trim();
    if (!reason) return setFieldError(form, `re-reason-${rule.id}`, 'Enter a reason for updating this rule.');
    setFieldError(form, `re-reason-${rule.id}`, '');
    let parsed;
    try { parsed = JSON.parse(v[`re-json-${rule.id}`] || '{}'); }
    catch { return setFieldError(form, `re-json-${rule.id}`, 'Enter valid JSON for the rule fields.'); }
    setFieldError(form, `re-json-${rule.id}`, '');
    status.hidden = true;
    const body = { expected_version: expectedVersion, reason, rule: { ...parsed, id: rule.id, owner: rule.owner } };
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.editStandardsRule(pid, rule.id, body), {
        success: `Updated ${rule.id}`,
        onError: (error) => { status.replaceChildren(describe(error)); status.hidden = false; return true; },
      });
    } catch { return; }
    if (status.hidden && onSaved) onSaved();
  });
  return form;
}

export function registerPutForm(ctx, pid, reg, onSaved) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const expectedVersion = (reg && reg.version) || 0;
  const localRules = ((reg && reg.rules) || []).filter((r) => !r.origin || r.origin.kind === 'project');
  const template = JSON.stringify({
    rules: localRules.map(({ origin, owner_status, effective_owner, ...rest }) => rest),
    inherits: (reg && reg.inherits) || null,
    shared: Boolean(reg && reg.shared),
    all_changes_must_limit: (reg && reg.all_changes_must_limit) || 5,
    named_editors: (reg && reg.named_editors) || [],
  }, null, 2);
  const form = h('form', { class: 'form', novalidate: true, 'data-form': 'standards-put' },
    status,
    field({ id: 'sp-json', label: 'Register definition (JSON)', type: 'textarea', rows: 10, value: template,
      hint: `Expected current version: ${expectedVersion}. New rules without explicit severity default to should.` }),
    field({ id: 'sp-reason', label: 'Reason for register update', required: true, maxlength: 500 }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, expectedVersion ? 'Publish new register version' : 'Create register v1')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const reason = (v['sp-reason'] || '').trim();
    if (!reason) return setFieldError(form, 'sp-reason', 'Enter a reason for this register change.');
    setFieldError(form, 'sp-reason', '');
    let parsed;
    try { parsed = JSON.parse(v['sp-json'] || '{}'); }
    catch { return setFieldError(form, 'sp-json', 'Enter a valid JSON object.'); }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return setFieldError(form, 'sp-json', 'Register definition must be a JSON object.');
    }
    setFieldError(form, 'sp-json', '');
    status.hidden = true;
    const body = { ...parsed, expected_version: expectedVersion, reason };
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.putStandards(pid, body), {
        success: 'Standards register updated',
        onError: (error) => { status.replaceChildren(describe(error)); status.hidden = false; return true; },
      });
    } catch { return; }
    if (status.hidden && onSaved) onSaved();
  });
  return form;
}

export function draftProposeForm(ctx, pid, onSaved) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const sampleRule = JSON.stringify({
    id: 'STD-01',
    statement: 'Observable standard to check on applicable changes.',
    rationale: 'Why this standard prevents defects.',
    owner: (ctx.me && ctx.me.id) || 'usr_0000000000000001',
    severity: 'should',
    applicability: { all_changes: true },
    check: { kind: 'judgement' },
  }, null, 2);
  const form = h('form', { class: 'form', novalidate: true, 'data-form': 'standards-draft-propose' },
    status,
    field({ id: 'dp-action', label: 'Draft action', type: 'select', value: 'upsert', options: [
      ['upsert', 'Propose new or updated rule'],
      ['remove', 'Propose removing a rule'],
    ] }),
    field({ id: 'dp-rule-id', label: 'Rule ID (when removing)', placeholder: 'e.g. STD-01' }),
    field({ id: 'dp-rule', label: 'Proposed rule (JSON, for upsert)', type: 'textarea', rows: 8, value: sampleRule }),
    field({ id: 'dp-reason', label: 'Reason for draft', required: true, maxlength: 500 }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, 'Propose draft')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const reason = (v['dp-reason'] || '').trim();
    if (!reason) return setFieldError(form, 'dp-reason', 'Enter a reason for proposing this draft.');
    setFieldError(form, 'dp-reason', '');
    const action = v['dp-action'] || 'upsert';
    let body;
    if (action === 'remove') {
      const rid = (v['dp-rule-id'] || '').trim();
      if (!rid) return setFieldError(form, 'dp-rule-id', 'Enter the rule ID to remove.');
      setFieldError(form, 'dp-rule-id', '');
      body = { action: 'remove', rule_id: rid, reason };
    } else {
      let rule;
      try { rule = JSON.parse(v['dp-rule'] || '{}'); }
      catch { return setFieldError(form, 'dp-rule', 'Enter valid JSON for the proposed rule.'); }
      setFieldError(form, 'dp-rule', '');
      body = { action: 'upsert', rule, reason };
    }
    status.hidden = true;
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.proposeStandardsDraft(pid, body), {
        success: 'Draft proposed',
        onError: (error) => { status.replaceChildren(describe(error)); status.hidden = false; return true; },
      });
    } catch { return; }
    if (status.hidden && onSaved) onSaved();
  });
  return form;
}

export function draftDecisionForm(ctx, pid, draft, expectedVersion, onSaved) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true, 'data-form': 'standards-draft-decide', 'data-draft-id': draft.id },
    status,
    field({ id: `dd-reason-${draft.id}`, label: 'Decision reason', required: true, maxlength: 500, placeholder: 'Why you are accepting or rejecting this draft' }),
    h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'primary', 'data-decision': 'accept', onclick: (e) => submitDecision(e.currentTarget, 'accept') }, 'Accept draft'),
      h('button', { type: 'button', class: 'danger', 'data-decision': 'reject', onclick: (e) => submitDecision(e.currentTarget, 'reject') }, 'Reject draft')));
  async function submitDecision(button, decision) {
    const v = formValues(form);
    const reason = (v[`dd-reason-${draft.id}`] || '').trim();
    if (!reason) { setFieldError(form, `dd-reason-${draft.id}`, 'Enter a reason before deciding this draft.'); return; }
    setFieldError(form, `dd-reason-${draft.id}`, '');
    status.hidden = true;
    const body = decision === 'accept'
      ? { decision: 'accept', reason, expected_version: expectedVersion }
      : { decision: 'reject', reason };
    try {
      await act(button, () => ctx.api.decideStandardsDraft(pid, draft.id, body), {
        success: decision === 'accept' ? 'Draft accepted' : 'Draft rejected',
        onError: (error) => { status.replaceChildren(describe(error)); status.hidden = false; return true; },
      });
    } catch { return; }
    if (status.hidden && onSaved) onSaved();
  }
  return form;
}

export async function page(ctx, { pid }) {
  let project, reg, drafts, versions;
  try {
    [project, reg, drafts, versions] = await Promise.all([
      ctx.api.project(pid),
      ctx.api.standards(pid),
      ctx.api.standardsDrafts(pid),
      ctx.api.standardsVersions(pid),
    ]);
  } catch (error) {
    return errorState(error, () => ctx.render());
  }
  const coordinator = canApprove(project) && !project.archived;
  const writer = canWrite(project) && !project.archived;
  const myId = ctx.me && ctx.me.id;
  const namedEditor = Array.isArray(reg.named_editors) && reg.named_editors.includes(myId);
  const canEditRegister = (coordinator || namedEditor) && !project.archived;
  const rules = Array.isArray(reg.effective_rules) ? reg.effective_rules : (Array.isArray(reg.rules) ? reg.rules : []);
  const expectedVersion = reg.version || 0;
  const gate = reg.gate || project.standards_gate || { mode: 'warn', source: 'default' };
  const reload = () => ctx.render();

  const lintBanner = Array.isArray(reg.lint_warnings) && reg.lint_warnings.length
    ? h('div', { class: 'banner', role: 'note' },
      h('div', null, h('strong', null, 'Register lint warnings:'),
        h('ul', null, reg.lint_warnings.map((w) => h('li', null, w)))))
    : null;

  const damagedBanner = reg.status === 'damaged'
    ? h('div', { class: 'banner crit', role: 'alert' }, reg.warning || 'The standards register is damaged.')
    : null;

  const gatePanel = h('section', { class: 'panel' },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'small' }, 'Standards gate'),
      h('span', { class: 'chip ' + (gate.mode === 'block' ? 'warn' : 'plain') }, `Mode: ${gate.mode}`)),
    h('div', { class: 'panel-body' },
      h('p', { class: 'small muted' },
        gate.mode === 'block'
          ? 'Approvals are refused when applicable must rules lack passing or waived check records.'
          : 'Warn mode: unresolved must rules are recorded on the delivery sign-off without refusing approval.',
        gate.set_by ? [' Last changed by ', actorWords(null, gate.set_by, 'someone'), gate.set_at ? [' ', time(gate.set_at)] : null, '.'] : ' (default).'),
      coordinator ? gateForm(ctx, pid, gate, reload) : null));

  const rulesBody = rules.length
    ? h('div', { class: 'table-wrap' }, h('table', { class: 'standards-table' },
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, 'Rule'),
        h('th', { scope: 'col' }, 'Severity'),
        h('th', { scope: 'col' }, 'Applies to'),
        h('th', { scope: 'col' }, 'Check'),
        h('th', { scope: 'col' }, 'Owner & origin'))),
      h('tbody', null, rules.map((r) => {
        const isInherited = r.origin && (r.origin.inherited || r.origin.kind === 'inherited');
        const originText = isInherited
          ? `Inherited from ${r.origin.project} v${r.origin.version}`
          : `Project v${(r.origin && r.origin.version) || expectedVersion}`;
        const canEditThisRule = !project.archived && !(isInherited && r.severity === 'must') && (coordinator || r.owner === myId);
        const appliesSummary = r.applicability && r.applicability.all_changes
          ? 'all_changes'
          : ['paths', 'languages', 'task_labels', 'task_kinds'].filter((k) => r.applicability && Array.isArray(r.applicability[k]) && r.applicability[k].length).join(', ') || 'all_changes';
        return h('tr', null,
          h('td', null,
            h('div', { class: 'mono strong' }, r.id),
            h('p', { class: 'small prose' }, r.statement),
            r.rationale ? h('p', { class: 'small muted' }, r.rationale) : null,
            canEditThisRule ? h('details', { class: 'fold' },
              h('summary', null, `Edit ${r.id}`),
              h('div', { class: 'fold-body' }, ruleEditForm(ctx, pid, r, expectedVersion, reload))) : null),
          h('td', null, h('span', { class: 'chip ' + (r.severity === 'must' ? 'warn' : 'plain') }, r.severity)),
          h('td', { class: 'small mono' }, appliesSummary),
          h('td', { class: 'small' }, r.check ? r.check.kind : 'judgement'),
          h('td', { class: 'small' },
            h('div', null, 'Owner: ', h('span', { class: 'mono' }, r.owner),
              r.owner_status && r.owner_status !== 'active' ? [' ', h('span', { class: 'chip warn' }, r.owner_status)] : null),
            h('div', { class: 'muted' }, originText)));
      }))))
    : empty('No review standards register yet', 'Until version 1 is created, reviews and approvals behave exactly as in a project without standards.');

  const registerPanel = h('section', { class: 'panel' },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'small' }, reg.version ? `Effective register (v${reg.version})` : 'Review standards register'),
      reg.inherits ? h('span', { class: 'chip plain' }, `Inherits ${reg.inherits}`) : null,
      reg.shared ? h('span', { class: 'chip ok' }, 'Shared register') : null),
    rulesBody,
    canEditRegister ? h('details', { class: 'fold panel-body' },
      h('summary', null, reg.version ? 'Update full register (coordinator / named editor)' : 'Create standards register v1'),
      h('div', { class: 'fold-body' }, registerPutForm(ctx, pid, reg, reload))) : null);

  const draftItems = Array.isArray(drafts && drafts.items) ? drafts.items : [];
  const draftsPanel = h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Rule drafts ', h('span', { class: 'nav-count' }, String(draftItems.length)))),
    h('div', { class: 'panel-body stack' },
      draftItems.length ? draftItems.map((d) => {
        const targetRule = rules.find((r) => r.id === d.rule_id);
        const canDecide = !project.archived && d.status === 'pending' && (coordinator || namedEditor || (targetRule && targetRule.owner === myId && d.action === 'upsert'));
        return h('div', { class: 'standards-draft' },
          h('div', { class: 'chips' },
            h('span', { class: 'mono strong' }, d.id),
            h('span', { class: 'chip ' + (d.status === 'accepted' ? 'ok' : d.status === 'rejected' ? 'warn' : 'plain') }, d.status),
            h('span', { class: 'mono small' }, `${d.action} ${d.rule_id}`)),
          h('p', { class: 'small' }, d.reason, ' — proposed by ', h('span', { class: 'mono' }, d.proposed_by), ' ', time(d.created_at)),
          d.rule ? h('details', null, h('summary', { class: 'small' }, 'Proposed rule JSON'), h('pre', { class: 'small' }, JSON.stringify(d.rule, null, 2))) : null,
          canDecide ? draftDecisionForm(ctx, pid, d, expectedVersion, reload) : null);
      }) : empty('No drafts', 'Contributors and agents can propose rule drafts for a person owner or coordinator to decide.'),
      writer ? h('details', { class: 'fold' }, h('summary', null, 'Propose a rule draft'), h('div', { class: 'fold-body' }, draftProposeForm(ctx, pid, reload))) : null));

  const versionItems = Array.isArray(versions && versions.versions) ? versions.versions : (Array.isArray(versions && versions.items) ? versions.items : []);
  const historyPanel = versionItems.length ? h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Version history')),
    h('div', { class: 'panel-body' }, h('ul', { class: 'open-items' }, versionItems.map((v) =>
      h('li', null, h('strong', null, `v${v.version}`), ' · ', time(v.created_at), ' by ', h('span', { class: 'mono' }, v.author || v.created_by || 'unknown'), ': ', v.reason))))) : null;

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Standards'), title: 'Review standards', lede: 'Explicit review standards, warn/block gate, and rule drafts.' }),
    damagedBanner,
    lintBanner,
    gatePanel,
    registerPanel,
    draftsPanel,
    historyPanel);
}
