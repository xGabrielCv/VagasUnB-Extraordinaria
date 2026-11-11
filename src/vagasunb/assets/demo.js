const $ = id => document.getElementById(id);
async function post(path, fields) {
  const body = new FormData(); body.set('csrf', $('csrf').value);
  for (const [key, value] of Object.entries(fields)) body.set(key, value);
  const response = await fetch(path, {method: 'POST', body});
  if (!response.ok) throw new Error('Não foi possível executar. Verifique a sessão e os valores.');
  return response.json();
}
function render(view, target, buttons) {
  target.innerHTML = view.text.replace(/\n/g, '<br>'); buttons.replaceChildren();
  for (const item of view.buttons) {
    const element = document.createElement(item.url ? 'a' : 'button'); element.textContent = item.label;
    if (item.url) { element.href = item.url; element.target = '_blank'; element.rel = 'noopener noreferrer'; }
    else element.onclick = () => interact({callback: item.callback}, target, buttons);
    buttons.append(element);
  }
}
async function interact(fields, target=$('message'), buttons=$('buttons')) {
  try { const view = await post('/demo/interact', {recipient: $('recipient').value, ...fields}); render(view, target, buttons); }
  catch (e) { $('feedback').textContent = e.message; }
}
$('interact').onsubmit = e => { e.preventDefault(); const text=$('text').value; $('text').value=''; interact({text}); };
$('recipient').onchange = () => { $('message').textContent='Envie /start para iniciar.'; $('buttons').replaceChildren(); $('alerts').replaceChildren(); };
$('vacancy').onsubmit = async e => { e.preventDefault(); try { const r=await post('/demo/vacancy', {cid:$('cid').value, available:$('available').value}); $('feedback').textContent=r.status; } catch(e) { $('feedback').textContent=e.message; } };
const seenAlerts = new Set();
async function fetchAlerts() {
  try {
    const result = await post('/demo/alerts', {recipient:$('recipient').value, seen:[...seenAlerts].join(',')});
    for (const view of result.alerts) {
      view.ids.forEach(id => seenAlerts.add(id));
      const a=document.createElement('article'), b=document.createElement('div');
      $('alerts').append(a,b); render(view,a,b);
    }
  } catch(e) { $('feedback').textContent=e.message; }
}
$('recipient').addEventListener('change', () => seenAlerts.clear());
$('dispatch').onclick = async () => {
  try { await post('/demo/dispatch', {}); await fetchAlerts(); $('feedback').textContent='Despacho verificado. Alertas elegíveis aparecem abaixo da conversa.'; }
  catch(e) { $('feedback').textContent=e.message; }
};
setInterval(fetchAlerts, 5000);
