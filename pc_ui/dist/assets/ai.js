const el=id=>document.getElementById(id);
let history=[];

function message(role,text){
  const item=document.createElement('article');item.className=`ai-message ${role}`;
  const label=document.createElement('strong');label.textContent=role==='user'?'你':'DeepSeek';
  const content=document.createElement('p');content.textContent=text;
  item.append(label,content);el('aiMessages').append(item);return content;
}

async function refreshAI(){
  try{
    const info=await(await fetch('/api/ai/status')).json();
    el('aiModel').textContent=info.configured?`${info.provider} · ${info.model} · 密钥已读取`:'请在 api_key/.env 配置 DeepSeek 密钥';
    el('aiSend').disabled=!info.configured||el('aiForm').dataset.busy==='true';
  }catch{el('aiModel').textContent='本机 AI 服务未连接';}
}

el('aiForm').onsubmit=async event=>{
  event.preventDefault();const text=el('aiText').value.trim();if(!text)return;
  el('aiForm').dataset.busy='true';el('aiSend').disabled=true;
  message('user',text);el('aiText').value='';const output=message('assistant','正在连接…');
  let answer='';el('aiChatState').textContent='正在接收流式回答';
  try{
    const response=await fetch('/api/ai/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,history})});
    if(!response.ok){const problem=await response.json();throw new Error(problem.error||'请求失败');}
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',done=false;
    const receiveEvent=event=>{
      const payload=event.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).replace(/^ /,'')).join('\n');
      if(!payload)return;
      const data=JSON.parse(payload);
      if(Object.prototype.hasOwnProperty.call(data,'vision'))window.BenchVision?.updateSelection(data.vision);
      if(data.error)throw new Error(data.error);
      if(data.delta){answer+=data.delta;output.textContent=answer;}
      if(data.done)done=true;
    };
    try{
      while(true){
        const packet=await reader.read();
        buffer+=packet.done?decoder.decode():decoder.decode(packet.value,{stream:true});
        let boundary;
        while((boundary=/\r?\n\r?\n/.exec(buffer))){
          receiveEvent(buffer.slice(0,boundary.index));buffer=buffer.slice(boundary.index+boundary[0].length);
        }
        if(packet.done){if(buffer.trim())receiveEvent(buffer);break;}
      }
    }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
    if(!done)throw new Error('回答连接已中断');
    history.push({role:'user',content:text},{role:'assistant',content:answer});history=history.slice(-12);
    el('aiChatState').textContent='回答完成';
  }catch(error){
    output.textContent=answer?`${answer}\n\n[连接中断：${error.message}]`:error.message;
    el('aiChatState').textContent='本次回答未完成';
  }finally{el('aiForm').dataset.busy='false';await refreshAI();}
};

refreshAI();setInterval(refreshAI,5000);
