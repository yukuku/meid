const initial=JSON.parse(document.querySelector('#initial').textContent);
const rows=[...document.querySelectorAll('.finding')],boxes=[...document.querySelectorAll('[data-check]')],q=document.querySelector('#query'),month=document.querySelector('#month'),review=document.querySelector('#review'),status=document.querySelector('#status'),dialog=document.querySelector('#editor'),editorStatus=document.querySelector('#editor-status');
let checks=initial.checks,entry=null,dirty=false,saving=false,requests=0;
function setStatus(message,error=false){status.textContent=message;status.classList.toggle('error',error)}
function filter(){let n=0;for(const row of rows.filter(r=>r.isConnected)){const done=!!checks[row.dataset.id];row.hidden=!!((month.value&&month.value!==row.dataset.month)||(review.value==='done'&&!done)||(review.value==='pending'&&done)||!row.textContent.toLowerCase().includes(q.value.toLowerCase()));if(!row.hidden)n++}const active=rows.filter(r=>r.isConnected);document.querySelector('#count').textContent=`${n} / ${active.length} findings · ${active.filter(r=>checks[r.dataset.id]).length} reviewed`;document.querySelector('#remaining').textContent=`${active.length} remaining findings · edit in place`;document.querySelector('#empty').hidden=active.length!==0}
function renderChecks(){for(const b of boxes)b.checked=!!checks[b.dataset.check];filter()}
async function api(path,data){requests++;try{const response=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-Review-Token':initial.token}:{},body:data?JSON.stringify(data):undefined,cache:'no-store'});const result=await response.json();if(!response.ok)throw Error(result.error||'Request failed');return result}catch(error){throw Error(error.message==='Failed to fetch'?'Local server is unavailable. Keep your edits open and restart the Python app.':error.message)}finally{requests--}}
for(const b of boxes)b.addEventListener('change',async()=>{b.disabled=true;setStatus('Saving review status…');try{const data=await api('/api/mark',{id:b.dataset.check,checked:b.checked,version:b.closest('.finding').dataset.version});checks=data.checks;renderChecks();setStatus('Review status saved on disk')}catch(error){b.checked=!!checks[b.dataset.check];setStatus(error.message,true)}finally{b.disabled=false}});
function rich(text){return text.split(/(<i>|<\/i>)/g).map(part=>part==='<i>'||part==='</i>'?part:part.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')).join('')}
function escapeText(text){return text.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}
function highlightText(text,ranges){let result='',cursor=0;for(const [a,z] of ranges){if(z<=cursor)continue;const start=Math.max(cursor,a);result+=escapeText(text.slice(cursor,start))+'<mark>'+escapeText(text.slice(start,z))+'</mark>';cursor=z}return result+escapeText(text.slice(cursor))+'\n'}
function moveHighlights(before,after,ranges){let a=0;while(a<before.length&&a<after.length&&before[a]===after[a])a++;let z=before.length,end=after.length;while(z>a&&end>a&&before[z-1]===after[end-1]){z--;end--}const delta=end-z;return ranges.map(([start,stop])=>[start<=a?start:start>=z?start+delta:a,stop<a?stop:stop>=z?stop+delta:end]).filter(([start,stop])=>stop>start)}
for(const button of document.querySelectorAll('[data-edit]'))button.addEventListener('click',async()=>{
 button.disabled=true;
 try{
  entry=await api('/api/edit/'+button.closest('.finding').dataset.id);
  const host=document.querySelector('#fields');host.replaceChildren();
  document.querySelector('#editor-title').textContent='Edit suggestion · '+entry.date+' · '+entry.finding;
  const names={a:'Opening verse',n:'Paragraph',p:'Poem',f:'Translator note'};
  for(const warning of entry.warnings){const note=document.createElement('div');note.className='notice';const title=document.createElement('p');title.textContent='This passage has changed in your file. Your wording is preserved below; incorporate this suggestion manually if you want it:';const proposal=document.createElement('p');proposal.innerHTML=rich(warning.suggestion);note.append(title,proposal);host.append(note)}
  entry.fields.forEach((field,i)=>{
   const section=document.createElement('section');section.className='field';
   const label=document.createElement('label');label.className='field-label';label.htmlFor='field-'+i;label.textContent=names[field.kind]+' '+(i+1)+(field.highlights.length?' · passage under review':'');
   const wrap=document.createElement('div');wrap.className='highlight-editor';
   const ink=document.createElement('div');ink.className='highlight-ink';ink.setAttribute('aria-hidden','true');
   const area=document.createElement('textarea');area.id='field-'+i;area.value=field.text;area.rows=Math.min(16,Math.max(3,Math.ceil(field.text.length/100)));area.spellcheck=false;
   let previous=area.value,ranges=field.highlights;
   const align=()=>{ink.style.width=(area.clientWidth+2)+'px';ink.scrollTop=area.scrollTop;ink.scrollLeft=area.scrollLeft};
   const paint=()=>{ink.innerHTML=highlightText(area.value,ranges);align()};
   area.addEventListener('scroll',align);
   const observer=new ResizeObserver(align);observer.observe(area);area._observer=observer;
   const italic=document.createElement('button');italic.type='button';italic.textContent='Italic';
   const preview=document.createElement('div');preview.className='preview';preview.innerHTML=rich(field.text);
   const update=()=>{ranges=moveHighlights(previous,area.value,ranges);previous=area.value;paint();dirty=true;editorStatus.textContent='Unsaved changes';preview.innerHTML=rich(area.value)};
   area.addEventListener('input',update);
   italic.addEventListener('click',()=>{const a=area.selectionStart,z=area.selectionEnd;area.setRangeText('<i>'+area.value.slice(a,z)+'</i>',a,z,'select');area.focus();update()});
   wrap.append(ink,area);section.append(label,wrap,italic,preview);host.append(section);paint();
  });
  dirty=false;editorStatus.textContent='Proposal only — edit freely, then Apply this suggestion to write it to meid-am.txt';editorStatus.classList.remove('error');dialog.showModal();
  requestAnimationFrame(()=>{host.querySelectorAll('.highlight-editor').forEach(wrap=>{wrap.querySelector('.highlight-ink').style.width=(wrap.querySelector('textarea').clientWidth+2)+'px'});const mark=host.querySelector('mark');if(mark){const wrap=mark.closest('.highlight-editor'),area=wrap.querySelector('textarea'),ink=wrap.querySelector('.highlight-ink');area.scrollTop=Math.max(0,mark.offsetTop-area.clientHeight/3);ink.scrollTop=area.scrollTop;wrap.closest('.field').scrollIntoView({block:'start'});area.focus({preventScroll:true})}});
 }catch(error){setStatus(error.message,true)}finally{button.disabled=false}
});
function closeEditor(){if(saving)return;if(dirty&&!confirm('Discard these unsaved edits?'))return;document.querySelectorAll('#fields textarea').forEach(area=>area._observer?.disconnect());dialog.close();dirty=false;entry=null}
document.querySelector('#close').onclick=closeEditor;dialog.addEventListener('cancel',event=>{event.preventDefault();closeEditor()});
document.querySelector('#save').onclick=async()=>{if(!entry||saving)return;saving=true;document.querySelector('#save').disabled=true;const fields=[...document.querySelectorAll('#fields textarea')];fields.forEach(f=>f.disabled=true);document.querySelectorAll('#fields button').forEach(b=>b.disabled=true);editorStatus.classList.remove('error');editorStatus.textContent='Saving to meid-am.txt…';try{const result=await api('/api/apply',{id:entry.finding,version:entry.version,fields:fields.map(f=>f.value)});entry.version=result.version;for(const row of rows.filter(r=>r.dataset.date===entry.date)){row.dataset.version=result.version;row.querySelector('.proposed .column').innerHTML=result.columns[row.dataset.id];row.querySelectorAll('.column')[1].innerHTML=result.current_columns[row.dataset.id]}for(const id of result.resolved)document.getElementById(id)?.remove();checks=result.checks;renderChecks();dirty=false;editorStatus.textContent='Saved to meid-am.txt. Corrected findings have been removed from the list.';setStatus('Suggestion applied to meid-am.txt');saving=false;closeEditor()}catch(error){editorStatus.textContent=error.message;editorStatus.classList.add('error')}finally{saving=false;document.querySelector('#save').disabled=false;fields.forEach(f=>f.disabled=false);document.querySelectorAll('#fields button').forEach(b=>b.disabled=false)}};
document.querySelector('#reload-file').onclick=()=>{if(dirty&&!confirm('Discard unsaved editor changes and reload the file?'))return;dirty=false;location.reload()};
for(const c of[q,month,review])c.addEventListener('input',filter);
document.querySelector('#export').onclick=async()=>{try{const data=await api('/api/export');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='renungan-review-progress.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(error){setStatus(error.message,true)}};
document.querySelector('#import').addEventListener('change',async event=>{const input=event.target;try{if(!input.files[0])return;if(input.files[0].size>50000)throw Error('Progress backup is too large');const data=JSON.parse(await input.files[0].text());const result=await api('/api/import',data);checks=result.checks;renderChecks();setStatus('Imported reviewed checks and saved them on disk')}catch(error){setStatus(error.message,true)}finally{input.value=''}});
window.addEventListener('beforeunload',event=>{if(dirty||saving||requests){event.preventDefault();event.returnValue=''}});renderChecks();
