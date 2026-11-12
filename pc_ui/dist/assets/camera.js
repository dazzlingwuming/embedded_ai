const el=id=>document.getElementById(id);
let socket=null,ready=false,attemptedAutoStart=false,objectURL=null,received=0,started=0,lastFrame=0,lastError='',saving=false,settingQuality=false;
let decoding=false,pendingFrame=null,renderTimes=[];
const image=el('cameraImage');
function overlay(text){el('cameraOverlayText').textContent=text;el('cameraOverlay').hidden=false;}
function buttons(){el('cameraToggle').disabled=!ready&&!socket;el('cameraToggle').textContent=socket?'暂停预览':'开始预览';el('cameraSave').disabled=saving||!socket||!lastFrame||performance.now()-lastFrame>4000;el('cameraFullscreen').disabled=!socket||!lastFrame;}
function stop(reason='预览已暂停'){
  const old=socket;socket=null;old?.close();image.hidden=true;image.removeAttribute('src');
  image.onload=image.onerror=null;decoding=false;pendingFrame=null;renderTimes=[];
  if(objectURL){URL.revokeObjectURL(objectURL);objectURL=null;}
  lastFrame=0;el('cameraLiveBadge').hidden=true;el('cameraState').textContent=reason;overlay(reason);buttons();
}
function start(){
  if(socket||!ready)return;
  lastError='';received=0;started=performance.now();lastFrame=0;
  decoding=false;pendingFrame=null;renderTimes=[];
  el('cameraState').textContent='正在连接';overlay('正在获取摄像头画面…');
  const current=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/camera`);
  socket=current;current.binaryType='blob';buttons();
  current.onmessage=({data})=>{
    if(socket!==current)return;
    if(typeof data==='string'){const event=JSON.parse(data);if(event.error){lastError=event.error;stop('视频连接中断');el('cameraHint').textContent=lastError;}return;}
    pendingFrame=data;
    renderFrame(current);
  };
  current.onerror=()=>{lastError='视频连接暂时不可用，请检查开发板后重新开始预览。';};
  current.onclose=()=>{if(socket!==current)return;stop('视频连接已断开');el('cameraHint').textContent=lastError||'视频连接已结束，点击“开始预览”重试。';};
}
function renderFrame(current){
  if(socket!==current||decoding||!pendingFrame)return;
  const data=pendingFrame;pendingFrame=null;decoding=true;
  const oldURL=objectURL;objectURL=URL.createObjectURL(new Blob([data],{type:'image/jpeg'}));
  image.onload=()=>requestAnimationFrame(()=>{
    if(socket!==current)return;
    el('cameraResolution').textContent=`${image.naturalWidth} × ${image.naturalHeight}`;
    image.hidden=false;el('cameraOverlay').hidden=true;
    received++;lastFrame=performance.now();renderTimes.push(lastFrame);renderTimes=renderTimes.filter(t=>lastFrame-t<5000);
    el('cameraFrames').textContent=received;el('cameraState').textContent=lastFrame-started<2500?'曝光稳定中':'正在预览';
    el('cameraLiveBadge').hidden=false;decoding=false;buttons();renderFrame(current);
  });
  image.onerror=()=>{if(socket===current){lastError='视频图像解码失败，请重新开始预览。';stop('画面解码失败');}};
  image.src=objectURL;if(oldURL)URL.revokeObjectURL(oldURL);
}
async function refresh(){
  try{
    const response=await fetch('/api/camera/status');if(!response.ok)throw new Error();const data=await response.json();
    ready=data.ready;el('cameraConnection').textContent=ready?'摄像头已连接':'摄像头未就绪';
    el('cameraQuality').disabled=!ready||settingQuality;
    if(!settingQuality&&data.quality&&document.activeElement!==el('cameraQuality'))el('cameraQuality').value=String(data.quality);
    el('cameraFPS').textContent=socket&&data.connected?`${data.fps.toFixed(1)} 帧/秒`:'—';
    el('cameraP95').textContent=socket&&data.interval_p95_ms!==null?`${data.interval_p95_ms} ms`:'—';
    el('cameraMaxGap').textContent=socket&&data.max_interval_ms!==null?`${data.max_interval_ms} ms`:'—';
    el('cameraLowFPS').textContent=socket&&data.low_1pct_fps!==null?`${data.low_1pct_fps} 帧/秒`:'样本不足';
    const renderFPS=renderTimes.filter(t=>performance.now()-t<5000).length/Math.min(5,Math.max(.001,(performance.now()-started)/1000));
    el('cameraRenderFPS').textContent=socket?`${renderFPS.toFixed(1)} 帧/秒`:'—';
    if(!socket&&!ready){el('cameraState').textContent='等待摄像头';overlay('等待摄像头连接');}
    if(socket&&lastFrame&&performance.now()-lastFrame>4000){el('cameraState').textContent='等待新画面';el('cameraLiveBadge').hidden=true;overlay('画面暂时停止更新');}
    buttons();
    if(ready&&!attemptedAutoStart&&!document.hidden&&!el('workspace').hidden){attemptedAutoStart=true;start();}
  }catch{ready=false;el('cameraConnection').textContent='本机视频服务未连接';buttons();}
}
el('cameraToggle').onclick=()=>{attemptedAutoStart=true;if(socket)stop();else start();};
el('cameraSave').onclick=async()=>{
  saving=true;buttons();
  try{
    const response=await fetch('/api/camera/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'照片保存失败');
    const result=el('cameraPhotoResult');result.replaceChildren(document.createTextNode('照片已保存在本项目：'));
    const link=document.createElement('a');link.href=data.url;link.target='_blank';link.rel='noopener';link.textContent=data.name;result.append(link);result.hidden=false;
  }catch(error){el('cameraPhotoResult').textContent=error.message;el('cameraPhotoResult').hidden=false;}
  finally{saving=false;buttons();}
};
el('cameraFullscreen').onclick=async()=>{try{await el('cameraFrame').requestFullscreen();}catch{el('cameraHint').textContent='浏览器未允许全屏，可直接在当前窗口查看。';}};
el('cameraQuality').onchange=async()=>{
  settingQuality=true;el('cameraQuality').disabled=true;
  try{
    const response=await fetch('/api/camera/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({quality:Number(el('cameraQuality').value)})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'画质设置失败');
    el('cameraHint').textContent='画质设置已生效。流畅档减少传输量；实际帧率仍取决于开发板和网络。';
  }catch(error){el('cameraHint').textContent=error.message;}
  finally{settingQuality=false;refresh();}
};
window.addEventListener('benchlink:view',event=>{if(event.detail!=='workspace'&&socket)stop('切换页面，预览已暂停');});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&socket)stop('页面隐藏，预览已暂停');});
window.addEventListener('pagehide',()=>{socket?.close();});
refresh();setInterval(refresh,1000);
