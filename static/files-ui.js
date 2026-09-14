const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dialog=document.createElement('dialog');
dialog.id='file-exchange';
dialog.innerHTML=`<div class="dialog-title"><h2>Файлы</h2><button class="icon" data-file-close aria-label="Закрыть">×</button></div>
<div class="file-toolbar"><label>Папка<select id="file-source"></select></label><label class="file-button">Загрузить с ПК<input type="file" id="file-pick" multiple hidden></label><button data-file-refresh aria-label="Обновить">↻</button></div>
<div id="file-destination"><label>Отправлять в телефон<select id="file-target"></select></label></div>
<p id="file-message" role="status"></p><div id="file-list"></div><p class="note">До 512 МБ на файл · 5 ГБ на аккаунт</p>`;
document.body.append(dialog);
const $=s=>dialog.querySelector(s);
let phones=[],loading=false;
const size=n=>n>=1024**3?(n/1024**3).toFixed(2)+' ГБ':n>=1024**2?(n/1024**2).toFixed(1)+' МБ':Math.max(1,Math.ceil(n/1024))+' КБ';
function message(text,error=false){$('#file-message').textContent=text;$('#file-message').classList.toggle('error',error)}
async function api(path,method='GET',body){
 const r=await fetch('/api'+path,{method,credentials:'same-origin',headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});
 const result=await r.json().catch(()=>({detail:'Сервер недоступен'}));
 if(!r.ok)throw Error(typeof result.detail==='string'?result.detail:'Ошибка запроса');return result;
}
function download(id){const a=document.createElement('a');a.href='/api/files/'+encodeURIComponent(id)+'/download';a.download='';document.body.append(a);a.click();a.remove()}
async function refresh(){
 const source=$('#file-source').value;
 $('#file-destination').classList.toggle('hidden',source!=='server');
 const data=await api(source==='server'?'/files':`/devices/${source}/files`);
 if(source!==$('#file-source').value)return;
 if(source==='server'){
  $('#file-list').innerHTML=data.files.map(f=>`<div class="list-row"><div class="file-name"><strong>${esc(f.name)}</strong><p>${size(f.size)} · ${new Date(f.created*1000).toLocaleString('ru-RU')}</p></div><div class="file-actions"><a class="file-button" href="/api/files/${f.id}/download" download>Скачать</a><button data-file-send="${f.id}" ${phones.length?'':'disabled'}>В телефон</button><button data-file-delete="${f.id}" aria-label="Удалить ${esc(f.name)}">×</button></div></div>`).join('')||'<div class="empty">Папка пуста</div>';
 }else{
  $('#file-list').innerHTML=data.files.map(name=>`<div class="list-row"><strong class="file-name">${esc(name)}</strong><button data-file-import="${esc(name)}">Скачать на ПК</button></div>`).join('')||'<div class="empty">В Download нет файлов</div>';
 }
}
async function open(){
 if(!dialog.open)dialog.showModal();message('Загрузка…');
 try{
  phones=(await api('/devices')).filter(p=>p.status==='running');
  $('#file-source').innerHTML='<option value="server">Моя папка на сервере</option>'+phones.map(p=>`<option value="${p.id}">${esc(p.name)} · Download</option>`).join('');
  $('#file-target').innerHTML=phones.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')||'<option>Нет запущенных телефонов</option>';
  await refresh();message('');
 }catch(e){message(e.message,true)}
}
document.addEventListener('click',e=>{if(e.target.closest('[data-action="files-open"]'))open()});
$('#file-source').onchange=()=>refresh().catch(e=>message(e.message,true));
dialog.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 if(b.hasAttribute('data-file-close')){dialog.close();return}
 if(loading)return;
 loading=true;b.disabled=true;
 try{
  if(b.hasAttribute('data-file-refresh')){await refresh();message('Обновлено')}
  if(b.dataset.fileSend){message('Передаём в телефон…');await api(`/files/${b.dataset.fileSend}/send/${$('#file-target').value}`,'POST');message('Файл в Download телефона')}
  if(b.dataset.fileDelete&&confirm('Удалить файл из папки на сервере?')){await api('/files/'+b.dataset.fileDelete,'DELETE');await refresh();message('Файл удалён')}
  if(b.hasAttribute('data-file-import')){message('Получаем файл из телефона…');const f=await api(`/devices/${$('#file-source').value}/files/import`,'POST',{name:b.dataset.fileImport});download(f.id);message('Файл сохранён на сервере и отправлен на скачивание')}
 }catch(e){message(e.message,true)}finally{b.disabled=false;loading=false}
});
function upload(file){return new Promise((resolve,reject)=>{
 const xhr=new XMLHttpRequest();xhr.open('POST','/api/files');
 xhr.upload.onprogress=e=>{if(e.lengthComputable)message(`${file.name} · ${Math.round(e.loaded/e.total*100)}%`)};
 xhr.onload=()=>{let body;try{body=JSON.parse(xhr.responseText)}catch{reject(Error('Сервер недоступен'));return}xhr.status<300?resolve(body):reject(Error(typeof body.detail==='string'?body.detail:'Ошибка загрузки'))};
 xhr.onerror=()=>reject(Error('Соединение прервано'));
 const data=new FormData();data.append('file',file);xhr.send(data);
})}
$('#file-pick').onchange=async e=>{
 const files=Array.from(e.target.files);e.target.disabled=true;
 try{for(const f of files){if(f.size>512*1024**2)throw Error('Максимальный размер файла — 512 МБ');await upload(f)}$('#file-source').value='server';await refresh();message('Загружено')}
 catch(e){message(e.message,true)}finally{e.target.value='';e.target.disabled=false}
};
