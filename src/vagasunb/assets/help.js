/* Text-only help: no network, focus transfer or form submission. */
(() => {
  // Keep the help button beside the label, outside the label's control subtree.
  document.querySelectorAll('label').forEach((label,index)=>{
    const button=label.querySelector('[data-help]'), control=label.querySelector('input,select,textarea');
    if(!button || !control)return;
    const field=document.createElement('div'), header=document.createElement('div');
    field.className='field';header.className='field-head';
    control.id ||= `field-${index}`;label.htmlFor=control.id;
    button.dataset.field=control.id;
    label.before(field);field.append(header,control);header.append(label,button);
  });
  let current=null, pinned=false, opening, closing;
  const tips=new Map();
  function position() {
    if(!current) return;
    const tip=tips.get(current), r=current.getBoundingClientRect();
    const width=document.documentElement.clientWidth, height=innerHeight;
    const scale=tip.offsetWidth?tip.getBoundingClientRect().width/tip.offsetWidth:1;
    tip.style.maxWidth=`${Math.min(340,(width-24)/scale)}px`;
    tip.style.maxHeight=`${(height-24)/scale}px`;
    const t=tip.getBoundingClientRect();
    tip.style.left=`${Math.max(12,Math.min(r.left,width-t.width-12))/scale}px`;
    tip.style.top=`${Math.max(12,Math.min(r.bottom+6,height-t.height-12))/scale}px`;
  }
  function close() {
    clearTimeout(opening); clearTimeout(closing);
    if(current) { const tip=tips.get(current); if(tip.hidePopover && tip.matches(':popover-open'))tip.hidePopover(); tip.hidden=true; }
    current=null;pinned=false;
  }
  function open(button,pin=false) {
    if(current && current!==button && pinned && !pin)return;
    clearTimeout(closing);clearTimeout(opening);
    if(current!==button) {close();current=button;}
    pinned=pin || pinned;
    const tip=tips.get(button);tip.hidden=false;
    if(tip.showPopover && !tip.matches(':popover-open')) tip.showPopover();
    position();
  }
  function leave() {
    clearTimeout(opening);clearTimeout(closing);
    closing=setTimeout(()=>{
      if(!current || pinned) return;
      const tip=tips.get(current);
      if(!current.matches(':hover,:focus') && !tip.matches(':hover'))close();
    },300);
  }
  document.querySelectorAll('[data-help]').forEach((button,index)=>{
    button.type='button';
    const tip=document.createElement('div');
    tip.id=`help-${index}`;tip.className='help-tip';tip.role='tooltip';
    tip.textContent=button.dataset.help;tip.hidden=true;
    if('showPopover' in tip)tip.setAttribute('popover','manual');
    document.body.append(tip);tips.set(button,tip);
    button.setAttribute('aria-describedby',tip.id);
    if(button.dataset.field)document.getElementById(button.dataset.field).setAttribute('aria-describedby',tip.id);
    button.addEventListener('pointerenter',()=>{opening=setTimeout(()=>open(button),250);});
    button.addEventListener('pointerleave',leave);
    button.addEventListener('focus',()=>open(button));
    button.addEventListener('blur',leave);
    button.addEventListener('click',e=>{e.preventDefault();if(current===button && pinned)close();else open(button,true);});
    tip.addEventListener('pointerenter',()=>clearTimeout(closing));tip.addEventListener('pointerleave',leave);
  });
  document.addEventListener('pointerdown',e=>{if(current && !current.contains(e.target) && !tips.get(current).contains(e.target))close();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
  addEventListener('resize',position);addEventListener('scroll',position,true);
  visualViewport?.addEventListener('resize',position);visualViewport?.addEventListener('scroll',position);
  new MutationObserver(position).observe(document.body,{attributes:true,attributeFilter:['style','class']});
})();
