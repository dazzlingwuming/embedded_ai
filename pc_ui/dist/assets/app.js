const $=id=>document.getElementById(id);
const stateNames={queued:'已排队',received:'设备已接收',completed:'读取完成',applied:'已生效',rejected:'已拒绝',expired:'已过期',unknown:'结果未知',recording:'录音中',interrupted:'录音中断'};
const recordingStateNames={completed:'已保存',recording:'录音中',interrupted:'录音中断'};
const methodNames={read_status:'读取状态',identify:'查询身份',read_waveform:'读取波形',set_parameter:'修改模拟参数',set_fault:'设置模拟故障'};
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let status={},socket=null,audioContext=null,monitor=null,gainNode=null,commandBusy=false,lastPacketAt=0,noticeTimer;
let samples=new Float32Array(2048),samplesAt=0,spectrumValues=null,simSamples=null,selectedView='workspace';
const displayScale=document.createElement('label');displayScale.className='wave-scale';displayScale.title='仅改变波形显示，不改变录音或采集增益';displayScale.innerHTML='显示缩放 <select id="waveScale" aria-label="波形显示缩放"><option value="1">×1</option><option value="16">×16</option><option value="64" selected>×64</option><option value="256">×256</option></select>';$('frameInfo').after(displayScale);
$('dropped').title='当前音频连接的缺帧数；每份录音另有独立缺帧记录';

