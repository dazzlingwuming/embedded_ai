const visionElement=id=>document.getElementById(id);
const visionPanel=visionElement('visionPanel');
let visionStatus=null,visionSelection=null,visionRequest=null,visionSetting=false;
let visionStatusError='',visionConfigError='',selectionUpdatedAt=0,configSavedAt=0,selectionIdentity='',selectionMediaIdentity='';
let visionTimer;

function safeVisionImage(value){
  if(typeof value!=='string'||!value.startsWith('/')||value.startsWith('//')||/[\\\u0000-\u001f]/.test(value))return null;
  try{
    const url=new URL(value,location.origin);
    return url.origin===location.origin&&['http:','https:'].includes(url.protocol)?url.pathname+url.search:null;
  }catch{return null;}
}

function visionTime(value){
  const time=new Date(value);
  return Number.isNaN(time.getTime())?'采集时间未知':time.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}

function visionQuality(value){
  const names={good:'较清晰',clear:'较清晰',acceptable:'可用',fair:'一般',poor:'质量较低',blurry:'较模糊',blur:'较模糊',dark:'偏暗',bright:'过亮',unknown:'未评估'};
  if(typeof value==='string')return names[value]||value;
  if(typeof value==='number'&&Number.isFinite(value))return `画面评分 ${Number(value.toFixed(2))}`;
  if(value&&typeof value==='object'){
    const hints=[];
    if(value.very_dark)hints.push('画面过暗');else if(value.underexposed)hints.push('画面偏暗');
    if(value.very_bright)hints.push('画面过亮');else if(value.overexposed)hints.push('部分画面过亮');
    if(value.low_texture)hints.push('纹理较少，清晰度难判断');
    else if(Array.isArray(value.warnings)&&value.warnings.includes('low_sharpness'))hints.push('可能偏模糊');
    if(hints.length)return hints.join('；');
    const label=value.label??value.level??value.status;
    if(typeof label==='string')return names[label]||label;
    if(typeof value.score==='number'&&Number.isFinite(value.score))return '相对较清晰（画面指标）';
  }
  return '未评估';
}

function visionWarnings(selection){
  const warnings=Array.isArray(selection?.warnings)?selection.warnings:[];
  const names={no_frames:'暂无可用画面',camera_unavailable:'摄像头暂不可用',stale_frame:'画面较早，可能与眼前场景不同',blurry:'画面较模糊',low_quality:'画面质量较低',too_dark:'画面偏暗',too_bright:'画面过亮',low_texture_sharpness_is_inconclusive:'画面纹理较少，清晰度难判断；可对准文字或边缘',low_sharpness:'画面可能偏模糊，请保持镜头和物体稳定',underexposed:'画面偏暗，可增加照明',overexposed:'画面过亮，可避开强光',older_different_appearance_frames_omitted:'已忽略外观差异较大的旧画面',latest_frame_scoring_did_not_finish:'最新画面还在检查中，本轮不使用旧画面',latest_frame_could_not_be_scored:'最新画面暂时无法检查，本轮不使用旧画面',near_duplicate_frames_omitted:'相似画面已去重',fewer_distinct_usable_frames_than_requested:'可用且不同的画面数量不足',recent_scoring_error:'近期画面检查出现异常，可等待新画面或重试',quality_worker_not_running:'后台画面检查尚未运行',window_limited_to_cache_retention:'仅在本机保留的近期画面中选择'};
  return [...new Set(warnings.filter(item=>typeof item==='string'&&item!=='quality_is_heuristic_texture_and_noise_affect_sharpness').map(item=>names[item]||'画面检查有额外限制，请留意本轮回答中的说明。'))];
}

function setVisionStatus(text,state='waiting'){
  const label=visionElement('visionState');
  if(label.textContent!==text)label.textContent=text;
  visionPanel.dataset.state=state;
}

