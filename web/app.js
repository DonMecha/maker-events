'use strict';
// 外部コンテンツはtextContentで描画する。外部HTML・画像・コードを同一originで実行しない。
const $ = s => document.querySelector(s);
let events=[], statuses=[], languages=[], overview={}, view='discover', selected=null, editing=null, busy=false;
let currentRoute, navigationIndex=0, navigationChain='', undoingNavigation=false;
const labels={title:'イベント名',url:'公式URL',start_at:'開始',end_at:'終了',deadline_at:'申込締切',venue:'会場',address:'住所',description:'説明',categories:'分野',region:'地域',online:'オンライン',cancelled:'中止',cancel_evidence:'中止根拠',language:'言語',language_evidence:'言語の根拠',status:'参加状態',note:'メモ',travel_minutes:'移動時間',language_evidence:'言語の確認根拠'};
const dates=new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',month:'numeric',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit'});
labels.hidden='興味なし';
Object.assign(labels,{organizer_name:'主催者・掲載者名',organizer_url:'主催者の確認先',organizer_evidence:'主催者の確認根拠',decision:'主催者の表示判断',name:'確認した主催者名',evidence:'主催者の確認根拠'});
const organizerDecision=e=>e.organizer_review?.effective_decision||'pending';
const statusLabel=s=>s==='見送り'?'不参加':s;
const organizerLabel=e=>({allowed:'主催者確認済み',pending:'主催者要確認',hidden:'非表示に指定'})[organizerDecision(e)];
const fmt=value=>value?dates.format(new Date(value)):'未確認';
function element(tag, text, className) {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
function badge(text,kind=''){return element('span',text,'badge '+kind);}
function button(text, action, cls=''){const b=element('button',text,cls);b.type='button';b.addEventListener('click',action);return b;}
function showMessage(text,error=false){$('#message').textContent=text;$('#message').classList.toggle('error',error);}
async function request(path, payload){
  const opts=payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)};
  const response=await fetch(path,{...opts,cache:'no-store'});
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error||'操作できませんでした');error.status=response.status;throw error;}
  return data;
}
async function load(){
  $('#reload').disabled=true;
  try{
    const data=await request('/api/events');events=data.events;statuses=data.statuses;languages=data.languages;overview=data.overview;
    const chosen=currentRoute.status;
    $('#state-filter').replaceChildren(new Option('通常表示（不参加を除く）','active'),new Option('すべて','all'),...statuses.map(s=>new Option(statusLabel(s),s)));$('#state-filter').value=chosen||'active';
    $('#updated').textContent='保存情報を表示：'+fmt(data.server_time)+' · 日本時間';
    renderRoute(currentRoute,history.state?.scrollY||0);
    showMessage('');
  }catch(e){showMessage('接続できません。通信状況とPCの稼働を確認してください。 '+e.message,true);}
  finally{$('#reload').disabled=false;}
}
function filtered(){
  return events.filter(e=>{
    if(!dayMatches(e))return false;
    const visibility=$('#visibility-filter').value;
    const status=$('#state-filter').value;
    if(status==='active' && visibility!=='hidden' && e.state.status==='見送り')return false;
    if(view==='discover' && status!=='見送り' && !workMatches(e))return false;
    if(visibility!=='all' && Boolean(e.visibility?.hidden)!==(visibility==='hidden'))return false;
    const organizer=$('#organizer-filter').value;
    if(view==='discover' && status!=='見送り' && visibility!=='hidden' && organizer!=='all' && organizerDecision(e)!==organizer)return false;
    if(view==='mine' && !e.state.status)return false;
    if($('#period').value==='future' && e.attendance.code==='past')return false;
    const l=$('#language-filter').value;
    if(l==='ja' && !['日本語','日本語対応あり'].includes(e.effective_language))return false;
    if(l==='check' && e.effective_language!=='要確認')return false;
    return ['active','all'].includes(status) || e.state.status===status;
  });
}
function openDetail(id){navigate({...currentRoute,selected:id});}
function dayMatches(event){return $('#day-filter').value!=='off'||event.day_off===true;}
function workMatches(event){return $('#visibility-filter').value==='hidden' || $('#schedule-filter').value==='all' || !event.attendance.work_conflict;}
function officialLink(event){const link=element('a','公式サイトで確認・申込み ↗','button primary');link.href=event.url;link.target='_blank';link.rel='noopener noreferrer';return link;}
function eventCard(event){
  const c=element('article',undefined,'card');
  const top=element('div',undefined,'row');top.append(element('span',fmt(event.start_at),'tabular'));
  if(event.version>1)top.append(badge('情報更新あり','check'));c.append(top,element('h3',event.title,'event-title'));
  c.append(element('p',(event.venue||'会場未確認')+' · '+event.region,'small muted'));
  c.append(element('p','主催者・掲載者：'+(event.organizer_review?.name||event.organizer_name||'未確認'),'small muted'));
  if(event.categories.length)c.append(element('p',event.categories.join(' / '),'small muted'));
  const badges=element('div',undefined,'badges');badges.append(badge(event.attendance.label,event.attendance.code==='possible'?'good':'check'));
  badges.append(badge('日本語：'+event.effective_language,event.effective_language==='要確認'?'check':''));
  badges.append(badge(organizerLabel(event),organizerDecision(event)==='allowed'?'good':'check'));
  if(event.visibility?.hidden)badges.append(badge('興味なし','check'));
  if(event.cancelled)badges.append(badge('公式に中止','cancelled'));
  if(event.duplicates.length)badges.append(badge('重複候補あり','check'));
  c.append(badges);
  if(event.deadline_at)c.append(element('p','申込締切 '+fmt(event.deadline_at),'small muted'));
  if(event.state.status)c.append(element('p','あなたの状態：'+statusLabel(event.state.status),'small muted'));
  const acts=element('div',undefined,'actions');
  acts.append(button(event.state.status?'✓ '+statusLabel(event.state.status):'☆ 気になる',()=>event.state.status?openDetail(event.id):setStatus(event,'気になる'),event.state.status?'starred':''),button('詳細を見る →',()=>openDetail(event.id),'quiet'));
  if(event.state.status!=='見送り'){const decline=button('不参加',()=>recordDecline(event),'quiet');decline.dataset.declineAction='';decline.disabled=busy;acts.append(decline);}
  const hide=button(event.visibility?.hidden?'表示に戻す':'興味なし',()=>setVisibility(event,!event.visibility?.hidden),'quiet');hide.dataset.visibilityAction='';hide.disabled=busy;acts.append(hide);c.append(acts);return c;
}
function renderList(){
  $('#list-screen').hidden=false;$('#detail-screen').hidden=true;
  $('#filters').hidden=!['discover','mine'].includes(view);$('#add').hidden=view==='sources'||view==='changes';
  $('#organizer-filter').disabled=view!=='discover'||$('#state-filter').value==='見送り';
  $('#schedule-filter').disabled=view!=='discover'||$('#visibility-filter').value==='hidden'||$('#state-filter').value==='見送り';
  if($('#visibility-filter').value==='hidden')$('#organizer-filter').disabled=true;
  const counts=events.filter(e=>dayMatches(e) && ($('#state-filter').value!=='active'||$('#visibility-filter').value==='hidden'||e.state.status!=='見送り') && workMatches(e) && ($('#period').value!=='future'||e.attendance.code!=='past') && ($('#visibility-filter').value==='all'||Boolean(e.visibility?.hidden)===($('#visibility-filter').value==='hidden')));
  $('#organizer-summary').hidden=view!=='discover'||$('#state-filter').value==='見送り';
  $('#organizer-summary').textContent='主催者確認済み '+counts.filter(e=>organizerDecision(e)==='allowed').length+'件 ／ 要確認 '+counts.filter(e=>organizerDecision(e)==='pending').length+'件 ／ 非表示 '+counts.filter(e=>organizerDecision(e)==='hidden').length+'件';
  document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===view)));
  const cards=$('#cards');cards.replaceChildren();
  $('#heading').textContent={discover:'これからのイベント',mine:'自分のイベント',changes:'公式情報の変更',sources:'取得元と保存'}[view];
  if(view==='sources'){renderSources(cards);return;}
  if(view==='changes'){
    const changed=events.filter(e=>e.version>1 && !e.visibility?.hidden);
    changed.forEach(e=>{const c=eventCard(e);c.append(element('p','最新変更 '+fmt(e.updated_at),'small muted'));cards.append(c);});
    if(!changed.length)cards.append(element('p','公式情報の変更はまだありません。変更前後はイベント詳細の履歴で確認できます。','empty'));return;
  }
  const list=filtered();list.forEach(e=>cards.append(eventCard(e)));
  if(!list.length){
    const empty=element('div',undefined,'empty');empty.append(element('p',view==='mine'?'イベントの状態を記録するとここに並びます。':'条件に合うイベントがありません。'));
    if(view==='discover' && $('#visibility-filter').value!=='hidden' && $('#organizer-filter').value==='allowed')empty.append(button('主催者要確認の候補を見る',()=>navigate({...currentRoute,organizer:'pending'})));
    cards.append(empty);
  }
}
function section(title){const s=element('section',undefined,'section');s.append(element('h3',title));return s;}
function field(label, tag, id, value){const l=element('label',label);const control=element(tag);control.id=id;control.value=value??'';l.append(control);return l;}
function hasUnsavedInput(){
  if($('#event-dialog').open)return true;
  if([...document.querySelectorAll('#organizer-review input,#organizer-review textarea')].some(e=>e.value!==e.dataset.initial))return true;
  if(!selected || !$('#note'))return false;
  const state=events.find(e=>e.id===selected)?.state;if(!state)return false;
  const dirty=$('#note').value!==state.note || $('#travel').value!==String(state.travel_minutes??'') || $('#personal-language').value!==state.language || $('#personal-evidence').value!==state.language_evidence;
  return dirty;
}
function canLeaveDetail(){return !hasUnsavedInput() || confirm('未保存の入力があります。破棄して移動しますか？');}
// URLは画面と絞り込みだけ。本人のメモ・認証情報は履歴へ保存しない。
function readRoute(){
  const p=new URLSearchParams(location.hash.slice(1));
  return {view:['discover','mine','changes','sources'].includes(p.get('view'))?p.get('view'):'discover',
    selected:/^[a-f0-9]{24}$/.test(p.get('event')||'')?p.get('event'):null,
    period:p.get('period')==='all'?'all':'future',
    language:['ja','check'].includes(p.get('language'))?p.get('language'):'all',status:p.get('status')||'active',
    organizer:['pending','hidden','all'].includes(p.get('organizer'))?p.get('organizer'):'allowed',
    visibility:['hidden','all'].includes(p.get('visibility'))?p.get('visibility'):'visible',schedule:p.get('schedule')==='all'?'all':'exclude',days:p.get('days')==='off'?'off':'all'};
}
function routeURL(route){
  const p=new URLSearchParams({view:route.view});
  if(route.selected)p.set('event',route.selected);
  for(const key of ['period','language','status','organizer','visibility','schedule','days'])if(route[key]!==({period:'future',language:'all',status:'active',organizer:'allowed',visibility:'visible',schedule:'exclude',days:'all'})[key])p.set(key,route[key]);
  return '#'+p.toString();
}
function rememberScroll(){
  if(history.state?.makerEvents)history.replaceState({...history.state,scrollY:window.scrollY},'');
}
function renderRoute(route,scrollY=0){
  view=route.view;selected=events.some(e=>e.id===route.selected)?route.selected:null;
  $('#period').value=route.period;$('#language-filter').value=route.language;
  $('#organizer-filter').value=route.organizer||'allowed';
  $('#visibility-filter').value=route.visibility||'visible';
  $('#schedule-filter').value=route.schedule||'exclude';
  $('#day-filter').value=route.days||'all';
  $('#state-filter').value=[...statuses,'active','all'].includes(route.status)?route.status:'active';
  currentRoute={...route,selected,status:$('#state-filter').value,organizer:$('#organizer-filter').value,visibility:$('#visibility-filter').value,schedule:$('#schedule-filter').value,days:$('#day-filter').value};
  if($('#event-dialog').open)$('#event-dialog').close();
  if(selected)renderDetail();else renderList();
  requestAnimationFrame(()=>window.scrollTo(0,scrollY));
}
function navigate(route,replace=false){
  if(busy){showMessage('保存が終わってから移動してください。');return;}
  if(!canLeaveDetail())return;
  if(JSON.stringify(route)===JSON.stringify(currentRoute))return;
  rememberScroll();const previousRoute=currentRoute;
  if(!replace)navigationIndex++;
  history[replace?'replaceState':'pushState']({makerEvents:true,chain:navigationChain,index:navigationIndex,route,scrollY:0,previousRoute},'',routeURL(route));
  showMessage('');renderRoute(route);
}
function backToList(){
  if(navigationIndex>0 && history.state?.previousRoute?.selected===null && history.state.previousRoute.view===view)history.back();
  else navigate({...currentRoute,selected:null},true);
}
window.addEventListener('popstate',ev=>{
  if(undoingNavigation){undoingNavigation=false;return;}
  const target=ev.state;
  if(!target?.makerEvents || target.chain!==navigationChain){location.reload();return;}
  if(busy || !canLeaveDetail()){
    if(busy)showMessage('保存が終わってから移動してください。');
    undoingNavigation=true;history.go(navigationIndex-target.index);return;
  }
  navigationIndex=target.index;showMessage('');renderRoute(target.route,target.scrollY||0);
});
window.addEventListener('beforeunload',ev=>{if(busy || hasUnsavedInput()){ev.preventDefault();ev.returnValue='';}});
// Safariの履歴更新回数制限を避ける。画面内の移動直前には必ず即時保存する。
let scrollSaveTimer;
window.addEventListener('scroll',()=>{if(!scrollSaveTimer)scrollSaveTimer=setTimeout(()=>{scrollSaveTimer=null;rememberScroll();},500);},{passive:true});
function renderDetail(){
  const event=events.find(e=>e.id===selected);if(!event)return;
  $('#list-screen').hidden=true;$('#detail-screen').hidden=false;
  const d=$('#detail');d.replaceChildren();
  const top=section(event.title);top.append(element('p',fmt(event.start_at)+(event.end_at?' 〜 '+fmt(event.end_at):' 終了未確認'),'tabular'),element('p',(event.venue||'会場未確認')+' '+event.address,'muted'));d.append(top);
  const organizer=element('div');organizer.id='organizer-review';d.append(organizer);renderOrganizer(event);
  const attendance=section('参加できそう？');attendance.append(badge(event.attendance.label,event.attendance.code==='possible'?'good':'check'),element('p',event.attendance.reason,'small muted'));d.append(attendance);
  if(event.duplicates.length)attendance.append(element('p','同名・同時刻の別URLがあります。誤統合を避け、別の記録として保持しています。','notice small'));
  const lang=section('日本語で参加できる？');lang.append(badge(event.effective_language,event.effective_language==='要確認'?'check':'good'),element('p',event.effective_language_evidence||'当日の使用言語の明記がありません。日本語の告知だけでは確定しません。','small muted'));d.append(lang);
  const status=section('参加状態');const grid=element('div',undefined,'status-grid');grid.id='status-buttons';
  statuses.forEach(s=>{const b=button(statusLabel(s),()=>setStatus(event,s));b.dataset.status=s;b.setAttribute('aria-pressed',String(event.state.status===s));grid.append(b);});
  status.append(grid,element('p','状態の記録だけでは、公式サイトの申込み・キャンセルは行いません。','small muted'));d.append(status);
  const personal=section('自分のメモ・確認');
  personal.append(field('メモ','textarea','note',event.state.note));personal.querySelector('textarea').maxLength=10000;personal.querySelector('textarea').rows=4;
  const travel=field('勤務先からの移動時間（分・空欄なら45分の仮設定）','input','travel',event.state.travel_minutes);const travelInput=travel.querySelector('input');travelInput.type='number';travelInput.min=0;travelInput.max=360;personal.append(travel);
  const language=field('本人が確認した言語（収集で上書きしません）','select','personal-language',event.state.language);const select=language.querySelector('select');languages.forEach(l=>select.add(new Option(l,l)));select.value=event.state.language;
  const evidence=field('言語を確認した根拠・公式の記載','input','personal-evidence',event.state.language_evidence);evidence.querySelector('input').maxLength=1000;personal.append(language,evidence);
  personal.append(button('メモ・確認を保存',()=>savePersonal(event),'primary'));d.append(personal);
  const apply=section('公式情報・申込み');apply.append(element('p',event.deadline_at?'申込締切 '+fmt(event.deadline_at):'申込締切は未確認です。公式サイトを確認してください。','muted'),officialLink(event));
  apply.append(element('p','最終取得：'+event.sources.map(s=>s.source+' '+fmt(s.last_seen)).join(' / '),'small muted'));
  apply.append(button('公式情報を手動修正',()=>openForm(event),'quiet'));d.append(apply);
  const desc=section('イベントの概要');desc.append(element('p',event.description||'説明は未登録です。','description small'));d.append(desc);
  const hist=section('変更履歴');const history=element('div');history.id='history';hist.append(history);d.append(hist);loadHistory(event.id);
}
async function loadHistory(id){
  try{const result=await request('/api/events/'+id+'/history');if(selected!==id)return;const box=$('#history');box.replaceChildren();
    result.history.forEach(h=>{
      const row=element('div',undefined,'history-item');row.append(element('p',h.kind+(h.actor==='owner'?' · あなたの記録':' · 公式の取得')),element('p',fmt(h.created_at),'small muted'));
      if(h.before){Object.keys(h.after).filter(k=>labels[k] && JSON.stringify(h.before[k])!==JSON.stringify(h.after[k])).forEach(k=>{
        const value=v=>v===null||v===undefined||v===''?'未設定':k==='status'?statusLabel(v):typeof v==='boolean'?(v?'あり':'なし'):Array.isArray(v)?v.join(' / '):String(v);
        const text=element('p',labels[k]+'：'+value(h.before[k])+' → '+value(h.after[k]),'small');
        if(text.textContent.length>400){const fold=element('details');fold.append(element('summary',labels[k]+'の変更'),text);row.append(fold);}else row.append(text);
      });}box.append(row);
    });
  }catch(e){if(selected===id)$('#history').replaceChildren(element('p','履歴を取得できませんでした。 '+e.message,'error'));}
}
async function writeState(event, fields){
  if(busy)return false;busy=true;
  document.querySelectorAll('#status-buttons button,[data-decline-action],[data-visibility-action]').forEach(b=>b.disabled=true);
  try{
    const result=await request('/api/events/'+event.id+'/state',{...fields,version:event.state.version});event.state=result.state;
    document.querySelectorAll('#status-buttons button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.status===event.state.status)));
    if(selected===event.id)loadHistory(event.id);else renderList();
    showMessage('記録を保存しました');return true;
  }catch(e){showMessage(e.message+(e.status===409?'（入力は画面に残しています。最新情報を更新し、内容を確認してください）':''),true);return false;}
  finally{busy=false;document.querySelectorAll('#status-buttons button,[data-decline-action],[data-visibility-action]').forEach(b=>b.disabled=false);}
}
async function setStatus(event,status){await writeState(event,{status});}
async function recordDecline(event){
  const previous=event.state.status,y=scrollY;
  if(await writeState(event,{status:'見送り'})){
    if(!selected)requestAnimationFrame(()=>window.scrollTo(0,y));
    const version=event.state.version;
    visibilityToast('不参加として記録しました',async()=>{
      const latest=events.find(e=>e.id===event.id)||event;
      if(latest.state.version!==version){visibilityToast('参加状態が変わっています。詳細で確認してください。');return;}
      if(await writeState(latest,{status:previous}))visibilityToast('不参加の記録を取り消しました');
      else visibilityToast('取り消せませんでした。更新して確認してください。');
    });
  }else visibilityToast('不参加を保存できませんでした。表示は変更していません。');
}
let visibilityToastTimer;
function visibilityToast(text,undo){
  clearTimeout(visibilityToastTimer);const box=$('#visibility-toast');box.replaceChildren(element('span',text));
  if(undo)box.append(button('元に戻す',undo));
  const close=button('×',()=>{box.hidden=true;},'quiet');close.setAttribute('aria-label','通知を閉じる');box.append(close);box.hidden=false;
  visibilityToastTimer=setTimeout(()=>{box.hidden=true;},12000);
}
async function setVisibility(event,hidden){
  if(busy)return;busy=true;document.querySelectorAll('[data-visibility-action],[data-decline-action]').forEach(b=>b.disabled=true);
  try{
    const result=await request('/api/events/'+event.id+'/visibility',{hidden,version:event.visibility?.version||0});event.visibility=result.visibility;
    if(selected){if(selected===event.id)loadHistory(event.id);}else{const y=scrollY;renderList();requestAnimationFrame(()=>window.scrollTo(0,y));}
    visibilityToast(hidden?'興味なしにして非表示にしました':'興味なしを解除しました',()=>setVisibility(events.find(e=>e.id===event.id)||event,!hidden));
  }catch(e){visibilityToast('保存できませんでした。'+e.message);}
  finally{busy=false;document.querySelectorAll('[data-visibility-action],[data-decline-action]').forEach(b=>b.disabled=false);}
}
async function savePersonal(event){
  const organizerDraft=[...document.querySelectorAll('#organizer-review input,#organizer-review textarea')].map(e=>[e.id,e.value]);
  const minutes=$('#travel').value;
  const success=await writeState(event,{note:$('#note').value,travel_minutes:minutes===''?null:Number(minutes),language:$('#personal-language').value,language_evidence:$('#personal-evidence').value});
  if(success){const data=await request('/api/events');events=data.events;renderDetail();organizerDraft.forEach(([id,value])=>{$('#'+id).value=value;});showMessage('メモと確認情報を保存しました');}
}
function renderOrganizer(event){
  const box=$('#organizer-review');box.replaceChildren();const s=section('主催者の確認');const review=event.organizer_review||{};
  s.append(badge(organizerLabel(event),organizerDecision(event)==='allowed'?'good':'check'));
  s.append(element('p','配信上の主催者・掲載者名：'+(event.organizer_name||'未提供'),'small muted'));
  if(event.organizer_evidence)s.append(element('p',event.organizer_evidence,'small muted'));
  if(review.identity_changed)s.append(element('p','確認後に掲載者情報が変わりました。再確認するまで通常一覧から外しています。','notice small'));
  s.append(element('p','通常一覧には、主催者と活動実績を確認したイベントを表示します。名前の掲載だけでは確認済みにしません。マイイベントの記録は残ります。','small muted'));
  if(review.url){const a=element('a','保存した確認根拠を開く ↗');a.href=review.url;a.target='_blank';a.rel='noopener noreferrer';s.append(a);}
  if(review.evidence)s.append(element('p',review.evidence,'small'));
  if(review.updated_at)s.append(element('p',(review.actor==='owner'?'あなたの確認':'公式情報に基づく確認')+' · '+fmt(review.updated_at),'small muted'));
  s.append(field('確認した主催者名','input','organizer-name',review.name||event.organizer_name||''));
  s.append(field('主催者・開催実績の確認先URL','input','organizer-url',review.url||event.organizer_url||event.url));
  s.append(field('確認した根拠・非表示にする理由','textarea','organizer-evidence',review.evidence||''));
  const actions=element('div',undefined,'actions');
  actions.append(button('確認済みにして表示',()=>saveOrganizer(event,'allowed'),'primary'),button('このイベントを非表示',()=>saveOrganizer(event,'hidden')),button('要確認に戻す',()=>saveOrganizer(event,'pending'),'quiet'));s.append(actions);box.append(s);
  box.querySelectorAll('input,textarea').forEach(e=>{e.dataset.initial=e.value;e.maxLength=e.id==='organizer-url'?2048:e.id==='organizer-name'?300:1000;});
}
async function saveOrganizer(event,decision){
  if(busy)return;busy=true;$('#organizer-review').querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const result=await request('/api/events/'+event.id+'/organizer',{decision,name:$('#organizer-name').value,url:$('#organizer-url').value,evidence:$('#organizer-evidence').value,version:event.organizer_review?.version||0});
    event.organizer_review={...result.review,effective_decision:result.review.decision,identity_changed:false};
    renderOrganizer(event);loadHistory(event.id);showMessage('主催者の確認を保存しました');
  }catch(e){showMessage(e.message,true);}finally{busy=false;$('#organizer-review').querySelectorAll('button').forEach(b=>b.disabled=false);}
}
function renderSources(cards){
  for(const source of ['TECH PLAY','Luma','connpass','Peatix','祝日']){
    const r=overview.runs?.find(x=>x.source===source);const c=element('section',undefined,'card');c.append(element('h3',source));
    const pending=source==='connpass' && overview.metadata?.connpass_application_status==='submitted' && (!r || r.status==='unconfigured');
    const status=pending?'pending':r?.status||(source==='Peatix'?'manual':'未実行');c.append(badge({ok:'取得成功',partial:'一部要確認',error:'取得失敗',unconfigured:'未接続',pending:'申請済み・審査待ち',manual:'公式URLの手動登録'}[status]||status,status==='ok'?'good':'check'));
    c.append(element('p',pending?'無料APIの利用申請は送信済みです。審査とAPIキーの発行を待っています。イベントの取得はまだ開始していません。':source==='Peatix'?'公式に使える一般イベント検索API・配信経路はまだ確認できていません。公式URLを登録して状態・メモを管理できます。':r?.detail||'日次処理はまだ実行されていません。','small muted'));
    if(r)c.append(element('p','最終実行 '+fmt(r.finished_at)+' · '+r.count+'件','small muted'));
    const guides={'connpass':'https://help.connpass.com/api/','TECH PLAY':'https://techplay.jp/information/236','Luma':'https://help.luma.com/p/ical-syncing','Peatix':'https://help.peatix.com/'};
    if(guides[source]){const a=element('a','公式の利用案内 ↗');a.href=guides[source];a.target='_blank';a.rel='noopener noreferrer';c.append(a);}
    if(source==='connpass'){const a=element('a',pending?'申請状況':'無料APIを申請（技術欄記入済み）','button');a.href='/connpass-application';c.append(a);}
    cards.append(c);
  }
  const storage=element('section',undefined,'card full');storage.append(element('h3','保存と日次処理'),element('p','本人の状態・メモと公式情報を分けてSQLiteへ保存します。日次はPythonだけで動き、LLMのトークンは使いません。','small muted'),element('p','毎日7時に収集とローカルバックアップを実行。PC停止中は利用できません。APIの認証情報がない取得元にはアクセスしません。','small muted'));
  storage.append(element('p','現在の指定：興味なし '+events.filter(e=>e.visibility?.hidden).length+'件 ／ 不参加 '+events.filter(e=>e.state.status==='見送り').length+'件。別々に日時付きで保存し、下のダウンロードに含めます。','small muted'));
  const exportLink=element('a','データと変更履歴をダウンロード','button');exportLink.href='/api/export';storage.append(exportLink);cards.append(storage);
}
function openForm(event=null){
  editing=event;$('#event-form').reset();$('#form-error').textContent='';$('#form-title').textContent=event?'公式情報を手動修正':'公式情報を登録';
  if(event)for(const name of ['title','url','start_at','end_at','deadline_at','venue','address','description','online','all_day','cancelled','cancel_evidence']){
    const input=$('#event-form').elements.namedItem(name);if(input.type==='checkbox')input.checked=!!event[name];else input.value=name.endsWith('_at')?(event[name]||'').slice(0,16):event[name]||'';
  }
  $('#event-dialog').showModal();
}
$('#event-form').addEventListener('submit',async ev=>{
  ev.preventDefault();const form=ev.currentTarget;const save=form.querySelector('[type=submit]');save.disabled=true;
  const raw=Object.fromEntries(new FormData(form));raw.online=form.elements.online.checked;raw.all_day=form.elements.all_day.checked;raw.cancelled=form.elements.cancelled.checked;
  try{let id=editing?.id;if(editing)await request('/api/events/'+id+'/facts',{version:editing.version,facts:raw});else id=(await request('/api/events',raw)).id;
    $('#event-dialog').close();await load();openDetail(id);showMessage('公式情報を保存しました');
  }catch(e){$('#form-error').textContent=e.message;}finally{save.disabled=false;}
});
$('#cancel-form').addEventListener('click',()=>$('#event-dialog').close());
$('#add').addEventListener('click',()=>openForm());
$('#back').addEventListener('click',backToList);
$('#reload').addEventListener('click',()=>{
  if(selected && !confirm('未保存の入力は破棄されます。最新情報に更新しますか？'))return;
  load();
});
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>navigate({...currentRoute,view:b.dataset.view,selected:null})));
['period','language-filter','state-filter','organizer-filter','visibility-filter','schedule-filter','day-filter'].forEach(id=>$('#'+id).addEventListener('change',()=>navigate({...currentRoute,period:$('#period').value,language:$('#language-filter').value,status:$('#state-filter').value,organizer:$('#organizer-filter').value,visibility:$('#visibility-filter').value,schedule:$('#schedule-filter').value,days:$('#day-filter').value})));
currentRoute=readRoute();
const savedNavigation=history.state;
navigationChain=savedNavigation?.makerEvents?savedNavigation.chain:(crypto.randomUUID?.()||Date.now()+'-'+Math.random());
navigationIndex=savedNavigation?.makerEvents?savedNavigation.index:0;
history.scrollRestoration='manual';
history.replaceState({makerEvents:true,chain:navigationChain,index:navigationIndex,route:currentRoute,scrollY:savedNavigation?.scrollY||0,previousRoute:savedNavigation?.previousRoute},'',routeURL(currentRoute));
load();
