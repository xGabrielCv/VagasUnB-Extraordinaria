const labels=JSON.parse(document.getElementById('presentation').textContent);
const label=code=>labels[code] ?? 'Estado não reconhecido; consulte o diagnóstico';
const number=n=>Number(n).toLocaleString('pt-BR',{maximumFractionDigits:2});
const duration=n=>n==null?'Ainda não medido':`${Math.floor(n/3600)?Math.floor(n/3600)+' h ':''}${Math.floor(n%3600/60)?Math.floor(n%3600/60)+' min ':''}${Math.round(n%60)} s`;
const date=value=>value?new Intl.DateTimeFormat('pt-BR',{timeZone:'America/Sao_Paulo',dateStyle:'short',timeStyle:'medium'}).format(new Date(value)):'Ainda não houve atualização';
function age(value){if(!value)return 'Ainda não houve sinal';const n=(Date.now()-Date.parse(value))/1000;return n< -5?'Relógio inconsistente':`há ${duration(Math.max(0,n))}`;}
let refreshing=false, screenSeconds=3, screenTimer;
async function refreshMetrics(){
  if(document.hidden || refreshing)return;
  refreshing=true;
  const put=(id,value)=>{const el=document.getElementById(id);if(el)el.textContent=value;};
  try{
    const response=await fetch('/metrics');
    if(response.status===401){location.assign('/login');return;}
    if(!response.ok)throw new Error();
    const data=await response.json(),c=data.cycle,w=data.worker;
    if(data.screen_s!==screenSeconds){screenSeconds=data.screen_s;clearInterval(screenTimer);screenTimer=setInterval(refreshMetrics,screenSeconds*1000);}
    const alive=w.at && Date.now()-Date.parse(w.at)<15000;
    put('situation',`${alive?'Serviço respondendo':'Sem sinal recente do serviço'} · ${label(data.source)} · Atualizações: ${label(w.collect)} · ${label(data.gates.collect.reason)} · Telegram ainda não conectado`);
    put('live-coverage',c.expected?`${number(c.valid??0)} / ${number(c.expected)}`:'Aguardando descoberta');
    put('live-status',`${label(c.status)} · ${number(c.empty??0)} consultas vazias · ${number(c.failed??0)} falhas`);
    put('live-interval',`${duration(data.desired_s)} / ${duration(c.effective_s)}`);
    put('live-next',w.collect==='running'?'Atualização em andamento':data.gates.collect.reason!=='ok'?label(data.gates.collect.reason):w.collect==='manual'?'Aguardando atualização manual':`Próxima tentativa em ${duration(w.next_attempt_s)} · Pausa automática: ${duration(w.backoff_s)}`);
    put('live-duration',duration(c.duration_s));
    put('live-network',`${number(c.requests??0)} requisições · ${number(c.bytes??0)} bytes · leitura ${number(c.parse_ms??0)} ms · publicação ${number(c.processing_ms??0)} ms · ${number(c.retries??0)} novas tentativas · ${number(c.changed??0)} turmas alteradas`);
    const pending=(data.outbox.pending??0)+(data.outbox.retry??0);
    put('live-pending',pending?`${number(pending)} avisos`:'Nenhum aviso aguardando envio');
    put('live-worker',`Atualizações: ${label(w.collect)} · Avisos: ${label(w.dispatch)} · Último sinal ${age(w.at)} (${date(w.at)}, Brasília)`);
    put('live-complete',`Última atualização completa: ${date(data.complete.finished)}${data.complete.period?' · período '+data.complete.period:''}.`);
    put('live-levels',`Período ${c.period??'ainda não definido'} · ${c.units??'ainda não medidas'} unidades distintas · Níveis: ${(c.levels??[]).map(v=>v.name).join(', ')||'aguardando descoberta'} · Última publicação: ${date(c.published)}.`);
    const last=data.batches[0];
    put('live-distribution',`Até ${data.sending.pool} envios simultâneos · Limite total: ${data.sending.global_per_s} mensagens/s · Intervalo por conversa: ${duration(data.sending.chat_interval_s)}. ${last?`Último lote: ${last.eligible} avisos · primeiro ${date(last.first)} · último ${date(last.last)} · diferença entre entregas ${duration(last.spread_s)} · espera inicial ${duration(last.first_latency_s)}`:'Nenhum lote observado.'}`);
    document.querySelectorAll('[data-gate]').forEach(el=>{const gate=data.gates[el.dataset.gate];el.textContent=`Agora: ${label(gate.reason)}. ${gate.next?`Próxima ${gate.next_open?'abertura':'parada'}: ${date(gate.next)} (Brasília).${gate.requested?'':' A pausa manual continuará valendo.'}`:'Sem mudança de horário prevista.'}`;});
    put('live-details',JSON.stringify(data,null,2));
  }catch{put('situation','Não foi possível consultar o estado local. Verifique os processos pelos scripts.');}finally{refreshing=false;}
}
document.querySelectorAll('form[action="/operations"]').forEach(form=>{
  if(form.querySelector('[name=action]')?.value!=='config')return;
  form.addEventListener('submit',async e=>{
    e.preventDefault(); const feedback=document.getElementById('save-feedback');
    try{const response=await fetch(form.getAttribute('action'),{method:'POST',body:new FormData(form)});
      if(response.ok){const target=new URL('/?saved=1#settings',location.href).href;if(location.href===target)location.reload();else location.assign(target);return;}
      const data=await response.json();feedback.textContent=data.detail??'Não foi possível salvar. Confira os valores.';feedback.setAttribute('role','alert');feedback.scrollIntoView({block:'center'});
    }catch{feedback.textContent='Conexão indisponível. Seus valores permanecem nos campos.';}
  });
});
document.querySelectorAll('form.schedule').forEach(form=>{
  const preview=document.createElement('p');preview.className='schedule-preview';form.append(preview);
  function update(){const prefix=form.querySelector('select').name.replace('_enabled',''),get=k=>form.elements[prefix+'_'+k].value;
    const days=[...form.querySelectorAll('[type=checkbox]:checked')].map(el=>el.parentElement.textContent.trim()).join(', ')||'todos os dias';
    preview.textContent=get('enabled')==='false'?'Funcionamento manual: a agenda não limita esta atividade.':`Programado para ${days}, ${get('from')&&get('to')?'das '+get('from')+' às '+get('to')+(get('from')>get('to')?' do dia seguinte':''):'durante o dia inteiro'}, no horário de Brasília. ${get('start')?'Com data inicial definida. ':''}${get('end')?'Com data final definida. ':''}A pausa manual prevalece.`;
  }form.addEventListener('input',update);update();
});
screenTimer=setInterval(refreshMetrics,3000);refreshMetrics();document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshMetrics();});
