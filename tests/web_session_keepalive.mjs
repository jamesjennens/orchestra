// The keep-alive API of the real web/js/api.js under Node (kittrial-5bb.330).
//
// argv: the api.js URL. There is no browser: the page's document is a stand-in that holds a
// visibility state and records the events the module dispatches, and the transport answers
// the session read without a server. What is checked is the rule the wizard relies on: a
// visible page touches the session, a hidden one polls nothing, a sign-out or an expired
// session ends the loop, and a warning reaches the page before the idle deadline.
const [apiUrl] = process.argv.slice(2);
const { createApi, SESSION_IDLE_WARNING_EVENT } = await import(apiUrl);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fakeDocument() {
  const listeners = new Map();
  const events = [];
  const handle = (type) => listeners.get(type) || new Set();
  const doc = {
    visibilityState: 'visible',
    events,
    addEventListener(type, handler) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(handler); },
    removeEventListener(type, handler) { handle(type).delete(handler); },
    dispatchEvent(event) {
      events.push(event);
      for (const handler of [...handle(event.type)]) handler(event);
      return true;
    },
    async visibility(state) {
      doc.visibilityState = state;
      for (const handler of [...handle('visibilitychange')]) await handler({ type: 'visibilitychange' });
    },
    listenerCount: (type) => handle(type || 'visibilitychange').size,
  };
  return doc;
}

globalThis.document = fakeDocument();
globalThis.CustomEvent = class CustomEvent {
  constructor(type, init) { this.type = type; this.detail = init && init.detail; }
};

// The answer GET /v1/sessions/current gives a browser session: the configured window and the
// session's own deadlines (the server side is tested in Python; here only the page's rule is).
function sessionAnswer(idleSeconds, absoluteSeconds) {
  const now = Date.now();
  return {
    user: { id: 'usr_0123456789abcdef', username: 'alex' },
    via: 'session',
    idle_seconds: idleSeconds,
    idle_expires_at: new Date(now + idleSeconds * 1000).toISOString(),
    absolute_seconds: absoluteSeconds,
    absolute_expires_at: new Date(now + absoluteSeconds * 1000).toISOString(),
  };
}

function counting(answer) {
  const calls = [];
  const transport = async (method, path) => {
    calls.push(method + ' ' + path);
    return answer(method, path);
  };
  return { calls, transport };
}

const SESSION = 'GET /v1/sessions/current';
const results = {};

// 1. keepAlive is one light authenticated touch, repeatable, and answers the window.
{
  const counted = counting(() => ({ status: 200, data: sessionAnswer(300, 43200) }));
  const api = createApi(counted.transport);
  const first = await api.keepAlive();
  results.keepAliveIsOneSessionRead = counted.calls.length === 1 && counted.calls[0] === SESSION;
  results.keepAliveAnswersTheWindow = first.idle_seconds === 300 && first.absolute_seconds === 43200
    && typeof first.idle_expires_at === 'string';
  await api.keepAlive();
  results.keepAliveTouchesAgain = counted.calls.length === 2;
}

// 2. A visible page touches; a hidden one polls nothing; the next sight touches at once;
//    stop() ends the loop.
{
  const counted = counting(() => ({ status: 200, data: sessionAnswer(30, 43200) }));
  const api = createApi(counted.transport);
  const loop = api.keepAliveWhileVisible({ intervalMs: 25 });
  await sleep(300);
  const whileVisible = counted.calls.length;
  results.touchesWhileVisible = whileVisible >= 3;
  await globalThis.document.visibility('hidden');
  await sleep(300);
  results.hiddenTabDoesNotPoll = counted.calls.length === whileVisible;
  await globalThis.document.visibility('visible');
  await sleep(100);
  results.touchesAgainWhenVisible = counted.calls.length > whileVisible;
  loop.stop();
  const atStop = counted.calls.length;
  await sleep(150);
  results.stopEndsTheLoop = counted.calls.length === atStop;
  results.stopRemovesTheListener = globalThis.document.listenerCount() === 0;
}