function paintVisionStatus(){
  const toggle=visionElement('visionEnabled');
  toggle.disabled=visionSetting||!visionStatus;
  if(visionStatus&&!visionSetting)toggle.checked=!!visionStatus.enabled;
  visionElement('visionToggleState').textContent=visionSetting?'正在保存…':visionStatus?(visionStatus.enabled?'已开启':'已关闭'):'正在检查';
  const sampler=visionStatus?.sampler;
  const samplerProblem=visionStatus?.enabled&&sampler?.error?'后台图片检查暂不可用，可重试；聊天仍可继续。':'';
  const problem=visionConfigError||visionStatusError||visionStatus?.error||samplerProblem;
  visionElement('visionError').textContent=typeof problem==='string'?problem:problem?'视觉观察暂不可用，请重试。':'';
  visionElement('visionError').hidden=!problem;
  visionElement('visionRetry').hidden=!problem;
  const count=Number.isFinite(sampler?.cache_frames)?sampler.cache_frames:null;
  const age=sampler?.last_received_age_seconds;
  visionElement('visionSamplerMeta').textContent=count===null?'':`本机缓存 ${count} 张${typeof age==='number'&&Number.isFinite(age)?` · 最近画面 ${Math.max(0,age).toFixed(1)} 秒前收到`:''}`;
  visionElement('visionSamplerMeta').hidden=!visionStatus?.enabled||count===null;
  if(visionSetting){setVisionStatus('正在更新摄像头设置');return;}
  if(visionStatusError){setVisionStatus('视觉状态暂不可用；聊天仍可继续','error');return;}
  if(!visionStatus){setVisionStatus('正在检查后台取帧状态');return;}
  if(!visionStatus.enabled){setVisionStatus('已关闭 · 后续提问不附摄像头图片','off');return;}
  if(visionStatus.error||samplerProblem){setVisionStatus('取帧遇到问题；本轮无图时仍可提问','error');return;}
  if(visionStatus.observing){
    const available=visionStatus.ready&&(count===null||count>0);
    setVisionStatus(available?'后台正在取帧 · 提问时再选图':'后台正在取帧 · 等待可用画面',available?'ready':'waiting');return;
  }
  setVisionStatus(visionStatus.ready?'已有可用画面 · 等待后台更新':'等待摄像头画面',visionStatus.ready?'ready':'waiting');
}

function updateVisionSelection(selection,{fromPoll=false}={}){
  if(selection===undefined)return;
  if(!fromPoll)selectionUpdatedAt=performance.now();
  visionSelection=selection&&typeof selection==='object'?selection:null;
  const identity=JSON.stringify(visionSelection);
  if(identity===selectionIdentity)return;
  selectionIdentity=identity;
  const images=Array.isArray(visionSelection?.selected)?visionSelection.selected.filter(item=>item&&typeof item==='object'):[];
  const gallery=visionElement('visionImages');
  visionElement('visionEmpty').hidden=images.length>0;
  visionElement('visionQualityHint').hidden=!images.length;
  visionElement('visionSelectionMeta').textContent=visionSelection?
    `${visionSelection.source==='voice'?'语音提问 · ':visionSelection.source==='text'?'文字提问 · ':''}本轮选中 ${images.length} 张${Number.isFinite(visionSelection.candidate_count)?` · ${visionSelection.candidate_count} 张候选`:''}`:'尚未为问题选图';
  const reasons={disabled:'摄像头参与对话已关闭',camera_unavailable:'摄像头暂不可用',no_usable_frames:'候选画面过暗或过亮，暂未选图',no_recent_frames:'没有近期可用画面',no_decodable_frames:'收到的图片暂时无法读取',no_frames_received:'尚未收到摄像头画面',latest_frame_pending:'最新画面尚在检查，暂不使用旧画面',latest_frame_unavailable:'最新画面暂不可用，可等待新画面后再问',vision_changed:'摄像头设置已变化'};
  const reason=reasons[visionSelection?.reason];
  visionElement('visionEmpty').textContent=visionSelection?`${reason?reason+'。':''}本轮没有附带图像，文字和语音对话仍可继续。`:'尚未提问。开启后在后台取帧，提问时再选择图片。';
  const phases={selected:'已选图，等待请求',requesting:'正随问题请求 AI',response_received:'AI 正在回答',completed:'本轮回答完成',cancelled:'本轮已取消',error:'本轮回答未完成'};
  visionElement('visionSelectionPhase').textContent=images.length?(phases[visionSelection?.phase]||'已为本轮问题选图'):'';
  const warnings=visionWarnings(visionSelection);
  visionElement('visionWarnings').textContent=warnings.join('；');
  visionElement('visionWarnings').hidden=!warnings.length;
  const mediaIdentity=JSON.stringify([visionSelection?.selection_id,images]);
  if(mediaIdentity===selectionMediaIdentity)return;
  selectionMediaIdentity=mediaIdentity;gallery.replaceChildren();
  for(const frame of images){
    const figure=document.createElement('figure');figure.className='vision-image';
    const media=document.createElement('div');media.className='vision-image-media';
    const image=document.createElement('img');image.width=160;image.height=120;image.alt='最近一次用于提问的摄像头图片';image.decoding='async';image.referrerPolicy='same-origin';image.hidden=true;
    const fallback=document.createElement('span');fallback.textContent='正在加载图片…';
    const retry=document.createElement('button');retry.type='button';retry.className='text-button';retry.textContent='重试图片';retry.hidden=true;
    const imageURL=safeVisionImage(frame.image_url);
    image.onload=()=>{image.hidden=false;fallback.hidden=true;retry.hidden=true;};
    image.onerror=()=>{image.hidden=true;fallback.hidden=false;fallback.textContent='图片暂时无法显示';retry.hidden=false;};
    retry.onclick=()=>{
      if(!imageURL)return;
      fallback.textContent='正在重新加载图片…';retry.hidden=true;
      const url=new URL(imageURL,location.origin);url.searchParams.set('preview_retry',String(Date.now()));
      image.src=url.pathname+url.search;
    };
    if(imageURL)image.src=imageURL;
    else fallback.textContent='图片地址不可用，仅接受本站图片';
    media.append(image,fallback,retry);
    const caption=document.createElement('figcaption'),time=document.createElement('time'),quality=document.createElement('span');
    time.textContent=`接收 ${visionTime(frame.received_utc)}`;
    time.title='图片到达本机的时间';
    if(frame.received_utc&&!Number.isNaN(new Date(frame.received_utc).getTime()))time.dateTime=frame.received_utc;
    quality.textContent=`画面质量：${visionQuality(frame.quality)}`;
    caption.append(time,quality);figure.append(media,caption);gallery.append(figure);
  }
}