async function api(path,payload){const r=await fetch(path,{method:payload===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw new Error(data.error||`请求失败：${r.status}`);return data;}
function notice(text,error=false){clearTimeout(noticeTimer);$('notice').textContent=text;$('notice').classList.toggle('error',error);$('notice').hidden=false;noticeTimer=setTimeout(()=>$('notice').hidden=true,error?15000:6500);}
function duration(seconds){seconds=Math.floor(seconds||0);return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}

function paintStatus(){
  const connected=!!status.board_connected,audio=status.board?.audio||{},record=status.recording,cameraOnly=status.board?.mode==='camera_only';
  $('connectionDot').classList.toggle('online',connected);
  $('connectionText').textContent=connected?'开发板在线':'开发板离线';
  if(document.activeElement!==$('boardUrl'))$('boardUrl').value=status.board_url||$('boardUrl').value;
  $('boardInfo').textContent=cameraOnly?'ESP32-P4-NANO · 摄像头模式':connected?`ESP32-P4-NANO · 固件 ${status.board.firmware} · 运行 ${duration(status.board.uptime_ms/1000)}`:(status.board_error||'尚未连接');
  if(connected&&status.board.network){const n=status.board.network;$('boardInfo').textContent+=` · Wi-Fi ${n.ssid||'未知'}${n.rssi_dbm!==undefined?' · '+n.rssi_dbm+' dBm':''}`;}
  $('sampleRate').textContent=audio.ready?`${audio.sample_rate/1000} kHz`:'—';
  $('sampleFormat').textContent=audio.ready?`${audio.bits_per_sample}bit / 单声道`:'—';
  $('gain').textContent=audio.ready?`${audio.gain_db} dB`:'—';
  $('alc').textContent=audio.ready?(audio.alc_register===0?'已关闭':'待核对'):'—';
  $('boardSpeakerState').textContent=status.board?.speaker?.amplifier_enabled?'正在播报':'保持静音';
  $('listenButton').disabled=!connected||!audio.ready;
  $('recordButton').disabled=!connected||!audio.ready||!!record||!!status.voice?.active;
  $('stopRecordButton').disabled=!record;
  $('rms').innerHTML=`${status.audio?.connected?status.audio.rms_dbfs:'—'}<small> dBFS</small>`;
  $('peak').innerHTML=`${status.audio?.connected?status.audio.peak_dbfs:'—'}<small> dBFS</small>`;
  $('dropped').textContent=status.audio?.missing_frames??'—';
  $('sequence').textContent=status.audio?.connected?status.audio.sequence:'—';
  $('frameInfo').textContent=status.audio?.connected?'48,000 S/s / PCM16 / 20 ms':'PCM / 等待数据';
  $('audioState').textContent=status.audio?.connected?(socket?'正在接收真实音频':'录音采集中'):(socket?'正在等待音频数据':'尚未启动监听');
  $('recordDot').classList.toggle('recording',!!record);
  $('recordState').textContent=record?'正在录音 · WAV':'未录音';
  $('recordDuration').textContent=duration((record?.samples_written||0)/48000);
  $('startSimButton').disabled=!connected||cameraOnly||!!status.simulator?.running;
  $('stopSimButton').disabled=!status.simulator?.running;
  const devices=status.board?.devices||[];
  $('devices').innerHTML=devices.length?devices.map(d=>`<tr><td><strong>${escape(d.name||d.device_id)}</strong><small>${escape(d.device_id)}</small></td><td><span class="source simulated">网络模拟</span></td><td class="${d.connected?'status-online':'status-offline'}">${d.connected?'在线':'离线'} · ${(d.last_seen_age_ms/1000).toFixed(1)}s</td></tr>`).join(''):'<tr><td colspan="3" class="empty">启动模拟器，或从笔记本运行网络模拟器。</td></tr>';
  const prior=$('deviceSelect').value;
  const identity=devices.map(d=>`${d.device_id}:${d.connected}`).join('|');
  if($('deviceSelect').dataset.identity!==identity){$('deviceSelect').innerHTML=devices.length?devices.map(d=>`<option value="${escape(d.device_id)}">${escape(d.device_id)} · ${d.connected?'在线':'离线'}</option>`).join(''):'<option value="">暂无设备</option>';$('deviceSelect').dataset.identity=identity;if(devices.some(d=>d.device_id===prior))$('deviceSelect').value=prior;}
  $('sendCommandButton').disabled=!connected||!devices.length||commandBusy;
  $('faultButton').disabled=!connected||!devices.length||commandBusy;
  $('commands').innerHTML=status.commands?.length?status.commands.slice(0,7).map(c=>`<tr><td class="mono" title="${escape(c.request_id)}">${escape(c.request_id?.slice(0,10))}</td><td class="mono">${escape(c.device_id)}</td><td>${escape(methodNames[c.method||c.request?.method]||c.request?.method||'—')}</td><td><span class="state ${escape(c.state)}">${escape(stateNames[c.state]||c.state)}</span></td><td>${escape(c.reason||c.result?.error||c.result?.model||'—')}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">尚未发送测试命令</td></tr>';
  $('cameraModeHint').textContent=cameraOnly?'当前使用摄像头程序，声音和仪器功能暂未启用。':'';
  $('footerStatus').textContent=cameraOnly?'真实摄像头 · 声音和仪器功能暂未启用':connected?`${status.board.video_enabled?'真实音视频':'真实音频'} / 模拟仪器 · ${status.board.physical_instruments_verified?'':'未接入实体仪器'}`:'本地存储 · 等待开发板连接';
}
async function refresh(){try{status=await api('/api/status');paintStatus();}catch(error){$('connectionText').textContent='本机服务未连接';$('connectionDot').classList.remove('online');}}

async function startListening(){
  if(socket){socket.close();socket=null;await audioContext?.close();audioContext=monitor=gainNode=null;$('listenButton').textContent='开始监听';return;}
  audioContext=new AudioContext({sampleRate:48000,latencyHint:'interactive'});
  await audioContext.audioWorklet.addModule('/assets/audio-worklet.js');
  monitor=new AudioWorkletNode(audioContext,'pcm-monitor');gainNode=audioContext.createGain();gainNode.gain.value=$('mute').checked?0:0.35;monitor.connect(gainNode).connect(audioContext.destination);
  await audioContext.resume();
  const currentSocket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/audio`);socket=currentSocket;currentSocket.binaryType='arraybuffer';
  currentSocket.onmessage=({data})=>{if(typeof data==='string')return;const view=new DataView(data);if(view.byteLength<24)return;const count=view.getUint16(20,true);if(view.byteLength!==24+count*2)return;const values=new Float32Array(count);for(let i=0;i<count;i++){const value=view.getInt16(24+i*2,true)/32768;values[i]=value;samples[samplesAt]=value;samplesAt=(samplesAt+1)%samples.length;}lastPacketAt=performance.now();monitor?.port.postMessage(values,[values.buffer]);};
  currentSocket.onclose=()=>{if(socket===currentSocket){socket=null;$('listenButton').textContent='开始监听';audioContext?.close();audioContext=monitor=gainNode=null;}};
  currentSocket.onerror=()=>notice('音频连接暂时中断，请检查开发板状态。',true);
  $('listenButton').textContent='停止监听';
}
$('listenButton').onclick=()=>startListening().catch(error=>notice(error.message,true));
$('mute').onchange=()=>{if(gainNode)gainNode.gain.setTargetAtTime($('mute').checked?0:0.35,audioContext.currentTime,0.02);};
$('recordButton').onclick=async()=>{try{$('recordButton').disabled=true;await api('/api/recordings/start',{label:$('recordLabel').value||'现场声音记录'});notice('已开始保存真实麦克风录音。');await refresh();}catch(e){notice(e.message,true);paintStatus();}};
$('stopRecordButton').onclick=async()=>{try{const result=await api('/api/recordings/stop',{});notice(`录音已保存：${duration(result.duration_seconds)}，可在“测试记录”中回放。`);await refresh();}catch(e){notice(e.message,true);}};
$('connectionForm').onsubmit=async event=>{event.preventDefault();try{await api('/api/config',{board_url:$('boardUrl').value});notice('开发板地址已保存，正在连接。');await refresh();}catch(e){notice(e.message,true);}};
$('startSimButton').onclick=async()=>{try{$('startSimButton').disabled=true;await api('/api/simulators/start',{count:4});notice('已启动四台网络模拟仪器，数据不代表实体仪器。');await new Promise(resolve=>setTimeout(resolve,700));await refresh();}catch(e){notice(e.message,true);}};
$('stopSimButton').onclick=async()=>{try{await api('/api/simulators/stop',{});notice('模拟器已停止；开发板将在心跳过期后显示离线。');await refresh();}catch(e){notice(e.message,true);}};
$('methodSelect').onchange=()=>$('parameterFields').hidden=$('methodSelect').value!=='set_parameter';
async function sendCommand(method,params){
  commandBusy=true;paintStatus();$('commandFeedback').textContent='命令已提交，等待模拟仪器响应…';
  try{const result=await api('/api/commands',{device_id:$('deviceSelect').value,method,params,ttl_ms:3000});$('commandFeedback').textContent=`${stateNames[result.state]||result.state}${result.reason?' · '+result.reason:''} · ${result.request_id.slice(0,10)}`;
    if(result.result?.samples){simSamples=result.result.samples;$('simWaveInfo').textContent=`${result.result.sample_rate.toLocaleString()} S/s · V（模拟） · 纵轴 ±8 V`;}
    if(result.state==='unknown')notice('结果未知：设备可能已执行。请先读取状态核对，不要盲目重发修改命令。',true);
    else if(result.state==='rejected')notice(`模拟仪器拒绝：${result.result?.error||result.reason||'未执行'}`,true);
    else if(method==='set_parameter'&&result.state==='applied'){notice(`模拟参数已生效：${result.result.name} = ${result.result.value}`);}
  }catch(e){notice(e.message,true);$('commandFeedback').textContent=e.message;}finally{commandBusy=false;await refresh();}
}
$('commandForm').onsubmit=event=>{event.preventDefault();const method=$('methodSelect').value;sendCommand(method,method==='set_parameter'?{name:$('parameterName').value,value:Number($('parameterValue').value)}:{});};
$('faultButton').onclick=()=>sendCommand('set_fault',{mode:$('faultMode').value});

async function loadRecords(){try{const {recordings}=await api('/api/recordings');$('recordList').innerHTML=recordings.length?recordings.map(r=>`<article class="record-item"><div class="record-item-header"><div><h3>${escape(r.label)} <span class="source real">真实麦克风</span></h3><p>${escape(r.session_id)} · ${r.sample_rate/1000}kHz / ${r.bits_per_sample}bit / 单声道 · 缺帧 ${r.missing_frames||0}</p></div><div><span class="state ${escape(r.status)}">${escape(recordingStateNames[r.status]||r.status)}</span> <span class="mono">${duration(r.duration_seconds||r.samples_written/r.sample_rate)}</span></div></div>${r.status==='recording'?'<p>录音进行中，停止后可回放和下载。</p>':`<audio controls preload="none" aria-label="回放 ${escape(r.label)}" src="/api/recordings/${r.session_id}/audio.wav"></audio><div class="record-links"><a href="/api/recordings/${r.session_id}/audio.wav?download=1">下载 WAV</a><a href="/api/recordings/${r.session_id}/metadata.json?download=1">采集参数与中断记录</a><a href="/api/recordings/${r.session_id}/events.jsonl?download=1">操作日志</a></div>`}</article>`).join(''):'<div class="empty">尚无录音。在采集工作台点击“开始录音”，即可保存真实声音。</div>';}catch(e){notice(e.message,true);}}
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>{selectedView=button.dataset.view;document.querySelectorAll('[data-view]').forEach(item=>{item.classList.toggle('active',item===button);item.setAttribute('aria-current',item===button?'page':'false');});$('workspace').hidden=selectedView!=='workspace';$('records').hidden=selectedView!=='records';$('aiView').hidden=selectedView!=='ai';if(selectedView==='records')loadRecords();window.dispatchEvent(new CustomEvent('benchlink:view',{detail:selectedView}));});
$('refreshRecords').onclick=loadRecords;

function canvasContext(id){const canvas=$(id),dpr=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}const c=canvas.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,w,h);return[c,w,h];}
function plotGrid(c,w,h,dark){c.strokeStyle=dark?'#24434b':'#e4ddcf';c.lineWidth=1;for(let n=1;n<5;n++){c.beginPath();c.moveTo(0,h*n/5);c.lineTo(w,h*n/5);c.stroke();}for(let n=1;n<8;n++){c.beginPath();c.moveTo(w*n/8,0);c.lineTo(w*n/8,h);c.stroke();}}
function emptyPlot(c,w,h,text,dark){c.fillStyle=dark?'#9cb9c0':'#88734f';c.font='11px "Segoe UI","Microsoft YaHei",sans-serif';c.textAlign='center';c.fillText(text,w/2,h/2);}
function spectrum(){const N=512,bins=128,result=new Float32Array(bins);for(let k=0;k<bins;k++){let re=0,im=0;for(let n=0;n<N;n++){const value=samples[(samplesAt+samples.length-N+n)%samples.length]*(0.5-0.5*Math.cos(2*Math.PI*n/(N-1)));const angle=2*Math.PI*(k*2)*n/N;re+=value*Math.cos(angle);im-=value*Math.sin(angle);}result[k]=20*Math.log10(Math.max(1e-6,Math.hypot(re,im)/(N/4)));}return result;}
let lastSpectrum=0,lastDraw=0;
function draw(timestamp){requestAnimationFrame(draw);if(document.hidden||selectedView!=='workspace'||timestamp-lastDraw<40)return;lastDraw=timestamp;const active=!!socket&&performance.now()-lastPacketAt<2000;
  {const[c,w,h]=canvasContext('audioWave');plotGrid(c,w,h,true);if(active){c.strokeStyle='#5ed2c0';c.lineWidth=1.1;c.beginPath();for(let n=0;n<1024;n++){const value=Math.max(-1,Math.min(1,samples[(samplesAt+samples.length-1024+n)%samples.length]*Number($('waveScale').value))),x=n/(1023)*w,y=h/2-value*(h*.44);n?c.lineTo(x,y):c.moveTo(x,y);}c.stroke();}else emptyPlot(c,w,h,'启动监听后，显示真实麦克风波形',true);}
  {const[c,w,h]=canvasContext('spectrum');plotGrid(c,w,h,true);if(active){if(timestamp-lastSpectrum>200){spectrumValues=spectrum();lastSpectrum=timestamp;}if(spectrumValues){c.fillStyle='#449e99';for(let n=0;n<spectrumValues.length;n++){const height=Math.max(0,Math.min(1,(spectrumValues[n]+96)/96))*(h-24);c.fillRect(n*w/spectrumValues.length,h-21-height,Math.max(1,w/spectrumValues.length-1),height);}}}else emptyPlot(c,w,h,'频谱随真实音频更新',true);c.font='9px Consolas';c.fillStyle='#9cb9c0';c.textAlign='left';['0','6k','12k','18k','24k Hz'].forEach((label,n)=>c.fillText(label,n*(w-42)/4+6,h-6));}
  {const[c,w,h]=canvasContext('simWave');plotGrid(c,w,h,false);if(simSamples){const extent=8;c.strokeStyle='#a57732';c.lineWidth=1.6;c.beginPath();simSamples.forEach((value,n)=>{const x=n/(simSamples.length-1)*w,y=h/2-value/extent*h*.4;n?c.lineTo(x,y):c.moveTo(x,y);});c.stroke();}else emptyPlot(c,w,h,'发送“读取模拟波形”后显示',false);}
}
refresh();setInterval(refresh,1000);requestAnimationFrame(draw);
