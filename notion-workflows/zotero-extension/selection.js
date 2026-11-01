/* Paper Chat 3.7 selection adapter. No Paper Chat source patches or database access. */
var PaperSelection = (() => {
  const windows = new Map();
  const NS = 'http://www.w3.org/1999/xhtml';
  const label = '同步选中内容到 Notion';
  let busy = false;
  function element(doc, name, value) {
    const node = doc.createElementNS(NS, name);
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function context(win) {
    const tab = win.Zotero_Tabs;
    const items = win.ZoteroPane?.getSelectedItems?.() || [];
    return String(tab?.selectedID) + ':' + items.map(i => i.id).join(',');
  }
  function targetItem(win) {
    const reader = Zotero.Reader.getByTabID(win.Zotero_Tabs?.selectedID);
    if (reader) {
      const attachment = Zotero.Items.get(reader.itemID);
      if (attachment?.parentItemID) return Zotero.Items.get(attachment.parentItemID);
    }
    const items = win.ZoteroPane?.getSelectedItems?.() || [];
    return items.length === 1 && items[0].isRegularItem() ? items[0] : null;
  }
  function capture(win) {
    const doc = win.document, selection = doc.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed || !selection.toString().trim()) return null;
    const range = selection.getRangeAt(0);
    const parent = node => node.nodeType === 1 ? node : node.parentElement;
    const start = parent(range.startContainer), end = parent(range.endContainer);
    const message = start?.closest('.assistant-message[data-message-id]');
    const root = message?.closest('.chat-panel-root');
    const host = root?.closest('[data-displayed-session-id]');
    if (!root || !host?.dataset.displayedSessionId || end?.closest('.assistant-message[data-message-id]') !== message) return null;
    if (start.closest('button,input,textarea,[contenteditable="true"]') || end.closest('button,input,textarea,[contenteditable="true"]')) return null;
    const fragment = range.cloneContents();
    // Range cloning includes hidden/non-selectable tool-call UI. Remove only
    // nodes whose original DOM counterpart is proved to be non-visible or
    // non-selectable. A structural mismatch is left for the text-only fallback.
    const common = range.commonAncestorContainer;
    const originals = common.nodeType === 1
      ? Array.from(common.querySelectorAll('*')).filter(n => range.intersectsNode(n)) : [];
    const copies = Array.from(fragment.querySelectorAll('*'));
    if (originals.length === copies.length && originals.every((n,i) => n.localName === copies[i].localName)) {
      originals.forEach((n,i) => {
        const css = win.getComputedStyle(n);
        if (css.display === 'none' || css.visibility === 'hidden' || css.visibility === 'collapse'
            || css.userSelect === 'none' || n.hidden) copies[i].remove();
      });
    }
    // Preserve formatting inherited from the common ancestor without widening selection.
    let ancestor = parent(range.commonAncestorContainer);
    let content = fragment;
    while (ancestor && ancestor !== message && !ancestor.classList.contains('chat-bubble')) {
      if (/^(STRONG|B|EM|I|U|S|DEL|A|CODE|BLOCKQUOTE|LI|UL|OL|P|H[1-6])$/.test(ancestor.tagName.toUpperCase())) {
        const outer = element(doc, ancestor.localName);
        if (ancestor.localName === 'a') outer.setAttribute('href', ancestor.getAttribute('href') || '');
        outer.append(content); content = outer;
      }
      ancestor = ancestor.parentElement;
    }
    const safe = element(doc,'div'); safe.append(content);
    safe.querySelectorAll('button,input,textarea,script,style,iframe,object,embed,.message-action-btn').forEach(n => n.remove());
    safe.querySelectorAll('*').forEach(n => {
      for (const a of Array.from(n.attributes)) if (!['href','alt','src','encoding','class','colspan','rowspan'].includes(a.name)) n.removeAttribute(a.name);
    });
    return {text:selection.toString(),html:safe.innerHTML,message,root,host,
      messageID:message.getAttribute('data-message-id'),context:context(win),
      originalMessage:message.textContent,sessionID:host.dataset.displayedSessionId,
      suggestedItem:targetItem(win),capturedAt:new Date().toISOString()};
  }
  function valid(win, snapshot) {
    return snapshot && snapshot.message.isConnected && snapshot.root.getClientRects().length > 0
      && snapshot.context === context(win) && snapshot.host.dataset.displayedSessionId === snapshot.sessionID
      && snapshot.message.textContent === snapshot.originalMessage;
  }
  async function worker(win, item, file, extra) {
    const library = Zotero.Libraries.get(item.libraryID);
    const kind = library.libraryType === 'group' ? 'group' : 'user';
    const id = kind === 'group' ? String(Zotero.Groups.getGroupIDFromLibraryID(item.libraryID)) : '0';
    const {Subprocess} = win.ChromeUtils.importESModule('resource://gre/modules/Subprocess.sys.mjs');
    const process = await Subprocess.call({command:deployment.python,
      arguments:[deployment.runner,'--item-key',item.key,'--library-type',kind,'--library-id',id,'--selection-file',file,...extra],
      workdir:deployment.project,stderr:'stdout'});
    let output = '';
    while (true) {const part=await process.stdout.readString();if(!part)break;output+=part;if(output.length>3000000)throw Error('本地程序输出异常');}
    await process.wait();
    try {return JSON.parse(output.trim());} catch {throw Error('无法解析本地同步结果，请检查本地日志');}
  }
  async function openPreview(win, state, snapshot) {
    if (busy || state.modal || !valid(win,snapshot)) return;
    state.modal = true; state.button.hidden = true;
    const doc=win.document, overlay=element(doc,'div'), panel=element(doc,'div');
    overlay.id='paper-selection-preview';
    overlay.style.cssText='position:fixed;inset:0;background:#0007;z-index:2147483647;display:flex;align-items:center;justify-content:center;';
    panel.style.cssText='background:white;color:#192334;border-radius:12px;padding:24px;width:min(720px,85vw);max-height:85vh;overflow:auto;font:14px system-ui;box-shadow:0 14px 50px #0004;';
    overlay.append(panel);doc.documentElement.append(overlay);
    panel.setAttribute('role','dialog');panel.setAttribute('aria-label','Paper Chat 选区同步预览');
    const style=element(doc,'style','#paper-selection-preview button:disabled{opacity:.45;cursor:default!important;}');
    panel.append(style);
    panel.append(element(doc,'h2','同步选中内容到 Notion'));
    const target=element(doc,'p'),pick=element(doc,'button','选择目标论文…');
    panel.append(target,pick,element(doc,'p','目标区域：Paper Chat 精选内容 · 仅发送以下选区，不保存 Zotero 子笔记'));
    const preview=element(doc,'pre',snapshot.text);
    preview.style.cssText='white-space:pre-wrap;overflow:auto;max-height:40vh;padding:16px;background:#f4f6f8;border-radius:8px;font:14px/1.7 system-ui;';
    const status=element(doc,'p','正在检查选区格式…'),actions=element(doc,'div');
    const cancel=element(doc,'button','取消'),send=element(doc,'button','同步'),link=element(doc,'button','打开 Notion 页面');
    link.hidden=true;send.disabled=true;
    for(const b of [cancel,send,link]) b.style.cssText='padding:8px 16px;margin-right:10px;cursor:pointer;';
    send.style.background='#2463cf';send.style.color='white';
    actions.append(cancel,send,link);panel.append(preview,status,actions);
    // Paper Chat can show a historical chat for a different paper than the reader.
    // Its DOM exposes a session ID, but no authoritative library/item binding.
    // Require an explicit item choice instead of treating the open PDF as proof.
    let item=null,file=null,hash=null,closed=false,url=null,generation=0;
    function close(){if(busy)return;closed=true;generation++;overlay.remove();state.modal=false;state.snapshot=null;}
    cancel.addEventListener('click',close);
    overlay.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close();}});
    function showTarget(){target.textContent=item ? '目标论文：'+item.getField('title')+' ['+item.key+']' : '请明确选择目标论文。当前阅读器：'+(snapshot.suggestedItem?.getField('title')||'无')+'（聊天可能来自其他论文）';}
    async function prepare(){
      const current=++generation;send.disabled=true;hash=null;showTarget();
      if(!item){status.textContent='请选择一个 Zotero 论文条目。';return;}
      try{
        const library=Zotero.Libraries.get(item.libraryID),kind=library.libraryType==='group'?'group':'user';
        const scope=kind==='group'?Zotero.Groups.getGroupIDFromLibraryID(item.libraryID):Zotero.Users.getCurrentUserID();
        if(!scope)throw Error('无法确认 Zotero 文献库身份');
        const requestID=Services.uuid.generateUUID().toString().replace(/[{}]/g,'');
        const request={schema_version:1,request_id:requestID,library_type:kind,library_id:String(scope),item_key:item.key,
          message_id:snapshot.messageID,session_id:snapshot.sessionID,captured_at:snapshot.capturedAt,text:snapshot.text,html:snapshot.html};
        const sep=deployment.project.includes('\\')?'\\':'/';
        const folder=[deployment.project,'.runtime','selections'].join(sep);
        await Zotero.File.createDirectoryIfMissingAsync(folder);
        const nextFile=folder+sep+requestID+'.json';
        await Zotero.File.putContentsAsync(nextFile,JSON.stringify(request));
        const result=await worker(win,item,nextFile,['--preview-selection']);
        if(closed||current!==generation)return;
        if(result.status!=='SELECTION_PREVIEW')throw Error(result.error||'预览失败');
        file=nextFile;hash=result.hash;
        status.textContent=result.warnings.length ? '格式提醒：'+result.warnings.join('；') : '仅同步预览中的内容，保留可支持的文字格式。';
        send.disabled=false;
      }catch(e){if(!closed&&current===generation)status.textContent=String(e.message||e);}
    }
    pick.addEventListener('click',async()=>{
      if(busy)return;
      const io={singleSelection:true};
      win.openDialog('chrome://zotero/content/selectItemsDialog.xhtml','','chrome,dialog,modal,centerscreen,resizable',io);
      if(io.dataOut?.length===1){const chosen=Zotero.Items.get(io.dataOut[0]);if(chosen?.isRegularItem()){item=chosen;await prepare();}}
    });
    send.addEventListener('click',async()=>{
      if(busy||!hash)return;
      if(!valid(win,snapshot)){status.textContent='论文、聊天或回答内容已变化，请取消后重新选择。';send.disabled=true;return;}
      busy=true;send.disabled=true;pick.disabled=true;cancel.disabled=true;status.textContent='正在核对论文关联并同步选中内容…';
      try{
        const result=await worker(win,item,file,['--selection-hash',hash]);
        if(!['SELECTION_SYNCED','SELECTION_UNCHANGED'].includes(result.status))throw Error(result.error||result.status);
        if(!/^https:\/\/www\.notion\.so\/[a-f0-9]{32}$/.test(result.url))throw Error('Notion 返回地址无效');
        url=result.url;link.hidden=false;cancel.textContent='关闭';
        status.textContent=result.status==='SELECTION_UNCHANGED'?'这段内容已同步，未重复追加。':'选中内容已同步到 Notion。';
      }catch(e){status.textContent=String(e.message||e);send.disabled=false;send.textContent='重试同一片段';}
      finally{busy=false;pick.disabled=false;cancel.disabled=false;}
    });
    link.addEventListener('click',()=>{if(url)Zotero.launchURL(url);});
    await prepare();
  }
  function attach(win){
    if(windows.has(win))return;
    const doc=win.document,button=element(doc,'button',label);
    button.id='paper-selection-sync';button.hidden=true;
    button.style.cssText='position:fixed;right:24px;bottom:145px;z-index:2147483646;background:#2463cf;color:white;border:0;border-radius:8px;padding:10px 16px;font:14px system-ui;box-shadow:0 3px 14px #0003;cursor:pointer;';
    doc.documentElement.append(button);
    const state={button,snapshot:null,modal:false};windows.set(win,state);
    const refresh=()=>{
      if(state.modal)return;
      state.snapshot=capture(win);
      const selection=doc.getSelection(),anchor=selection?.anchorNode;
      const withinChat=(anchor?.nodeType===1?anchor:anchor?.parentElement)?.closest('.chat-panel-root');
      const invalid=!!withinChat && !selection.isCollapsed && !!selection.toString().trim() && !state.snapshot;
      button.hidden=(!state.snapshot&&!invalid)||busy;
      button.disabled=!state.snapshot;
      button.textContent=invalid?'请仅选择同一条 AI 回答中的正文':label;
      button.title=invalid?'跨回答、输入框或不兼容的 Paper Chat 界面不能同步，请重新选择。':'';
    };
    button.addEventListener('mousedown',e=>e.preventDefault());
    button.addEventListener('click',()=>{if(state.snapshot)void openPreview(win,state,state.snapshot);});
    doc.addEventListener('selectionchange',refresh);
    doc.addEventListener('mouseup',refresh);
    const observer=new win.MutationObserver(()=>{
      if(state.modal)return;
      if(state.snapshot&&!valid(win,state.snapshot)){state.snapshot=null;button.hidden=true;}
    });
    observer.observe(doc.documentElement,{childList:true,subtree:true,characterData:true,
      attributes:true,attributeFilter:['data-displayed-session-id','hidden','style']});
    state.cleanup=()=>{observer.disconnect();doc.removeEventListener('selectionchange',refresh);doc.removeEventListener('mouseup',refresh);button.remove();doc.getElementById('paper-selection-preview')?.remove();};
  }
  function detach(win){windows.get(win)?.cleanup();windows.delete(win);}
  return {attach,detach};
})();
