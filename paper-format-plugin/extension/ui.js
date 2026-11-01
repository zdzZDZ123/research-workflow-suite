/* Local UI: manuscript strings are textContent, never HTML or network request fields. */
var PaperFormatterUI={
async open(win,api,selection){
  const doc=win.document,NS='http://www.w3.org/1999/xhtml';
  const e=(tag,text,attrs={})=>{const el=doc.createElementNS(NS,tag);if(text!==undefined)el.textContent=text;for(const [k,v]of Object.entries(attrs))el.setAttribute(k,v);return el;};
  const overlay=e('div',undefined,{id:'pf-dialog',role:'dialog','aria-label':'论文格式转换','aria-modal':'true'});
  const style=e('style',`
    #pf-dialog{position:fixed;inset:0;z-index:2147483646;background:#17312b66;display:flex;align-items:center;justify-content:center;font:14px/1.55 system-ui;color:#193a34}
    #pf-dialog *{box-sizing:border-box}#pf-dialog .pf-shell{width:96vw;height:94vh;background:#f7f9f7;border-radius:14px;display:flex;flex-direction:column;box-shadow:0 22px 80px #102d3255;overflow:hidden}
    #pf-dialog .pf-head{padding:16px 24px;border-bottom:1px solid #d8e3dd;display:flex;align-items:center;gap:16px;background:#fff}
    #pf-dialog .pf-head strong{font-size:20px}#pf-dialog .pf-head small{flex:1;color:#597069}
    #pf-dialog .pf-body{display:grid;grid-template-columns:340px 1fr;min-height:0;min-width:0;flex:1;border:0;margin:0;padding:0}#pf-dialog .pf-controls{overflow:auto;padding:22px;background:#fff;border-right:1px solid #d8e3dd}
    #pf-dialog .pf-main{min-width:0;overflow:auto;padding:22px}#pf-dialog h3{margin:12px 0 8px;font-size:16px}#pf-dialog p{margin:8px 0}
    #pf-dialog label{display:block;margin:10px 0 4px;font-weight:600}#pf-dialog input,#pf-dialog select,#pf-dialog textarea{font:inherit;color:#173d34;border:1px solid #b7cbc1;border-radius:5px;background:#fff;padding:8px;width:100%;resize:vertical}
    #pf-dialog input[type=checkbox]{width:auto;margin-right:8px}#pf-dialog textarea{min-height:70px}#pf-dialog button{font:inherit;padding:8px 12px;border:1px solid #a7c1b5;border-radius:5px;background:#f4f9f6;color:#173d34;cursor:pointer;margin:7px 6px 7px 0}
    #pf-dialog button.pf-primary{background:#146851;color:white;border-color:#146851}#pf-dialog button:disabled{opacity:.45;cursor:wait}#pf-dialog .pf-status{padding:10px 22px;background:#edf4ef;white-space:pre-wrap;border-top:1px solid #d8e3dd;max-height:130px;overflow:auto}
    #pf-dialog .pf-note{font-size:12px;color:#56736a}#pf-dialog .pf-bad{color:#a53b21;background:#fff4ee;border:1px solid #e9cabc;padding:10px;border-radius:5px;margin:6px 0;white-space:pre-wrap}
    #pf-dialog details{margin:12px 0;padding:10px;background:#f1f6f3;border-radius:7px}#pf-dialog summary{cursor:pointer;font-weight:600}
    #pf-dialog .pf-pages{display:grid;grid-template-columns:1fr 1fr;gap:15px;align-items:start}#pf-dialog .pf-page{min-width:0}#pf-dialog .pf-canvas{position:relative;background:#dde5e0;line-height:0;border:1px solid #becdc5;touch-action:none}#pf-dialog .pf-canvas img{width:100%;display:block}#pf-dialog .pf-box{position:absolute;border:2px solid #c95723;background:#e2a45544;pointer-events:none}
    #pf-dialog .pf-block{border-bottom:1px solid #cbdcd2;padding:10px 0}#pf-dialog .pf-block img{max-width:100%;max-height:200px}#pf-dialog table{border-collapse:collapse;width:100%;font-size:13px}#pf-dialog td{border-bottom:1px solid #d7e4da;padding:6px;overflow-wrap:anywhere}#pf-dialog .pf-row{display:flex;gap:8px}#pf-dialog .pf-row>*{flex:1}
  `);
  const shell=e('div',undefined,{class:'pf-shell'}),header=e('div',undefined,{class:'pf-head'}),body=e('fieldset',undefined,{class:'pf-body'});
  const left=e('div',undefined,{class:'pf-controls'}),main=e('div',undefined,{class:'pf-main'}),status=e('div','准备就绪',{class:'pf-status',role:'status'});
  header.append(e('strong','论文格式转换'),e('small','本机处理 · 原位覆盖 · 可恢复版本'));
  let busy=false,job=null,profile=null,template=null,manual={},embeddedManual={},reviews={},corrected=null,clearCorrection=false;
  const clone=value=>JSON.parse(JSON.stringify(value));
  function selectJob(value){job=value;const bound=job.preview_layout_sha256&&job.preview_layout_sha256===job.manual_layout_sha256;manual=clone(bound?job.manual_annotations||{}:{});embeddedManual=clone(bound?job.embedded_manual||{}:{});reviews=clone(job.reviews||{});corrected=null;clearCorrection=false;}
  function reviewRequest(){return {command:'review',run_id:job.run_id,reviews,manual,embedded_manual:embeddedManual,layout_sha256:job.preview_layout_sha256,...(corrected?{corrected_docx:corrected}:{}),...(clearCorrection?{clear_correction:true}:{})};}
  const close=()=>{if(busy)return;overlay.remove();api.closed(win);};
  const closeButton=e('button','关闭');closeButton.onclick=close;header.append(closeButton);
  body.append(left,main);shell.append(header,body,status);overlay.append(style,shell);doc.documentElement.append(overlay);
  const input=(label,value='',tag='input',parent=left)=>{const node=e(tag);node.value=value;parent.append(e('label',label),node);return node;};
  const button=(label,fn,parent=left,primary=false)=>{const node=e('button',label,{type:'button'});if(primary)node.className='pf-primary';node.onclick=()=>perform(fn);parent.append(node);return node;};
  async function perform(fn){
    if(busy)return;busy=true;body.disabled=true;closeButton.disabled=true;overlay.setAttribute('aria-busy','true');
    try{await fn();}catch(error){status.textContent=String(error.message||error);status.style.color='#a53b21';Zotero.logError(error);}
    finally{busy=false;body.disabled=false;closeButton.disabled=false;overlay.setAttribute('aria-busy','false');}
  }
  function progress(event){status.style.color='';status.textContent=event.message;}
  const call=req=>api.call(req,progress);
  function localURL(path){const file=Cc['@mozilla.org/file/local;1'].createInstance(Ci.nsIFile);file.initWithPath(path);return Services.io.newFileURI(file).spec;}
  async function pick(title,pattern){
    const fp=Cc['@mozilla.org/filepicker;1'].createInstance(Ci.nsIFilePicker);
    fp.init(win.browsingContext,title,Ci.nsIFilePicker.modeOpen);fp.appendFilter(title,pattern);
    const result=await new Promise(resolve=>fp.open(resolve));
    return result===Ci.nsIFilePicker.returnOK?fp.file.path:null;
  }
  let rows=await api.attachments(selection);
  const attachment=input('目标附件','','select');
  if(!rows.length)attachment.append(e('option','请在文献或附件上右键进入',{value:''}));
  else if(rows.length>1)attachment.append(e('option','请选择本次要覆盖的附件',{value:'',disabled:'',selected:''}));
  for(const item of rows)attachment.append(e('option',item.getField('title')||item.attachmentFilename,{value:item.id}));
  function target(){if(!attachment.value)throw Error('请先明确选择本次要覆盖的附件');const item=Zotero.Items.get(Number(attachment.value));if(!item)throw Error('请先选择一个 DOCX/PDF 附件');return item;}
  left.append(e('p','论文内容、页面、批注和模板正文留在本机。联网只解析你输入的格式要求及公开期刊指南。',{class:'pf-note'}));
  const instruction=input('你希望使用什么格式？','', 'textarea');instruction.placeholder='例如：A4；双栏；页边距 2.5cm；正文 宋体 12pt；1.5 倍行距';
  const journal=input('期刊名称（可选）');const article=input('文章类型（使用期刊规范时必填）');
  const url=input('出版方作者指南链接（可选）');
  function invalidateProfile(){profile=null;formatSummary.replaceChildren(e('p','格式来源已修改，请重新预览规则。',{class:'pf-note'}));}
  for(const field of [instruction,journal,article,url])field.addEventListener('input',invalidateProfile);
  const sources=e('div');left.append(sources);
  button('查找官方指南',async()=>{
    const result=await call({command:'search',input:{journal:journal.value.trim(),article_type:article.value.trim()}});
    sources.replaceChildren(e('p','请选择对应期刊及文章类型的出版方网站：',{class:'pf-note'}));
    for(const row of result.candidates){const b=button(row.title,async()=>{url.value=row.url;invalidateProfile();sources.replaceChildren(e('p',row.url,{class:'pf-note'}));},sources);b.title=row.url;}
  });
  const templateLabel=e('p','尚未选择模板',{class:'pf-note'});
  button('选择 DOCX / DOTX / PDF 模板',async()=>{
    const path=await pick('格式模板','*.docx;*.dotx;*.pdf');if(!path)return;
    template=(await call({command:'template',path})).profile;templateLabel.textContent=path;invalidateProfile();
  });left.append(templateLabel);
  button('移除模板',async()=>{template=null;templateLabel.textContent='尚未选择模板';invalidateProfile();});
  const formatSummary=e('div');
  function showProfile(){
    formatSummary.replaceChildren();if(!profile)return;
    formatSummary.append(e('h3','将应用的格式'));
    const table=e('table');
    function flatten(o,prefix=''){for(const[k,v]of Object.entries(o)){if(v&&typeof v==='object')flatten(v,prefix+k+'.');else{const row=e('tr');row.append(e('td',prefix+k),e('td',String(v)));table.append(row);}}}
    flatten(profile.rules);formatSummary.append(table);
    for(const warning of profile.unresolved||[])formatSummary.append(e('p',warning,{class:'pf-bad'}));
    if(profile.overrides?.length)formatSummary.append(e('p','已按你的要求覆盖其他来源的 '+profile.overrides.length+' 项规则。完整差异会写入检查报告。',{class:'pf-note'}));
    const details=e('details'),editor=e('textarea');details.append(e('summary','检查或编辑结构化格式规则'));editor.rows=16;editor.value=JSON.stringify(profile,null,2);details.append(editor);
    button('保存已确认的格式',async()=>{
      profile=(await call({command:'cache_profile',profile:JSON.parse(editor.value)})).profile;showProfile();status.textContent='已保存格式规则；未完成的内容检查仍会阻止覆盖。';
    },details);formatSummary.append(details);
  }
  async function resolveProfile(){
    let remote={};
    if(instruction.value.trim()||journal.value.trim()||url.value.trim()){
      status.textContent='解析格式要求…';
      remote=await call({command:'resolve',input:{instruction:instruction.value.trim(),journal:journal.value.trim(),article_type:article.value.trim(),guideline_url:url.value.trim()}});
    }
    if(remote.cached){
      // Resolve's cached combined profile has explicit precedence; requesting a template needs separate sources.
      if(template)remote=await call({command:'resolve',refresh:true,input:{instruction:instruction.value.trim(),journal:journal.value.trim(),article_type:article.value.trim(),guideline_url:url.value.trim()}});
      else profile=remote.profile;
    }
    if(!profile||!remote.cached)profile=(await call({command:'merge_profile',journal:remote.journal_profile,template,explicit:remote.explicit_profile||remote.profile})).profile;
    if(!Object.keys(profile.rules).length)throw Error('请填写格式要求或选择模板');
    main.replaceChildren(formatSummary);showProfile();return profile;
  }
  button('预览格式规则',resolveProfile);
  button('使用已缓存格式',async()=>{
    const data=await call({command:'profiles'});main.replaceChildren(e('h3','已验证的本地格式'));
    for(const row of data.profiles){button(row.id.slice(0,10)+' · '+JSON.stringify(row.profile.rules).slice(0,100),async()=>{profile=row.profile;main.replaceChildren(formatSummary);showProfile();},main);}
    if(!data.profiles.length)main.append(e('p','还没有已验证的格式。'));
  });
  button('转换并覆盖',async()=>{
    const item=target();if(!profile)await resolveProfile();
    if(profile.unresolved?.length)throw Error('格式规则仍有待确认项，请先核对并保存规则。');
    const current=await api.snapshot(item);
    const inspected=await call({command:'inspect',attachment:current.attachment});
    const citations=await api.localCitationUpdates(current.attachment.path,profile,item);
    selectJob(await call({command:'prepare',request:{schema_version:1,...current,source_sha256:inspected.source_sha256},profile}));
    await executeJob({command:'convert',run_id:job.run_id,...(citations?{citation_updates:citations}:{})},item);
  },left,true);
  button('任务记录与恢复',showHistory);
  const settings=e('details');settings.append(e('summary','首次安装 · 模型与检索设置'));left.append(settings);
  const base=input('兼容模型 API 地址','', 'input',settings);base.placeholder='https://…/v1';
  const model=input('模型 ID','','input',settings);
  const apiStyle=input('API 类型','','select',settings);for(const v of ['chat_completions','responses'])apiStyle.append(e('option',v,{value:v}));
  const modelKey=input('模型密钥（留空保留）','','input',settings);modelKey.type='password';
  const searchKey=input('Brave Search 密钥（留空保留）','','input',settings);searchKey.type='password';
  button('保存独立设置',async()=>{
    await call({command:'configure',settings:{base_url:base.value.trim(),model:model.value.trim(),api_style:apiStyle.value},model_key:modelKey.value,search_key:searchKey.value});
    modelKey.value='';searchKey.value='';status.textContent='设置已保存，密钥保存在 Windows 凭据管理器。';
  },settings);
  button('检查本机环境',async()=>{const data=await call({command:'doctor'});main.replaceChildren(e('h3','环境检查'),e('pre',JSON.stringify(data,null,2)));},settings);
  button('加载设置',async()=>{const data=await call({command:'settings'});base.value=data.base_url||'';model.value=data.model||'';apiStyle.value=data.api_style||'chat_completions';status.textContent='模型密钥：'+(data.has_model_key?'已配置':'未配置')+'；检索密钥：'+(data.has_search_key?'已配置':'未配置');},settings);
  main.append(e('h3','把论文切换到需要的格式'),e('p','选择附件并输入要求，然后点击“转换并覆盖”。检查通过后会替换当前附件，Zotero 条目和文件位置保持不变。'),e('p','首次使用请先检查本机环境。复杂自然语言和期刊规范需要独立配置模型；已缓存的规则以及常见格式指令可以离线使用。'),formatSummary);
  function pageViewer(parent,label,pages){
    const part=e('div',undefined,{class:'pf-page'}),choose=e('select'),canvas=e('div',undefined,{class:'pf-canvas'}),image=e('img',undefined,{alt:label});
    part.append(e('h3',label),choose,canvas);canvas.append(image);parent.append(part);
    for(const p of pages)choose.append(e('option','第 '+(p.index+1)+' 页',{value:p.index}));
    const view={choose,canvas,image,pages,onChange:null,show};image.draggable=false;
    function show(){const page=pages.find(p=>p.index===Number(choose.value));if(page)image.src=localURL(page.image);canvas.querySelectorAll('.pf-box').forEach(n=>n.remove());view.onChange?.();}
    choose.onchange=show;show();return view;
  }
  function drawPosition(view,position,original=false){
    if(!position||Number(view.choose.value)!==position.pageIndex)return;
    const page=view.pages.find(p=>p.index===position.pageIndex);if(!page)return;
    let rects=position.rects||[];
    if(!rects.length&&position.paths?.length){const pts=position.paths.flat(),xs=pts.filter((_,i)=>i%2===0),ys=pts.filter((_,i)=>i%2);rects=[[Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)]];}
    for(const [x0,y0,x1,y1]of rects){const box=e('div',undefined,{class:'pf-box'});Object.assign(box.style,{left:x0/page.width*100+'%',top:(page.height-y1)/page.height*100+'%',width:(x1-x0)/page.width*100+'%',height:(y1-y0)/page.height*100+'%'});if(original){box.style.borderColor='#2475ac';box.style.background='#2475ac22';}view.canvas.append(box);}
  }
  async function executeJob(request,item=targetForJob()){
    const runID=request.run_id;
    try{job=await call(request);if(job.status==='validated')job=await api.commit(item,job,progress);selectJob(job);renderJob();}
    catch(error){try{selectJob(await call({command:'load',run_id:runID}));renderJob();}catch(loadError){Zotero.logError(loadError);}throw error;}
  }
  function renderJob(){
    main.replaceChildren();
    const state={committed:'已覆盖并保留可恢复版本',needs_review:'需要本地校对，原稿尚未覆盖',validated:'检查通过，等待覆盖',failed:'转换失败，原稿保留',rolled_back:'已恢复一致版本'};
    main.append(e('h3',state[job.status]||job.status),e('p','任务 '+job.run_id,{class:'pf-note'}));
    if(job.error)main.append(e('p',(job.error.code||'')+'：'+(job.error.detail||''),{class:'pf-bad'}));
    for(const problem of job.report?.issues||[])main.append(e('p',problem.detail+(problem.page!==undefined?'（第 '+(problem.page+1)+' 页）':''),{class:'pf-bad'}));
    if(job.refresh_warning)main.append(e('p','文件已提交，但索引刷新失败：'+job.refresh_warning,{class:'pf-bad'}));
    if(job.reader_warning)main.append(e('p','文件已提交，请通过“打开结果”重新打开阅读器：'+job.reader_warning,{class:'pf-bad'}));
    const actions=e('div');main.append(actions);
    if(job.report_path)button('查看检查报告',()=>Zotero.launchFile(job.report_path),actions);
    if(job.status==='committed')button('打开结果',()=>Zotero.getMainWindow().ZoteroPane.viewAttachment(targetForJob().id),actions,true);
    if(job.editable_docx&&job.status!=='committed')button('打开本地排版校对稿',()=>Zotero.launchFile(job.editable_docx),actions);
    if(job.status==='needs_review'||job.status==='failed'){
      const correctionStatus=e('p',job.manual_document?'已保留本任务的人工修正稿，重新检查时继续使用。':'尚未选择人工修正稿',{class:'pf-note'});actions.append(correctionStatus);
      button('选择修正后的 DOCX',async()=>{const path=await pick('修正后的本地校对稿','*.docx');if(path){corrected=path;clearCorrection=false;correctionStatus.textContent='下次检查使用：'+path;}},actions);
      button('移除人工修正稿',async()=>{corrected=null;clearCorrection=true;correctionStatus.textContent='下次检查将根据原稿与格式规则重新排版，历史修正稿仍保留。';},actions);
    }
    if(job.status==='validated')button('覆盖原附件',async()=>{job=await api.commit(targetForJob(),job,progress);renderJob();},actions,true);
    if(!job.preview_original||!job.preview_new){
      if(['prepared','needs_review','failed'].includes(job.status))button('重新检查并覆盖',()=>executeJob(reviewRequest()),actions,true);
      status.textContent=state[job.status]||job.status;return;
    }
    const pages=e('div',undefined,{class:'pf-pages'});main.append(pages);
    const old=pageViewer(pages,'原稿',job.preview_original.pages),fresh=pageViewer(pages,'新稿',job.preview_new.pages,true);
    const annotationRows=[...(job.annotation_mapping?.unresolved||[]).map(a=>({...a,kind:'zotero'})),...(job.embedded_mapping?.unresolved||[]).map(a=>({...a,kind:'embedded'}))];
    if(annotationRows.length){
      const select=input('待定位批注：选择后在新稿上拖动框选目标区域','','select',main);
      for(const [i,a]of annotationRows.entries())select.append(e('option',a.type+' · '+(a.text||a.key),{value:i}));
      const note=e('p','',{class:'pf-note'});main.append(note);
      const selected=()=>annotationRows[Number(select.value)],locations=a=>a.kind==='zotero'?manual:embeddedManual;
      const multi=a=>['highlight','underline'].includes(a.type);
      function redraw(){
        const a=selected(),position=locations(a)[a.key];
        for(const view of [old,fresh])view.canvas.querySelectorAll('.pf-box').forEach(n=>n.remove());
        drawPosition(old,a.original_position,true);drawPosition(fresh,position);
        note.textContent=(position?'已记录 '+position.rects.length+' 个区域，待重新检查。':'尚未定位。')+(multi(a)?' 跨行高亮可在同一页逐行追加框选。':' 每次框选替换当前定位。');
      }
      const showAnchor=()=>{const a=selected();old.choose.value=a.original_position?.pageIndex??0;old.show();const pos=locations(a)[a.key];if(pos)fresh.choose.value=pos.pageIndex;fresh.show();redraw();};
      old.onChange=redraw;fresh.onChange=redraw;select.onchange=showAnchor;showAnchor();
      button('撤销上一框',async()=>{const a=selected(),map=locations(a),pos=map[a.key];if(pos){pos.rects.pop();if(!pos.rects.length)delete map[a.key];}redraw();},main);
      button('清除此批注定位',async()=>{const a=selected();delete locations(a)[a.key];redraw();},main);
      let start,box;
      function point(event){const r=fresh.image.getBoundingClientRect();return r.width&&r.height?{x:Math.max(0,Math.min(1,(event.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(event.clientY-r.top)/r.height))}:null;}
      fresh.canvas.onpointerdown=event=>{
        if(busy||event.button!==0)return;start=point(event);if(!start)return;
        event.preventDefault();box=e('div',undefined,{class:'pf-box'});fresh.canvas.append(box);fresh.canvas.setPointerCapture(event.pointerId);
      };
      fresh.canvas.onpointermove=event=>{
        if(!start)return;const end=point(event);if(!end)return;
        Object.assign(box.style,{left:Math.min(start.x,end.x)*100+'%',top:Math.min(start.y,end.y)*100+'%',width:Math.abs(end.x-start.x)*100+'%',height:Math.abs(end.y-start.y)*100+'%'});
      };
      fresh.canvas.onpointerup=event=>{
        if(!start)return;const end=point(event),begin=start;start=null;
        if(!end){redraw();return;}
        const pi=Number(fresh.choose.value),p=fresh.pages.find(p=>p.index===pi),a=selected(),map=locations(a),previous=map[a.key];
        if(Math.abs(end.x-begin.x)*p.width<1||Math.abs(end.y-begin.y)*p.height<1){redraw();status.textContent='框选范围太小，请拖动选择完整文字区域。';return;}
        if(multi(a)&&previous&&previous.pageIndex!==pi){redraw();status.textContent='一个批注的多个区域需位于同一页。更换页面定位前，请先清除此批注定位。';return;}
        const rect=[Math.min(begin.x,end.x)*p.width,(1-Math.max(begin.y,end.y))*p.height,Math.max(begin.x,end.x)*p.width,(1-Math.min(begin.y,end.y))*p.height];
        map[a.key]={pageIndex:pi,rects:[...(multi(a)?previous?.rects||[]:[]),rect]};
        redraw();status.textContent='已记录 '+a.key+' 的定位，重新检查时会验证引用文字是否一致。';
      };
      fresh.canvas.onpointercancel=fresh.canvas.onlostpointercapture=()=>{if(start){start=null;redraw();}};
    }
    if(job.extraction?.blocks?.some(b=>b.review_required)){
      const details=e('details');details.open=true;details.append(e('summary','本地识别校对：逐块对照原稿'));main.append(details);
      for(const b of job.extraction.blocks){
        if(!b.review_required)continue;
        const card=e('div',undefined,{class:'pf-block'});details.append(card);
        button('原稿第 '+(b.page+1)+' 页 · '+(b.label||b.kind),async()=>{old.choose.value=b.page;old.show();old.canvas.scrollIntoView({block:'center'});},card);
        let edit;
        const order=input('阅读顺序',String(reviews[b.id]?.order??b.order),'input',card);order.type='number';order.step='0.1';
        const kind=input('内容类型','','select',card);
        const choices=b.kind==='image'?['image']:b.table?['text','heading','table']:['text','heading'];
        const names={image:'保留原区域',text:'正文',heading:'标题',table:'表格'};
        for(const k of choices)kind.append(e('option',names[k],{value:k}));kind.value=reviews[b.id]?.kind??b.kind;
        if(b.image){const img=e('img',undefined,{src:localURL(b.image),alt:'原区域'});card.append(img);}
        if(b.kind==='table'){edit=e('textarea');edit.rows=12;edit.value=JSON.stringify(reviews[b.id]?.table??b.table,null,2);card.append(e('p','表格使用结构化单元格；误识别为表格时可改为正文。'),edit);}
        else if(b.kind!=='image'){edit=e('textarea');edit.value=reviews[b.id]?.text??b.text;card.append(edit);}
        const check=e('input',undefined,{type:'checkbox'}),label=e('label');check.checked=!!reviews[b.id]?.confirmed;
        label.append(check,doc.createTextNode('已对照原区域核对内容、范围和顺序'));card.append(label);
        function changed(){
          if(check.checked){
            try{reviews[b.id]={confirmed:true,kind:kind.value,order:Number(order.value),...(edit?(kind.value==='table'?{table:JSON.parse(edit.value)}:{text:edit.value}):{})};}
            catch(error){check.checked=false;status.textContent='表格结构不是有效 JSON，请修正后确认。';}
          }else delete reviews[b.id];
        }
        check.onchange=changed;if(edit)edit.oninput=()=>{check.checked=false;changed();};
        order.oninput=()=>{check.checked=false;changed();};kind.onchange=()=>{check.checked=false;if(edit)edit.value=kind.value==='table'?JSON.stringify(b.table,null,2):b.text;changed();};
      }
    }
    if(job.status==='needs_review'||job.status==='failed'){
      if(job.profile?.rules.references?.style_id||job.profile?.rules.references?.csl_xml){
        button('在本机补齐引文对应关系',async()=>{
          const context=await call({command:'citation_context',run_id:job.run_id});
          const panel=e('details');panel.open=true;panel.append(e('summary','文内引文 ↔ 本地 Zotero 文献'));
          panel.append(e('p','填写段落编号、原引文文字及对应 Zotero 条目键。数据只在本机读取，不自动补造作者、年份等信息。'));
          const referenceList=e('pre',context.paragraphs.map(p=>p.paragraph+': '+p.text).join('\n'));referenceList.style.whiteSpace='pre-wrap';panel.append(referenceList);
          const editor=e('textarea');editor.rows=14;editor.value=JSON.stringify({citations:[{paragraph:0,quote:'[1]',item_keys:[]}],bibliography_paragraphs:[]},null,2);panel.append(editor);
          const confirm=e('input',undefined,{type:'checkbox'}),label=e('label');label.append(confirm,doc.createTextNode('已逐项核对全部文内引文和参考文献对应关系'));panel.append(label);
          button('校验本地对应并重新排版',async()=>{
            if(!confirm.checked)throw Error('请先逐项核对对应关系');
            const mapping=JSON.parse(editor.value);mapping.confirmed=true;const item=targetForJob();
            for(const c of mapping.citations||[]){
              c.items=[];
              for(const key of c.item_keys||[]){
                const reference=Zotero.Items.getByLibraryAndKey(item.libraryID,key);
                if(!reference||!reference.isRegularItem())throw Error('找不到本地文献条目 '+key);
                await reference.loadAllData();
                c.items.push({id:key,uris:[Zotero.URI.getItemURI(reference)],itemData:{...Zotero.Utilities.Item.itemToCSLJSON(reference),id:key}});
              }
              delete c.item_keys;
            }
            const seeded=await call({command:'seed_citations',run_id:job.run_id,mapping});
            const updates=await api.localCitationUpdates(null,job.profile,item,seeded.citations);
            await executeJob({...reviewRequest(),citation_updates:updates},item);
          },panel,true);main.append(panel);panel.scrollIntoView({block:'start'});
        },main);
      }
      button('重新检查并覆盖',async()=>{
        await executeJob(reviewRequest());
      },main,true);
    }
    status.textContent=state[job.status]||job.status;
  }
  function targetForJob(){const item=Zotero.Items.getByLibraryAndKey(job.attachment.library_id,job.attachment.key);if(!item)throw Error('找不到原附件，不能提交');return item;}
  async function showHistory(){
    const data=await call({command:'history'});main.replaceChildren(e('h3','任务记录与恢复'));
    if(api.recoveryError)main.append(e('p',api.recoveryError,{class:'pf-bad'}));
    for(const error of data.errors||[])main.append(e('p','任务 '+error.run_id+' 的记录无法读取：'+error.detail+'。原始目录保留，请修复记录后再提交新转换。',{class:'pf-bad'}));
    for(const warning of api.recoverySummary?.warnings||[])main.append(e('p','任务 '+warning.run_id+' 已恢复，但刷新失败：'+warning.detail,{class:'pf-bad'}));
    button('恢复未完成的提交',async()=>{const result=await api.recoverPending();await showHistory();status.textContent='已恢复 '+result.recovered.length+' 项；仍需处理 '+result.errors.length+' 项。';},main);
    if(!data.jobs.length&&!data.errors?.length)main.append(e('p','还没有转换记录。'));
    for(const record of data.jobs){
      const card=e('div',undefined,{class:'pf-block'});card.append(e('strong',record.attachment.key+' · '+record.status),e('p',record.created_at,{class:'pf-note'}));main.append(card);
      button('查看 / 继续校对',async()=>{selectJob(await call({command:'load',run_id:record.run_id}));renderJob();},card);
      if(record.status==='committed')button('恢复此次转换前的版本',async()=>{
        selectJob(await call({command:'load',run_id:record.run_id}));const item=targetForJob();
        if(!win.confirm('将恢复此任务开始前的文件和批注。当前状态也会单独备份，可再次恢复。继续？'))return;
        job=await call({command:'restore',run_id:record.run_id,...await api.snapshot(item)});
        job=await api.commit(item,job,progress);renderJob();
      },card);
    }
  }
  overlay.addEventListener('keydown',event=>{if(event.key==='Escape')close();});
  return close;
}
};
