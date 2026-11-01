'use strict';

// Exercise the shipped privileged bridge through its JSON subprocess boundary.
// No Zotero library, manuscript, network endpoint, or installed package is used.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../extension/bridge.js'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function attachment(id, key = `ATT${id}`) {
  return {
    id, key, libraryID: 1, parentKey: 'PARENT', deleted: false,
    isFileAttachment: () => true,
    isPDFAttachment: () => true,
    getFilePathAsync: async () => `C:\\synthetic\\${key}.pdf`,
    getFilePath: () => `C:\\synthetic\\${key}.pdf`,
    loadDataType: async () => {},
    getAnnotations: () => [],
  };
}
function job(item, run = `run-${item.id}`, status = 'validated') {
  return {
    run_id: run, status,
    attachment: { library_id: item.libraryID, key: item.key },
    annotations_original: [], annotations_target: [],
  };
}
function harness(handler, items = []) {
  const requests = [], logs = [], readerOpens = [], refreshed = [];
  const win = { document: { getElementById: () => null } };
  const readers = [];
  let readerOpen = async () => {};
  const subprocess = {
    async call() {
      let input = '', response, read = false;
      return {
        stdin: {
          async write(value) { input += value; },
          async close() {
            const request = JSON.parse(input);
            requests.push(request);
            response = Promise.resolve().then(() => handler(request)).then(
              data => ({ event: 'result', data }),
              error => ({ event: 'error', code: error.message }),
            );
          },
        },
        stdout: { async readString() {
          if (read) return '';
          read = true;
          return JSON.stringify(await response) + '\n';
        } },
        stderr: { readString: async () => '' },
        wait: async () => ({ exitCode: (await response).event === 'error' ? 1 : 0 }),
        kill() { throw Error('Unexpected oversized response'); },
      };
    },
  };
  const Zotero = {
    Prefs: { get: () => 'synthetic-deployment.json' },
    File: { getContentsAsync: async () => JSON.stringify({ python: 'python.exe', project: 'C:\\synthetic', home: 'C:\\synthetic\\jobs' }) },
    Libraries: { get: () => ({ editable: true, filesEditable: true }) },
    Items: { getByLibraryAndKey: (library, key) => items.find(item => item.libraryID === library && item.key === key) },
    DB: { executeTransaction: async fn => fn() },
    Reader: { _readers: readers, async open(id) { readerOpens.push(id); return readerOpen(id); } },
    Sync: { Storage: { Local: { async checkForUpdatedFiles(library, ids) { refreshed.push(...ids); } } } },
    Notifier: { trigger: async () => {} },
    Fulltext: { indexItems: async () => {} },
    logError: error => logs.push(String(error)), debug: () => {},
    getMainWindow: () => win,
  };
  const context = vm.createContext({
    Zotero, rootURI: 'file:///synthetic/',
    Cc: { '@mozilla.org/process/environment;1': { getService: () => ({ get: () => 'C:\\synthetic' }) } },
    Ci: { nsIEnvironment: {} },
    ChromeUtils: { importESModule(url) {
      assert.match(url, /Subprocess\.sys\.mjs$/);
      return { Subprocess: subprocess };
    } },
  });
  vm.runInContext(source, context, { filename: 'extension/bridge.js' });
  return {
    api: context.PaperFormatter, requests, logs, readers, readerOpens, refreshed, win,
    failReaderOpen(error) { readerOpen = async () => { throw error; }; },
  };
}

test('reader reopen failure leaves a successfully committed file committed', async () => {
  const item = attachment(1), original = job(item);
  let fileVersion = 'original', readerClosed = false;
  const h = harness(async request => {
    if (request.command === 'begin_commit') {
      fileVersion = 'candidate';
      return { ...original, status: 'awaiting_zotero' };
    }
    if (request.command === 'finish_commit') return { ...original, status: 'committed' };
    if (request.command === 'begin_rollback') fileVersion = 'original';
    throw Error(`Unexpected command: ${request.command}`);
  }, [item]);
  h.readers.push({ itemID: item.id, close() { readerClosed = true; } });
  h.failReaderOpen(Error('Reader unavailable'));
  const result = await h.api.commit(item, original);
  assert.equal(result.status, 'committed');
  assert.match(result.reader_warning, /Reader unavailable/);
  assert.equal(fileVersion, 'candidate');
  assert.equal(readerClosed, true);
  assert.deepEqual(h.readerOpens, [item.id]);
  assert.deepEqual(h.requests.map(r => r.command), ['begin_commit', 'finish_commit']);
  assert.equal(h.api.running.has(item.id), false);
});