async function visionAPI(path,body){
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);
  try{
    const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
    if(response.status===404)throw new Error('视觉观察接口尚未就绪，可稍后重试；聊天仍可继续。');
    let data;
    try{data=await response.json();}catch{throw new Error('视觉服务返回内容暂时无法读取，请重试。');}
    if(!data||typeof data!=='object')throw new Error('视觉服务返回状态不完整，请重试。');
    if(!response.ok)throw new Error(typeof data.error==='string'?data.error:`视觉服务请求失败（${response.status}）`);
    return data;
  }finally{clearTimeout(timeout);}
}

async function refreshVision(){
  if(visionRequest)return visionRequest;
  const requestedAt=performance.now();
  visionRequest=(async()=>{
    try{
      const snapshot=await visionAPI('/api/vision/status');visionStatusError='';
      if(requestedAt>=configSavedAt)visionStatus=snapshot;
      if(requestedAt>=selectionUpdatedAt)updateVisionSelection(snapshot.last_selection,{fromPoll:true});
    }catch(error){visionStatusError=error.name==='AbortError'?'视觉状态读取超时，可重试；聊天仍可继续。':error.name==='TypeError'?'本机视觉服务未连接，请检查本地服务或重试。':error.message;}
    finally{visionRequest=null;paintVisionStatus();}
  })();
  return visionRequest;
}

async function saveVisionEnabled(){
  if(visionSetting)return;
  const enabled=visionElement('visionEnabled').checked;
  visionSetting=true;visionConfigError='';paintVisionStatus();
  try{
    const updated=await visionAPI('/api/vision/config',{enabled});
    configSavedAt=performance.now();
    visionStatus=typeof updated.enabled==='boolean'?updated:{...visionStatus,enabled};
    if(Object.prototype.hasOwnProperty.call(updated,'last_selection'))updateVisionSelection(updated.last_selection);
  }catch(error){visionConfigError=error.name==='AbortError'?'摄像头设置保存超时，请重试。':error.name==='TypeError'?'本机视觉服务未连接，摄像头设置尚未保存。':error.message;}
  finally{visionSetting=false;paintVisionStatus();await refreshVision();}
}

if(visionPanel){
  window.BenchVision={updateSelection:updateVisionSelection,refresh:refreshVision};
  visionElement('visionEnabled').onchange=saveVisionEnabled;
  visionElement('visionRetry').onclick=()=>{visionConfigError='';refreshVision();};
  visionElement('visionExample').onclick=()=>{
    const input=visionElement('aiText');input.value='我手里是什么？';input.dispatchEvent(new Event('input',{bubbles:true}));
    input.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});input.focus({preventScroll:true});
    visionElement('aiChatState').textContent='已填入示例，点击“发送消息”才会请求 AI';
  };
  const poll=async()=>{if(!document.hidden)await refreshVision();visionTimer=setTimeout(poll,2000);};
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshVision();});
  window.addEventListener('pagehide',()=>clearTimeout(visionTimer));
  updateVisionSelection(null);poll();
}
