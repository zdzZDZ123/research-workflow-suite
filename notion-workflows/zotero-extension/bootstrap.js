/* Only the official menu API and a local child process; no scheduled/background sync. */
var menuID;
var relationMenuID;
var deployment;
var running = false;
var runningRelations = false;
var addonID;
var selectionUI;

function install() {}
function uninstall() {}

function onMainWindowLoad({ window }) {
  window.MozXULElement.insertFTLIfNeeded("paper-deep-reading.ftl");
  selectionUI?.attach(window);
}

function onMainWindowUnload({ window }) {
  selectionUI?.detach(window);
  window.document.querySelector('[href="paper-deep-reading.ftl"]')?.remove();
}

async function startup({ id, rootURI }) {
  addonID = id;
  await Zotero.initializationPromise;
  if (typeof Zotero.MenuManager?.registerMenu !== "function") {
    throw new Error("Paper Deep Reading requires the Zotero 8+ menu API");
  }
  const configurationScope = {};
  Services.scriptloader.loadSubScript(rootURI + "deployment.js", configurationScope);
  deployment = configurationScope.DEPLOYMENT;
  const selectionScope = { Zotero, Services, deployment };
  Services.scriptloader.loadSubScript(rootURI + "selection.js", selectionScope);
  selectionUI = selectionScope.PaperSelection;
  for (const window of Zotero.getMainWindows()) onMainWindowLoad({ window });
  menuID = Zotero.MenuManager.registerMenu({
    menuID: "paper-deep-reading-sync",
    pluginID: id,
    target: "main/library/item",
    menus: [{
      menuType: "menuitem",
      l10nID: "paper-deep-reading-sync",
      onShowing(event, context) {
        context.setVisible(context.items?.length === 1 && context.items[0].isRegularItem());
        context.setEnabled(!running);
      },
      onCommand(event, context) {
        if (context.items?.length === 1) void synchronize(context.items[0]);
      }
    }]
  });
  if (!menuID) throw new Error("Menu registration failed");
  relationMenuID = Zotero.MenuManager.registerMenu({
    menuID: "paper-relations-discover",
    pluginID: id,
    target: "main/menubar/tools",
    menus: [{
      menuType: "menuitem",
      l10nID: "paper-relations-discover",
      onShowing(event, context) { context.setEnabled(!runningRelations); },
      onCommand() { void discoverRelations(); }
    }]
  });
  if (!relationMenuID) throw new Error("Relationship menu registration failed");
}

async function discoverRelations() {
  if (runningRelations) return;
  runningRelations = true;
  const window = Zotero.getMainWindow();
  const progress = new Zotero.ProgressWindow({ closeOnClick: true });
  progress.changeHeadline("发现 Notion 论文关系");
  progress.addDescription("正在检查论文全文和关系库…");
  progress.show();
  try {
    const { Subprocess } = window.ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    const process = await Subprocess.call({
      command: deployment.python,
      arguments: [deployment.relationRunner, "run"],
      workdir: deployment.project,
      stderr: "stdout"
    });
    let output = "";
    while (true) {
      const chunk = await process.stdout.readString();
      if (!chunk) break;
      output += chunk;
      if (output.length > 100000) throw new Error("关系发现程序输出异常；请检查本地日志。");
    }
    await process.wait();
    // PDF extraction can emit diagnostic lines on stderr, which Zotero merges
    // into stdout here. The Python command writes its JSON result last.
    const lines = output.trim().split(/\r?\n/);
    const result = JSON.parse(lines[lines.length - 1]);
    if (!["NO_PAIR", "SYNCED", "UNCHANGED"].includes(result.status)) {
      throw new Error(result.error || result.message || result.status);
    }
    progress.changeHeadline(result.status === "NO_PAIR" ? "当前没有可比较的论文对" : "论文关系发现完成");
    progress.addDescription(`合格 ${result.eligible} 篇；可读全文 ${result.readable} 篇；跳过 ${result.skipped} 条；自动发布 ${result.auto_published} 条；待审核 ${result.pending} 条；剩余 ${result.remaining_pairs} 对。`);
    if (result.url && /^https:\/\/www\.notion\.so\/[a-f0-9]{32}$/.test(result.url)) {
      progress.addDescription(`<a href="${result.url}">打开 Notion 论文关系图谱</a>`);
    }
    progress.startCloseTimer(30000);
  } catch (error) {
    progress.changeHeadline("论文关系发现已暂停");
    const message = String(error.message || error);
    progress.addDescription(message.includes("MODEL_CONFIGURATION_REQUIRED")
      ? "尚未配置关系分析模型。请先在本机设置兼容 API 的地址、模型 ID、接口类型和密钥。"
      : message);
  } finally {
    runningRelations = false;
  }
}