test('a failed recovery and damaged history record do not block later valid jobs', async () => {
  const first = attachment(1), second = attachment(2);
  const bad = job(first, 'bad', 'replacing'), good = job(second, 'good', 'awaiting_zotero');
  const h = harness(async request => {
    if (request.command === 'pending') return { jobs: [bad, good], errors: [{ run_id: 'damaged', detail: 'Unreadable manifest' }] };
    if (request.command === 'begin_rollback') throw Error('SOURCE_CHANGED');
    if (request.command === 'finish_commit') return { ...good, status: 'committed' };
    throw Error(`Unexpected command: ${request.command}`);
  }, [first, second]);
  const result = plain(await h.api.recoverPending());
  assert.deepEqual(result.recovered, [{ run_id: 'good', status: 'committed' }]);
  assert.equal(result.errors.length, 2);
  assert.equal(result.errors[0].run_id, 'damaged');
  assert.equal(result.errors[1].run_id, 'bad');
  assert.match(result.errors[1].detail, /SOURCE_CHANGED/);
  assert.match(h.api.recoveryError, /damaged/);
  assert.match(h.api.recoveryError, /bad/);
  assert.deepEqual(h.refreshed, [second.id]);
  assert.equal(h.api.running.size, 0);
});

test('overlapping recovery requests share one pending scan and one commit', async () => {
  const item = attachment(1), pending = job(item, 'recover', 'awaiting_zotero');
  const started = deferred(), release = deferred();
  const h = harness(async request => {
    if (request.command === 'pending') { started.resolve(); await release.promise; return { jobs: [pending] }; }
    if (request.command === 'finish_commit') return { ...pending, status: 'committed' };
    throw Error(`Unexpected command: ${request.command}`);
  }, [item]);
  const a = h.api.recoverPending();
  await started.promise;
  const b = h.api.recoverPending();
  release.resolve();
  const [first, second] = await Promise.all([a, b]);
  assert.deepEqual(plain(first), plain(second));
  assert.deepEqual(h.requests.map(r => r.command), ['pending', 'finish_commit']);
  assert.equal(first.recovered.length, 1);
});

test('commit cannot race a recovery that owns the same attachment', async () => {
  const item = attachment(1), pending = job(item, 'recover', 'awaiting_zotero');
  const started = deferred(), release = deferred();
  const h = harness(async request => {
    if (request.command === 'pending') return { jobs: [pending] };
    if (request.command === 'finish_commit') { started.resolve(); await release.promise; return { ...pending, status: 'committed' }; }
    throw Error(`Unexpected command: ${request.command}`);
  }, [item]);
  const recovery = h.api.recoverPending();
  await started.promise;
  await assert.rejects(h.api.commit(item, job(item, 'new-job')), /此附件已有操作正在运行/);
  assert.equal(h.api.running.has(item.id), true);
  release.resolve();
  await recovery;
  assert.equal(h.api.running.has(item.id), false);
  assert.equal(h.requests.some(r => r.command === 'begin_commit'), false);
});

test('recovery skips an attachment owned by a live commit without releasing its guard', async () => {
  const item = attachment(1), other = attachment(2);
  const current = job(item), pending = job(other, 'other', 'awaiting_zotero');
  const started = deferred(), release = deferred();
  const h = harness(async request => {
    if (request.command === 'begin_commit') { started.resolve(); await release.promise; return { ...current, status: 'awaiting_zotero' }; }
    if (request.command === 'pending') return { jobs: [{ ...current, status: 'replacing' }, pending] };
    if (request.command === 'finish_commit') return { ...(request.run_id === current.run_id ? current : pending), status: 'committed' };
    throw Error(`Unexpected command: ${request.command}`);
  }, [item, other]);
  const committing = h.api.commit(item, current);
  await started.promise;
  const result = plain(await h.api.recoverPending());
  assert.deepEqual(result.recovered, [{ run_id: 'other', status: 'committed' }]);
  assert.equal(result.errors.length, 1);
  assert.match(result.errors[0].detail, /此附件正在处理中/);
  assert.equal(h.api.running.has(item.id), true);
  await assert.rejects(h.api.commit(item, current), /此附件已有操作正在运行/);
  release.resolve();
  await committing;
  assert.equal(h.api.running.size, 0);
});

test('backend pending failure is visible and a later retry clears the stale error', async () => {
  let fail = true;
  const h = harness(async request => {
    assert.equal(request.command, 'pending');
    if (fail) throw Error('BACKEND_UNAVAILABLE');
    return { jobs: [] };
  });
  await assert.rejects(h.api.recoverPending(), /BACKEND_UNAVAILABLE/);
  assert.match(h.api.recoveryError, /无法读取未完成的提交.*BACKEND_UNAVAILABLE/);
  fail = false;
  const result = plain(await h.api.recoverPending());
  assert.deepEqual(result, { recovered: [], errors: [], warnings: [] });
  assert.equal(h.api.recoveryError, '');
  assert.equal(h.requests.length, 2);
});

