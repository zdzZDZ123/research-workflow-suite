var formatter, menuIDs = [], rootURL;
function install() {}
function uninstall() {}
function onMainWindowLoad({window}) {
  window.MozXULElement.insertFTLIfNeeded('paper-formatter.ftl');
}
function onMainWindowUnload({window}) {
  formatter?.closeUI(window);
  window.document.querySelector('[href="paper-formatter.ftl"]')?.remove();
}
async function startup({id,rootURI}) {
  await Zotero.initializationPromise;
  rootURL=rootURI;
  const scope={Zotero,Services,Cc,Ci,ChromeUtils,rootURI};
  Services.scriptloader.loadSubScript(rootURI+'bridge.js',scope);
  Services.scriptloader.loadSubScript(rootURI+'ui.js',scope);
  formatter=scope.PaperFormatter;
  formatter.UI=scope.PaperFormatterUI;
  Zotero.PaperFormatter=formatter;
  for (const window of Zotero.getMainWindows()) onMainWindowLoad({window});
  menuIDs.push(Zotero.MenuManager.registerMenu({menuID:'paper-formatter-convert',pluginID:id,target:'main/library/item',menus:[{
    menuType:'menuitem',l10nID:'paper-formatter-convert',
    onShowing(event,context){context.setVisible(context.items?.length===1 && (context.items[0].isRegularItem()||context.items[0].isAttachment()));},
    onCommand(event,context){formatter.openUI(context.items?.[0]).catch(Zotero.logError);}
  }]}));
  menuIDs.push(Zotero.MenuManager.registerMenu({menuID:'paper-formatter-settings',pluginID:id,target:'main/menubar/tools',menus:[{
    menuType:'menuitem',l10nID:'paper-formatter-settings',onCommand(){formatter.openUI(null).catch(Zotero.logError);}
  }]}));
  // Recovery is local and never contacts an external service or starts a new conversion.
  formatter.recoverPending().catch(error=>{Zotero.logError(error);formatter.recoveryError=String(error);});
}
function shutdown() {
  for (const window of Zotero.getMainWindows()) onMainWindowUnload({window});
  for (const id of menuIDs) if(id) Zotero.MenuManager.unregisterMenu(id);
  menuIDs=[];
  delete Zotero.PaperFormatter;
}
