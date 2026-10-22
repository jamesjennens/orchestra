// Release facts over HTTP (kittrial-5bb.378, slice 2): the read-only pre-flight and ONE
// release group. The browser cannot decide Git membership, so this page resolves each
// named task against its own recorded lifecycle scope and sends the resolved targets.
// The server re-verifies them and reports a stale target as skipped, never guessed.
import { h, shortSha, confirmDialog } from '../dom.js';
import { pageHead, field, formValues, setFieldError, act, empty, errorState, describe } from '../ui.js';

const OWNER = ['owner', 'superuser'];
const COMMIT = /^[0-9a-f]{40}$/;

function crumbs(ctx, project) {
  return [{ label: 'Projects', href: ctx.href('/projects') },
          { label: project.name, href: ctx.href('/p/' + project.id) },
          { label: 'Releases' }];
}

function table(headers, rows) {
  return h('div', { class: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null, headers.map((label) => h('th', { scope: 'col' }, label)))),
    h('tbody', null, rows)));
}

export async function page(ctx, { pid }) {
  const project = await ctx.api.project(pid);
  const owner = OWNER.includes(project.role);
  const queryHost = h('div', { class: 'panel-body' });
  const resultHost = h('div');

  // -- the read-only pre-flight ------------------------------------------------
  const env = field({ id: 'release-env', label: 'Environment', required: true,
                      placeholder: 'e.g. production', hint: 'The environment whose live releases and reverted integrations you want to see.' });
  const check = h('button', { type: 'submit', class: 'primary' }, 'Check');
  const checkForm = h('form', { class: 'panel-body stack', novalidate: true }, env, h('div', null, check));
  checkForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = formValues(checkForm);
    const environment = (values['release-env'] || '').trim();
    if (!environment) return setFieldError(checkForm, 'release-env', 'Enter an environment.');
    setFieldError(checkForm, 'release-env', '');
    let data;
    try {
      data = await act(check, () => ctx.api.releaseQuery(pid, {
        source_commit: '', integration_commit: '', release_id: '', environment }));
    } catch { return; }
    queryHost.replaceChildren(
      h('div', null, h('h3', { class: 'small' }, 'Reverted integrations ', h('span', { class: 'nav-count' }, data.reverted.length)),
        data.reverted.length
          ? table(['Task', 'Integration commit'], data.reverted.map((row) => h('tr', { 'data-reverted': row.task },
              h('td', { class: 'mono small' }, row.task), h('td', { class: 'mono small' }, shortSha(row.integration_commit)))))
          : h('p', { class: 'muted' }, 'No integration is reverted by an operator record in this project.')),
      h('div', null, h('h3', { class: 'small' }, 'Recorded live releases in ' + data.environment,
          h('span', { class: 'nav-count' }, data.live_releases.length)),
        data.live_releases.length
          ? table(['Task', 'Release', 'Integration commit', 'Liveness'], data.live_releases.map((row) => h('tr', { 'data-live': row.task },
              h('td', { class: 'mono small' }, row.task), h('td', null, row.release_id || '—'),
              h('td', { class: 'mono small' }, shortSha(row.integration_commit)), h('td', null, row.liveness))))
          : h('p', { class: 'muted' }, 'No task is recorded live in this environment.')));
  });
  queryHost.replaceChildren(h('p', { class: 'muted small' }, 'Nothing is sent until you press Check. The answer writes nothing.'));

  // -- one group ---------------------------------------------------------------
  const form = h('form', { class: 'panel-body stack', novalidate: true },
    field({ id: 'deploy-env', label: 'Environment', required: true, placeholder: 'e.g. production' }),
    field({ id: 'deploy-release', label: 'Release id', required: true, placeholder: 'e.g. orchestra-1022c',
            hint: 'The name people use for this release.' }),
    field({ id: 'deploy-commit', label: 'Release commit', required: true, placeholder: '40 hexadecimal characters',
            hint: 'The full commit this release was cut at; targets are checked against their own integrations.' }),
    field({ id: 'deploy-evidence', label: 'Evidence', required: true, placeholder: 'e.g. build URL or CI run',
            hint: 'One or more pointers; one per line.' }),
    field({ id: 'deploy-targets', label: 'Targets', type: 'textarea', rows: 5,
            hint: 'One task id per line. Each is resolved against its recorded integration; a task without a passing integration at its current scope is listed as skipped.' }),
    field({ id: 'deploy-supersede', label: 'Supersede (optional)', type: 'textarea', rows: 3,
            hint: 'Tasks this release makes no longer live, one id per line. Leave empty for a plain deploy.' }),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', id: 'deploy-verified' }), ' The deploy was live-verified'),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', id: 'deploy-rollback' }), ' This is an explicit rollback'),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Record this release')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = formValues(form);
    const environment = (values['deploy-env'] || '').trim();
    const release = (values['deploy-release'] || '').trim();
    const commit = (values['deploy-commit'] || '').trim();
    const evidence = (values['deploy-evidence'] || '').split('\n').map((line) => line.trim()).filter(Boolean);
    const ids = (values['deploy-targets'] || '').split(/[\s,]+/).map((id) => id.trim()).filter(Boolean);
    const supersede = (values['deploy-supersede'] || '').split(/[\s,]+/).map((id) => id.trim()).filter(Boolean);
    const rollback = form.querySelector('#deploy-rollback').checked;
    const verified = form.querySelector('#deploy-verified').checked;
    for (const [id, message] of [['deploy-env', environment ? '' : 'Enter an environment.'],
                                 ['deploy-release', release ? '' : 'Enter a release id.'],
                                 ['deploy-commit', COMMIT.test(commit) ? '' : 'Enter the full 40-character lowercase commit.'],
                                 ['deploy-evidence', evidence.length ? '' : 'Add at least one evidence pointer.']]) {
      setFieldError(form, id, message);
      if (message) return;
    }
    if (!ids.length && !supersede.length) return setFieldError(form, 'deploy-targets', 'Name at least one task.');
    if (ids.length > 200) return setFieldError(form, 'deploy-targets', 'One request carries at most 200 targets; split the release into groups.');
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    // Resolve each target against its recorded lifecycle, as a coordinator's own checkout
    // would. A task without a passing integration at its current scope is reported locally
    // (the server would report it as skipped too; this saves the request).
    const targets = [];
    const local = [];
    try {
      for (const task of ids) {
        let life;
        try { life = await ctx.api.lifecycle(pid, task); } catch (error) {
          local.push({ task, reason: describe(error) }); continue;
        }
        const scope = life.scope || {};
        const integrated = (life.facts || {}).integrated || {};
        if (integrated.value !== 'passed' || !COMMIT.test(scope.source_commit || '')
            || !COMMIT.test(scope.integration_commit || '')) {
          local.push({ task, reason: 'no passing integration at its current scope' });
          continue;
        }
        targets.push({ task, source_commit: scope.source_commit, integration_commit: scope.integration_commit });
      }
      if (!targets.length && !supersede.length) {
        throw new Error('None of the named tasks has a passing integration at its current scope.');
      }
      const proceed = await confirmDialog({
        title: 'Record this release?',
        body: `${release} in ${environment}: ${targets.length} target(s)` +
              (rollback ? ', as an explicit rollback' : '') +
              (local.length ? `, ${local.length} left out as skipped` : '') + '.',
        confirmLabel: 'Record release',
      });
      if (!proceed) return;
      const body = { scope: { source_commit: '', integration_commit: commit, release_id: release, environment },
                     evidence, provenance: 'performed', live_verified: verified, targets };
      if (rollback) body.rollback = true;
      if (supersede.length) body.supersede = supersede;
      const data = await act(button, () => ctx.api.deployRelease(pid, body), { success: 'Release facts recorded' });
      const skipped = local.concat(data.skipped || []);
      resultHost.replaceChildren(h('section', { class: 'panel', 'data-panel': 'release-result' },
        h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'This group'),
          h('span', { class: 'small muted' }, `Group ${data.groups_completed} of ${data.groups_total}`)),
        h('div', { class: 'panel-body stack' },
          data.targets && data.targets.length
            ? table(['Task', 'Deployed', 'Live', 'Verified', 'Note'], data.targets.map((row) => h('tr', { 'data-released': row.task },
                h('td', { class: 'mono small' }, row.task),
                h('td', null, row.deployed ? 'yes' : 'no'),
                h('td', null, row.live || '—'),
                h('td', null, row.live_verified ? 'yes' : 'no'),
                h('td', { class: 'small muted' }, row.flag ? (row.flag_reason || row.flag) : '—'))))
            : h('p', { class: 'muted' }, 'No target was written in this group.'),
          h('div', null, h('h3', { class: 'small' }, 'Skipped ', h('span', { class: 'nav-count' }, skipped.length)),
            skipped.length
              ? table(['Task', 'Reason'], skipped.map((row) => h('tr', { 'data-skipped': row.task },
                  h('td', { class: 'mono small' }, row.task), h('td', { class: 'small' }, row.reason))))
              : h('p', { class: 'muted' }, 'Nothing was skipped.')),
          h('p', { class: 'small muted' }, 'This request wrote one group. A release that spans more than one group is sent as separate requests, each with a fresh read between them.'))));
      try { resultHost.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch { /* the shim has no layout */ }
    } catch (error) {
      if (error && error.status === 403) setFieldError(form, 'deploy-targets', 'You may not record releases in this project.');
      else if (error) setFieldError(form, 'deploy-targets', describe(error));
    } finally { button.disabled = false; }
  });

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project), title: 'Releases',
      lede: 'What is live in an environment, and the release facts this project records. A release records evidence; it does not deploy code.' }),
    project.archived ? h('div', { class: 'banner' }, 'This project is archived. Release facts can still be read; nothing new can be recorded.') : null,
    h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Pre-flight'),
        h('span', { class: 'small muted' }, 'Read the reverted integrations and the recorded live releases before selecting targets')),
      checkForm, queryHost),
    h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Record one release group'),
        h('span', { class: 'small muted' }, owner ? 'Only an owner or superuser may record a release' : 'Owners and superusers record releases; you can read the pre-flight')),
      owner && !project.archived ? form : h('div', { class: 'panel-body' },
        empty('Recording a release needs the project’s release authority',
              'An owner or superuser holds it, and an owner may grant it to one agent. A coordinator can read the pre-flight above.'))),
    resultHost);
}