test('duplicate asynchronous openUI calls create only one dialog', async () => {
  const h = harness(() => { throw Error('No backend call expected'); });
  const opened = deferred(), release = deferred();
  let opens = 0;
  h.api.UI = { async open(win, dialogAPI) { opens++; opened.resolve(); await release.promise; return () => dialogAPI.closed(win); } };
  const a = h.api.openUI(attachment(1));
  await opened.promise;
  const b = h.api.openUI(attachment(2));
  release.resolve();
  await Promise.all([a, b]);
  assert.equal(opens, 1);
  await h.api.openUI(attachment(3));
  assert.equal(opens, 1);
  h.api.closeUI(h.win);
  await h.api.openUI(attachment(4));
  assert.equal(opens, 2);
});

test('a dialog refusing close while busy stays registered until it actually closes', async () => {
  const h = harness(() => { throw Error('No backend call expected'); });
  let busy = true, opens = 0, closes = 0;
  h.api.UI = { async open(win, dialogAPI) {
    opens++;
    return () => { if (busy) return; closes++; dialogAPI.closed(win); };
  } };
  await h.api.openUI(attachment(1));
  h.api.closeUI(h.win);
  await h.api.openUI(attachment(2));
  assert.equal(opens, 1);
  assert.equal(closes, 0);
  busy = false;
  h.api.closeUI(h.win);
  assert.equal(closes, 1);
  await h.api.openUI(attachment(3));
  assert.equal(opens, 2);
});

test('a failed asynchronous dialog creation can be retried', async () => {
  const h = harness(() => { throw Error('No backend call expected'); });
  let opens = 0;
  h.api.UI = { async open(win, dialogAPI) {
    if (++opens === 1) throw Error('Dialog initialization failed');
    return () => dialogAPI.closed(win);
  } };
  await assert.rejects(h.api.openUI(attachment(1)), /Dialog initialization failed/);
  await h.api.openUI(attachment(2));
  assert.equal(opens, 2);
});

test('close during dialog initialization waits for its close callback and prevents duplicate opening', async () => {
  const h = harness(() => { throw Error('No backend call expected'); });
  const opened = deferred(), release = deferred();
  let opens = 0, closes = 0;
  h.api.UI = { async open(win, dialogAPI) {
    opens++; opened.resolve(); await release.promise;
    return () => { closes++; dialogAPI.closed(win); };
  } };
  const first = h.api.openUI(attachment(1));
  await opened.promise;
  h.api.closeUI(h.win);
  const duplicate = h.api.openUI(attachment(2));
  assert.equal(opens, 1);
  assert.equal(closes, 0);
  release.resolve();
  await Promise.all([first, duplicate]);
  assert.equal(opens, 1);
  assert.equal(closes, 1);
  await h.api.openUI(attachment(3));
  assert.equal(opens, 2);
});

test('an old dialog completing after an early close cannot unregister the replacement', async () => {
  const h = harness(() => { throw Error('No backend call expected'); });
  const opened = deferred(), release = deferred();
  let opens = 0, oldAPI, oldCloses = 0;
  h.api.UI = { async open(win, dialogAPI) {
    if (++opens === 1) {
      oldAPI = dialogAPI; opened.resolve(); await release.promise;
      return () => { oldCloses++; dialogAPI.closed(win); };
    }
    return () => dialogAPI.closed(win);
  } };
  const old = h.api.openUI(attachment(1));
  await opened.promise;
  oldAPI.closed(h.win);
  await h.api.openUI(attachment(2));
  release.resolve();
  await old;
  await h.api.openUI(attachment(3));
  assert.equal(oldCloses, 1);
  assert.equal(opens, 2);
});

test('a late initialization failure from an old dialog cannot remove the replacement DOM', async () => {
  const h = harness(() => { throw Error('No backend call expected'); });
  const opened = deferred(), release = deferred();
  let opens = 0, oldAPI, removed = 0;
  h.win.document.getElementById = () => ({ remove() { removed++; } });
  h.api.UI = { async open(win, dialogAPI) {
    if (++opens === 1) {
      oldAPI = dialogAPI; opened.resolve(); await release.promise;
      throw Error('Late failure');
    }
    return () => dialogAPI.closed(win);
  } };
  const old = h.api.openUI(attachment(1));
  const rejected = assert.rejects(old, /Late failure/);
  await opened.promise;
  oldAPI.closed(h.win);
  await h.api.openUI(attachment(2));
  release.resolve();
  await rejected;
  await h.api.openUI(attachment(3));
  assert.equal(removed, 0);
  assert.equal(opens, 2);
});