async function synchronize(item) {
  if (running) return;
  running = true;
  const window = Zotero.getMainWindow();
  const progress = new Zotero.ProgressWindow({ closeOnClick: true });
  progress.changeHeadline("Paper Chat → Notion");
  progress.addDescription("正在读取已保存的精读子笔记并核验映射…");
  progress.show();
  try {
    const library = Zotero.Libraries.get(item.libraryID);
    const kind = library.libraryType === "group" ? "group" : "user";
    const scopeID = kind === "group" ? String(Zotero.Groups.getGroupIDFromLibraryID(item.libraryID)) : "0";
    const { Subprocess } = window.ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    // Argument arrays avoid shell quoting and injection. The subprocess does not
    // depend on Codex or on the lifecycle of a background Python server.
    const process = await Subprocess.call({
      command: deployment.python,
      arguments: [deployment.runner, "--item-key", item.key, "--library-type", kind, "--library-id", scopeID],
      workdir: deployment.project,
      stderr: "stdout"
    });
    let output = "";
    while (true) {
      const chunk = await process.stdout.readString();
      if (!chunk) break;
      output += chunk;
      if (output.length > 100000) throw new Error("同步程序输出异常；请检查本地日志。");
    }
    await process.wait();
    const result = JSON.parse(output.trim());
    if (!["SYNCED", "UNCHANGED"].includes(result.status)) {
      let message = result.message || result.error || result.status;
      if (result.status === "NOTE_SELECTION_REQUIRED") {
        const choices = result.notes.map((n, i) => `${i + 1}. ${n.title} [${n.key}]`).join("\n");
        const answer = window.prompt("存在多个候选，请输入要同步的编号：\n" + choices, "");
        const selected = Number(answer) - 1;
        if (answer !== null && Number.isInteger(selected) && selected >= 0 && selected < result.notes.length) {
          // Persist the explicit selection locally for subsequent retries.
          await runBinding(Subprocess, item.key, kind, scopeID, result.notes[selected].key);
          running = false;
          progress.close();
          return synchronize(item);
        }
        return;
      }
      throw new Error(message);
    }
    progress.changeHeadline(result.status === "UNCHANGED" ? "内容无变化，已跳过" : `精读同步成功 · v${result.version}`);
    const url = result.url || `https://www.notion.so/${result.page_id.replaceAll("-", "")}`;
    if (!/^https:\/\/www\.notion\.so\/[a-f0-9]{32}$/.test(url)) throw new Error("同步程序返回了无效页面地址");
    progress.addDescription(`<a href="${url}">打开 Notion 精读页面</a>`);
    progress.startCloseTimer(15000);
  } catch (error) {
    progress.changeHeadline("同步已暂停");
    progress.addDescription(String(error.message || error));
  } finally {
    running = false;
  }
}

async function runBinding(Subprocess, key, kind, library, note) {
  const process = await Subprocess.call({command: deployment.python,
    arguments: [deployment.runner, "--item-key", key, "--library-type", kind, "--library-id", library, "--bind-note", note],
    workdir: deployment.project, stderr: "stdout"});
  let output = "";
  while (true) { const part = await process.stdout.readString(); if (!part) break; output += part; }
  await process.wait();
  const result = JSON.parse(output.trim());
  if (result.status !== "BOUND") throw new Error(result.error || "绑定失败");
}

function shutdown() {
  if (menuID) Zotero.MenuManager.unregisterMenu(menuID);
  if (relationMenuID) Zotero.MenuManager.unregisterMenu(relationMenuID);
  for (const window of Zotero.getMainWindows()) onMainWindowUnload({ window });
  menuID = null;
  relationMenuID = null;
  selectionUI = null;
}
