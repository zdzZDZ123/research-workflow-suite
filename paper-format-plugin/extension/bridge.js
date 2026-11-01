/* Privileged Zotero API adapter. No backend SQLite access and no network document transport. */
var PaperFormatter = (()=>{
  let deployment, recoveryTask;
  const running=new Set(), windows=new Map();
  const keys=['key','type','text','comment','color','pageLabel','sortIndex','position','tags','authorName','isExternal'];
  const stable=value=>{
    if(Array.isArray(value))return value.map(stable);
    if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(k=>[k,stable(value[k])]));
    return value;
  };
  function comparable(rows) {
    return JSON.stringify(stable(rows.map(a=>{
      const o=Object.fromEntries(keys.filter(k=>k in a).map(k=>[k,a[k]]));
      if(o.tags)o.tags=[...o.tags].sort((a,b)=>JSON.stringify(stable(a)).localeCompare(JSON.stringify(stable(b))));
      return o;
    }).sort((a,b)=>a.key.localeCompare(b.key))));
  }
  async function deploymentConfig(){
    if(deployment)return deployment;
    const environment=Cc['@mozilla.org/process/environment;1'].getService(Ci.nsIEnvironment);
    const override=Zotero.Prefs.get('extensions.paperformatter.deployment',true);
    const files=override?[override]:[rootURI+'deployment.json',environment.get('LOCALAPPDATA')+'\\PaperFormatter\\deployment.json'];
    for(const file of files){
      try{
        let source=file;
        if(/^(jar:|file:)/.test(file)){
          const {NetUtil}=ChromeUtils.importESModule('resource://gre/modules/NetUtil.sys.mjs');
          source=NetUtil.newChannel({uri:file,loadUsingSystemPrincipal:true});
        }
        const raw=await Zotero.File.getContentsAsync(source,'utf-8');
        deployment=JSON.parse(raw.replace(/^\uFEFF/,''));break;
      }catch(e){Zotero.debug('Paper Formatter deployment binding: '+String(e));}
    }
    if(!deployment)throw Error('请先运行 Install-Backend.ps1 安装本地后端，并安装脚本生成的本机 XPI。');
    if(!deployment.python||!deployment.project)throw Error('本地后端配置不完整');
    return deployment;
  }
  async function call(request,onProgress=()=>{}){
    const deploy=await deploymentConfig();
    const {Subprocess}=ChromeUtils.importESModule('resource://gre/modules/Subprocess.sys.mjs');
    const process=await Subprocess.call({command:deploy.python,arguments:['-m','paper_formatter.cli'],workdir:deploy.project,
      environment:{PAPER_FORMATTER_HOME:deploy.home,PYTHONUTF8:'1',PYTHONUNBUFFERED:'1'},environmentAppend:true,stderr:'pipe'});
    let stderr='';
    const drain=(async()=>{while(true){const s=await process.stderr.readString();if(!s)break;stderr=(stderr+s).slice(-4000);}})();
    await process.stdin.write(JSON.stringify(request));
    await process.stdin.close();
    let buffer='',result,error,total=0;
    while(true){
      const chunk=await process.stdout.readString();if(!chunk)break;
      total+=chunk.length;if(total>40_000_000){process.kill();throw Error('本地后端响应过大');}
      buffer+=chunk;
      let i;
      while((i=buffer.indexOf('\n'))>=0){
        const line=buffer.slice(0,i).trim();buffer=buffer.slice(i+1);if(!line)continue;
        const message=JSON.parse(line);
        if(message.event==='progress')onProgress(message);
        else if(message.event==='result')result=message.data;
        else if(message.event==='error')error=message.code+(message.detail?': '+message.detail:'');
      }
    }
    const exit=await process.wait();await drain;
    if(error||exit.exitCode!==0||!result)throw Error(error||'本地后端未正常返回：'+stderr);
    return result;
  }
  async function attachments(item){
    if(!item)return [];
    const rows=item.isAttachment()?[item]:Zotero.Items.get(item.getAttachments());
    const result=[];
    for(const a of rows){const path=await a.getFilePathAsync();if(path&&/\.(docx|pdf)$/i.test(path))result.push(a);}
    return result;
  }
  async function snapshot(item,{allowMissing=false}={}){
    if(!item?.isFileAttachment())throw Error('目标必须是本地 DOCX 或 PDF 附件');
    const library=Zotero.Libraries.get(item.libraryID);
    if(!library.editable||library.filesEditable===false)throw Error('当前文库没有附件写入权限');
    if(item.deleted)throw Error('附件已在回收站中');
    const path=await item.getFilePathAsync()||(allowMissing?item.getFilePath():false);
    if(!path||!/\.(docx|pdf)$/i.test(path))throw Error('找不到本地 DOCX/PDF 文件；旧版 .doc 请先转换');
    await item.loadDataType('childItems');
    const rows=item.getAnnotations();
    const annotations=[];
    for(const a of rows){
      await a.loadAllData();
      const json=Zotero.Annotations.toJSONSync(a);
      if(json.readOnly&&!json.isExternal)throw Error('包含无权修改的批注，请在有编辑权限的文库中操作');
      annotations.push(json);
    }
    return {attachment:{library_id:item.libraryID,key:item.key,parent_key:item.parentKey||'',path},annotations};
  }
  async function localCitationUpdates(path,profile,item,clustersOverride=null){
    const rule=profile.rules.references||{};
    if(!rule.style_id&&!rule.csl_xml)return null;
    let clusters=clustersOverride;
    if(!clusters){const inspected=await call({command:'inspect',attachment:(await snapshot(item)).attachment});clusters=inspected.document?.citations;}
    if(!clusters?.length)return null;
    await Zotero.Styles.init();
    const id=rule.style_id?.startsWith('http')?rule.style_id:'http://www.zotero.org/styles/'+rule.style_id;
    const style=rule.csl_xml?new Zotero.Style(rule.csl_xml):Zotero.Styles.get(id);
    if(!style)throw Error('CSL_STYLE_NOT_INSTALLED: 请先在 Zotero 引用设置中安装样式 '+id);
    const items=new Map(), citations=[];
    for(const cluster of clusters){
      const copy=JSON.parse(JSON.stringify(cluster));
      copy.properties={noteIndex:copy.properties?.noteIndex||0};
      for(const c of copy.citationItems){
        let data=c.itemData;
        if(!data){
          for(const uri of c.uris||[]){const found=await Zotero.URI.getURIItem(uri);if(found){data=Zotero.Utilities.Item.itemToCSLJSON(found);break;}}
        }
        if(!data?.title||!data.type)throw Error('CITATION_METADATA_REQUIRED: 引文缺少标题或类型；不能补造文献信息');
        const key=String(c.id);
        if(items.has(key)&&JSON.stringify(items.get(key))!==JSON.stringify({...data,id:key}))throw Error('CITATION_ID_CONFLICT');
        items.set(key,{...data,id:key});c.id=key;
      }
      citations.push(copy);
    }
    const system=new Zotero.Cite.System({automaticJournalAbbreviations:false});
    system.retrieveItem=id=>{if(!items.has(String(id)))throw Error('CITATION_ITEM_NOT_FOUND');return items.get(String(id));};
    const xml=style.getXML();
    const processor=new Zotero.CiteProc.CSL.Engine(system,xml,rule.locale||'en-US',true);
    processor.setOutputFormat('html');
    const output=processor.rebuildProcessorState(citations,'html');
    const result={citations:{},bibliography:[],style_id:style.styleID||id,locale:rule.locale||'en-US'};
    for(const [citationID,noteIndex,markup] of output){
      const parser=new (Zotero.getMainWindow().DOMParser)();
      const text=parser.parseFromString(markup,'text/html').body.textContent;
      result.citations[citationID]={html:markup,text};
    }
    const bib=processor.makeBibliography();
    if(bib){result.bibliography=bib[1];result.bibliography_parameters=bib[0];}
    if(Object.keys(result.citations).length!==clusters.length)throw Error('CSL_RESULT_INCOMPLETE');
    return result;
  }
  async function setAnnotations(item,rows,restore=false){
    if(restore){
      const wanted=new Set(rows.map(a=>a.key));
      for(const a of item.getAnnotations()){if(!wanted.has(a.key)){a.deleted=true;await a.save({skipSelect:true});}}
    }
    for(const json of rows){
      let a=Zotero.Items.getByLibraryAndKey(item.libraryID,json.key);
      if(!a){
        if(!restore)throw Error('ANNOTATION_DISAPPEARED');
        a=new Zotero.Item('annotation');a.libraryID=item.libraryID;a.key=json.key;a.parentID=item.id;
      }
      if(restore){
        a.annotationType=json.type;a.annotationAuthorName=json.authorName||'';
        if(['highlight','underline'].includes(json.type))a.annotationText=json.text||'';
        a.annotationIsExternal=!!json.isExternal;a.annotationComment=json.comment||'';a.annotationColor=json.color;
        a.setTags((json.tags||[]).map(t=>({tag:t.name})));a.deleted=false;
      }
      a.annotationPosition=JSON.stringify(json.position);
      a.annotationPageLabel=json.pageLabel;
      a.annotationSortIndex=json.sortIndex;
      await a.save({skipSelect:true});
    }
  }
  async function refresh(item){
    await Zotero.Sync.Storage.Local.checkForUpdatedFiles(item.libraryID,[item.id]);
    await Zotero.Notifier.trigger('modify','item',[item.id],{});
    if(item.isPDFAttachment())await Zotero.Fulltext.indexItems([item.id]);
  }
  async function commit(item,job,progress=()=>{}){
    if(running.has(item.id))throw Error('此附件已有操作正在运行');
    running.add(item.id);
    const openReaders=(Zotero.Reader._readers||[]).filter(r=>r.itemID===item.id);
    let replaced=false;
    try{
      for(const reader of openReaders)reader.close();
      await Zotero.DB.executeTransaction(async()=>{
        const current=await snapshot(item);
        if(comparable(current.annotations)!==comparable(job.annotations_original))throw Error('ANNOTATIONS_CHANGED');
        job=await call({command:'begin_commit',run_id:job.run_id,...current},progress);
        replaced=true;
        await setAnnotations(item,job.annotations_target,!!job.restores_run);
      });
      const after=await snapshot(item);
      job=await call({command:'finish_commit',run_id:job.run_id,...after},progress);
      try{await refresh(item);}catch(error){job.refresh_warning=String(error);Zotero.logError(error);}
      // Reopening a reader is presentation only: the file and database are already committed.
      if(openReaders.length){try{await Zotero.Reader.open(item.id);}catch(error){job.reader_warning=String(error);Zotero.logError(error);}}
      return job;
    }catch(error){
      if(replaced){
        try{
          const rolled=await call({command:'begin_rollback',run_id:job.run_id,...await snapshot(item)});
          await Zotero.DB.executeTransaction(()=>setAnnotations(item,rolled.annotations_original,!!rolled.restores_run));
          await call({command:'finish_rollback',run_id:job.run_id,...await snapshot(item)});
          await refresh(item);
        }catch(recovery){throw Error(String(error)+'\n恢复待完成：'+String(recovery)+'。请打开任务记录，原稿备份仍保留。');}
      }
      throw error;
    }finally{running.delete(item.id);}
  }
  async function recoverPending(){
    if(recoveryTask)return recoveryTask;
    recoveryTask=recoverJobs();
    try{return await recoveryTask;}finally{recoveryTask=null;}
  }
  async function recoverJobs(){
    let pending;
    try{pending=await call({command:'pending'});}
    catch(error){api.recoveryError='无法读取未完成的提交：'+String(error);throw error;}
    const result={recovered:[],errors:[...(pending.errors||[])],warnings:[]};
    for(const job of pending.jobs){
      let item,locked=false;
      try{
        item=Zotero.Items.getByLibraryAndKey(job.attachment.library_id,job.attachment.key);
        if(!item)throw Error('找不到原附件，请恢复文库连接后重试');
        if(running.has(item.id))throw Error('此附件正在处理中，请稍后重试恢复');
        running.add(item.id);locked=true;
        let action;
        await Zotero.DB.executeTransaction(async()=>{
          const current=await snapshot(item,{allowMissing:true});
          if(job.status==='awaiting_zotero'&&(await item.getFilePathAsync())&&comparable(current.annotations)===comparable(job.annotations_target)){
            await call({command:'finish_commit',run_id:job.run_id,...current});action='committed';
          }else{
            const rolled=await call({command:'begin_rollback',run_id:job.run_id,...current});
            await setAnnotations(item,rolled.annotations_original,!!rolled.restores_run);action='rolled_back';
          }
        });
        if(action==='rolled_back')await call({command:'finish_rollback',run_id:job.run_id,...await snapshot(item)});
        result.recovered.push({run_id:job.run_id,status:action});
        try{await refresh(item);}catch(error){result.warnings.push({run_id:job.run_id,detail:String(error)});Zotero.logError(error);}
      }catch(error){result.errors.push({run_id:job.run_id,detail:String(error)});Zotero.logError(error);}
      finally{if(locked)running.delete(item.id);}
    }
    api.recoverySummary=result;
    api.recoveryError=result.errors.map(e=>'任务 '+e.run_id+'：'+e.detail).join('\n');
    return result;
  }
  const api={call,snapshot,attachments,commit,recoverPending,localCitationUpdates,comparable,running,
    async openUI(item){
      const win=Zotero.getMainWindow();if(windows.has(win))return windows.get(win).ready;
      const entry={close:null,ready:null,closeRequested:false};windows.set(win,entry);
      // A late callback from an older dialog must never unregister its replacement.
      const dialogAPI=Object.create(api);dialogAPI.closed=closedWin=>api.closed(closedWin,entry);
      entry.ready=Promise.resolve().then(()=>api.UI.open(win,dialogAPI,item)).then(close=>{
        entry.close=close;
        if(entry.closeRequested||windows.get(win)!==entry)close();
      }).catch(error=>{
        if(windows.get(win)===entry){windows.delete(win);win.document.getElementById('pf-dialog')?.remove();}
        throw error;
      });
      return entry.ready;
    },
    closeUI(win){const entry=windows.get(win);if(entry?.close)entry.close();else if(entry)entry.closeRequested=true;},
    closed(win,entry){if(entry&&windows.get(win)===entry)windows.delete(win);}
  };
  return api;
})();