// 3. A page opened while the tab is hidden touches nothing until it is shown.
{
  globalThis.document.visibilityState = 'hidden';
  const counted = counting(() => ({ status: 200, data: sessionAnswer(30, 43200) }));
  const api = createApi(counted.transport);
  const loop = api.keepAliveWhileVisible({ intervalMs: 25 });
  await sleep(120);
  results.noTouchWhileHidden = counted.calls.length === 0;
  await globalThis.document.visibility('visible');
  await sleep(80);
  results.firstTouchWhenShown = counted.calls.length >= 1;
  loop.stop();
}

// 4. The warning, in the case it exists for: the touches stop reaching the service, so the
//    deadline this loop last heard about comes near. A few minutes (here, seconds) before it,
//    the page hears it — a CustomEvent on the document and the caller's own handler.
{
  const warnings = [];
  const seen = [];
  let failing = false;
  const counted = counting(() => (failing
    ? { status: 503, data: { error: { code: 'busy', message: 'not completed; send it again in a moment' } } }
    : { status: 200, data: sessionAnswer(2, 43200) }));
  const api = createApi(counted.transport);
  const listener = (event) => seen.push(event);
  globalThis.document.addEventListener(SESSION_IDLE_WARNING_EVENT, listener);
  const loop = api.keepAliveWhileVisible({ intervalMs: 25, warnMs: 1500,
    onWarning: (detail) => warnings.push(detail) });
  await sleep(40);
  const touched = counted.calls.filter((call) => call === SESSION).length;
  failing = true;
  await sleep(1400);
  const retried = counted.calls.filter((call) => call === SESSION).length;
  loop.stop();
  globalThis.document.removeEventListener(SESSION_IDLE_WARNING_EVENT, listener);
  const dispatched = globalThis.document.events.filter((event) => event.type === SESSION_IDLE_WARNING_EVENT);
  results.failedTouchIsRetried = touched >= 1 && retried > touched;
  results.warnsBeforeTheDeadline = warnings.length >= 1 && dispatched.length >= 1;
  results.warningCarriesTheWindow = warnings.length >= 1 && warnings[0].idleSeconds === 2
    && typeof warnings[0].idleExpiresAt === 'string'
    && warnings[0].remainingSeconds <= 2;
  results.warningIsTheDocumentEvent = seen.length === dispatched.length
    && seen[0].type === SESSION_IDLE_WARNING_EVENT && seen[0].detail.idleSeconds === 2;
}

// 5. Signing out stops the loop: nothing touches a session that is being deleted.
{
  const counted = counting((method) => method === 'DELETE'
    ? { status: 204, data: null } : { status: 200, data: sessionAnswer(30, 43200) });
  const api = createApi(counted.transport);
  api.keepAliveWhileVisible({ intervalMs: 25 });
  await sleep(120);
  const touchesBefore = counted.calls.filter((call) => call === SESSION).length;
  await api.logout();
  const atSignOut = counted.calls.length;
  await sleep(200);
  results.signOutStopsTheLoop = counted.calls.length === atSignOut;
  results.touchedBeforeSignOut = touchesBefore >= 2;
  results.signOutDeletedTheSession = counted.calls.includes('DELETE /v1/sessions/current');
}

// 6. An expired session (401) ends the loop instead of polling a session that is gone.
{
  let status = 200;
  const counted = counting(() => (status === 200
    ? { status: 200, data: sessionAnswer(30, 43200) }
    : { status: 401, data: { error: { code: 'unauthenticated', message: 'Session expired or revoked' } } }));
  const api = createApi(counted.transport);
  api.keepAliveWhileVisible({ intervalMs: 25 });
  await sleep(100);
  results.touchedBeforeExpiry = counted.calls.filter((call) => call === SESSION).length >= 2;
  status = 401;
  await sleep(120);
  const after401 = counted.calls.length;
  await sleep(200);
  results.expiredSessionStopsTheLoop = counted.calls.length === after401;
}

console.log(JSON.stringify(results));
