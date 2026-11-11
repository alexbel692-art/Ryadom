'use strict';
// «Рядом» — панель администратора (без зависимостей)

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const state = { token: sessionStorage.getItem('ryadom_token') || localStorage.getItem('ryadom_token'), me: null, timer: null };

function toast(t) {
  const el = $('#toast'); el.textContent = t; el.classList.add('show');
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove('show'), 2200);
}
function copy(t) { navigator.clipboard?.writeText(t).then(() => toast('Скопировано'), () => toast('Не удалось скопировать')); }

async function api(path, opts = {}) {
  const r = await fetch('/panel/api' + path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json', ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* пусто */ }
  if (r.status === 401 && path !== '/login') { logout(true); throw new Error('Сессия истекла'); }
  if (!r.ok) throw new Error(data?.error || ('Ошибка ' + r.status));
  return data;
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000), diff = Date.now() / 1000 - ts;
  if (diff < 60) return 'только что';
  if (diff < 3600) return Math.floor(diff / 60) + ' мин назад';
  if (diff < 86400 && new Date().getDate() === d.getDate()) return 'сегодня ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function fmtDur(a, b) {
  if (!a) return '—';
  const s = Math.max(0, (b || Date.now() / 1000) - a);
  if (s < 60) return Math.round(s) + ' с';
  if (s < 3600) return Math.round(s / 60) + ' мин';
  return (s / 3600).toFixed(1).replace('.', ',') + ' ч';
}
function osShort(os) {
  if (!os) return '—';
  return os.split(' / ')[1] || os.split(' / ')[0];
}
function fmtId(id) { return String(id).replace(/(\d{3})(?=\d)/g, '$1 '); }

// ------------------------------------------------------------------ вход
function renderLogin(msg = '') {
  clearInterval(state.timer);
  $('#root').innerHTML = `
  <div class="login"><form class="card" id="lf">
    <img src="/logo.svg" alt="">
    <h1>Вход в панель</h1>
    <div class="muted small">Только для администраторов</div>
    <label>Логин</label><input name="u" autocomplete="username" required autofocus>
    <label>Пароль</label><input name="p" type="password" autocomplete="current-password" required>
    <label class="row" style="color:var(--ink);font-size:14px"><input type="checkbox" name="r" style="width:auto"> Запомнить на этом компьютере</label>
    <div class="err" id="le">${esc(msg)}</div>
    <button class="btn primary" style="width:100%;justify-content:center;margin-top:6px">Войти</button>
  </form></div>`;
  $('#lf').onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      const d = await api('/login', { method: 'POST', body: { username: f.u.value, password: f.p.value } });
      state.token = d.token;
      (f.r.checked ? localStorage : sessionStorage).setItem('ryadom_token', d.token);
      boot();
    } catch (err) { $('#le').textContent = err.message; }
  };
}
async function logout(silent) {
  if (!silent) { try { await api('/logout', { method: 'POST' }); } catch { } }
  sessionStorage.removeItem('ryadom_token'); localStorage.removeItem('ryadom_token');
  state.token = null; renderLogin(silent ? 'Войдите снова' : '');
}

// ------------------------------------------------------------------ каркас
const PAGES = {
  devices: ['Устройства', pageDevices],
  sessions: ['Сеансы', pageSessions],
  files: ['Файлы', pageFiles],
  users: ['Пользователи', pageUsers],
  connect: ['Подключение', pageConnect],
  account: ['Мой аккаунт', pageAccount],
};
function shell() {
  $('#root').innerHTML = `
  <div class="layout">
    <aside class="side">
      <div class="brand"><img src="/logo.svg" alt=""><span>${esc(state.me.app_name)}</span></div>
      <nav class="nav">${Object.entries(PAGES).map(([k, [t]]) => `<a href="#${k}" data-p="${k}">${t}</a>`).join('')}</nav>
      <div class="foot muted">${esc(state.me.display_name || state.me.name)}<br><a href="#" id="lo">Выйти</a></div>
    </aside>
    <main id="main"></main>
  </div>`;
  $('#lo').onclick = (e) => { e.preventDefault(); logout(); };
}
function route() {
  clearInterval(state.timer);
  const key = (location.hash.slice(1) || 'devices').split('/')[0];
  const [, fn] = PAGES[key] || PAGES.devices;
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('active', a.dataset.p === key));
  fn($('#main'));
}
async function boot() {
  if (!state.token) return renderLogin();
  try { state.me = await api('/me'); } catch { return; }
  document.title = state.me.app_name + ' — панель';
  shell(); route();
}
window.addEventListener('hashchange', () => state.me && route());

// ------------------------------------------------------------------ диалоги
function dialog(html, onSubmit) {
  const d = $('#dlg');
  d.innerHTML = `<form method="dialog" id="df">${html}<div class="err" id="de"></div>
    <div class="row end"><button class="btn" value="cancel" formnovalidate>Отмена</button><button class="btn primary" value="ok">Сохранить</button></div></form>`;
  d.showModal();
  $('#df').onsubmit = async (e) => {
    if (e.submitter?.value !== 'ok') return;
    e.preventDefault();
    try { await onSubmit(e.target); d.close(); } catch (err) { $('#de').textContent = err.message; }
  };
}
function confirmDlg(text, okText = 'Удалить') {
  return new Promise(res => {
    const d = $('#dlg');
    d.innerHTML = `<p style="margin-top:0">${text}</p><div class="row end"><button class="btn" id="cn">Отмена</button><button class="btn primary" id="cy">${okText}</button></div>`;
    d.showModal();
    $('#cn').onclick = () => { d.close(); res(false); };
    $('#cy').onclick = () => { d.close(); res(true); };
  });
}

// ------------------------------------------------------------------ устройства
async function pageDevices(main) {
  main.innerHTML = `
    <div class="stats" id="st"></div>
    <div class="head"><h1>Устройства</h1>
      <select id="flt" style="width:auto"><option value="">Все</option><option value="on">В сети</option><option value="off">Не в сети</option></select>
      <input class="search" id="q" placeholder="Поиск: ID, имя, пользователь, заметка">
    </div>
    <div class="card tbl"><table>
      <thead><tr><th></th><th>Устройство</th><th>ID</th><th class="hide-m">Система</th><th class="hide-m">Владелец</th><th class="hide-m">Был в сети</th><th></th></tr></thead>
      <tbody id="tb"><tr><td colspan="7" class="empty">Загрузка…</td></tr></tbody>
    </table></div>`;
  let all = [];
  const scheme = state.me.uri_scheme;
  const draw = () => {
    const s = $('#q').value.trim().toLowerCase(), f = $('#flt').value;
    const rows = all.filter(d => (!f || (f === 'on') === d.online) &&
      (!s || [d.id, d.hostname, d.alias, d.username, d.owner, d.note, d.ip].join(' ').toLowerCase().includes(s.replace(/\s/g, '')) ||
        [d.hostname, d.alias, d.username, d.owner, d.note].join(' ').toLowerCase().includes(s)));
    $('#tb').innerHTML = rows.length ? rows.map(d => `
      <tr>
        <td><span class="dot ${d.online ? 'on' : ''}" title="${d.online ? 'В сети' : 'Не в сети'}"></span></td>
        <td class="name-cell"><b>${esc(d.alias || d.hostname || 'Без имени')}</b>
          <span class="muted small">${esc(d.username)}${d.note ? ' · ' + esc(d.note) : ''}</span>
          ${d.active_sessions ? `<span class="badge red">Сеансов: ${d.active_sessions}</span>` : ''}</td>
        <td class="mono"><span class="copy" data-id="${esc(d.id)}">${esc(fmtId(d.id))}</span></td>
        <td class="hide-m small">${esc(osShort(d.os))}<div class="muted">${esc(d.version ? 'v' + d.version : '')}</div></td>
        <td class="hide-m small">${esc(d.owner || '—')}</td>
        <td class="hide-m small">${d.online ? '<span class="badge">в сети</span>' : esc(fmtTime(d.last_seen))}</td>
        <td><div class="actions">
          <a class="btn sm primary" href="${esc(scheme)}://connect/${esc(d.id)}" title="Открыть в программе">Подключиться</a>
          <a class="btn sm hide-m" href="${esc(scheme)}://file-transfer/${esc(d.id)}">Файлы</a>
          ${d.active_sessions ? `<button class="btn sm danger" data-dis="${esc(d.id)}">Разорвать</button>` : ''}
          <button class="btn sm" data-ed="${esc(d.id)}">⋯</button>
        </div></td>
      </tr>`).join('') : `<tr><td colspan="7" class="empty">${all.length ? 'Ничего не найдено' : 'Устройств пока нет. Установите клиент на компьютер — он появится здесь автоматически.'}</td></tr>`;
    document.querySelectorAll('[data-id]').forEach(el => el.onclick = () => copy(el.dataset.id));
    document.querySelectorAll('[data-dis]').forEach(el => el.onclick = () => disconnect(el.dataset.dis));
    document.querySelectorAll('[data-ed]').forEach(el => el.onclick = () => editDevice(all.find(d => d.id === el.dataset.ed)));
  };
  const load = async () => {
    try {
      const [st, devs] = await Promise.all([api('/stats'), api('/devices')]);
      all = devs;
      $('#st').innerHTML = [['Устройств', st.devices], ['В сети', st.online], ['Активных сеансов', st.active_sessions], ['Сеансов за сутки', st.sessions_24h]]
        .map(([k, v]) => `<div class="card stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');
      draw();
    } catch (e) { toast(e.message); }
  };
  $('#q').oninput = draw; $('#flt').onchange = draw;
  await load();
  state.timer = setInterval(load, 10000);

  async function disconnect(id) {
    if (!await confirmDlg(`Разорвать все активные сеансы на устройстве <b>${esc(fmtId(id))}</b>? Это произойдёт в течение ~15 секунд.`, 'Разорвать')) return;
    try { await api(`/devices/${id}/disconnect`, { method: 'POST' }); toast('Команда отправлена'); } catch (e) { toast(e.message); }
  }
  async function editDevice(d) {
    let users = [];
    try { users = await api('/users'); } catch { }
    const det = await api('/devices/' + d.id).catch(() => ({ sysinfo: {}, sessions: [] }));
    dialog(`
      <h2>${esc(d.alias || d.hostname || d.id)}</h2>
      <div class="kv small" style="margin-bottom:6px">
        <div class="k">ID</div><div class="v mono">${esc(d.id)}</div>
        <div class="k">Имя компьютера</div><div class="v">${esc(d.hostname || '—')}</div>
        <div class="k">Пользователь ОС</div><div class="v">${esc(d.username || '—')}</div>
        <div class="k">Система</div><div class="v">${esc(d.os || '—')}</div>
        <div class="k">Процессор / ОЗУ</div><div class="v">${esc(d.cpu || '—')} · ${esc(d.memory || '—')}</div>
        <div class="k">Версия клиента</div><div class="v">${esc(d.version || '—')}</div>
        <div class="k">IP</div><div class="v mono">${esc(d.ip || '—')}</div>
        <div class="k">Впервые</div><div class="v">${esc(fmtTime(d.first_seen))}</div>
      </div>
      <label>Название в панели</label><input name="alias" value="${esc(d.alias)}" placeholder="${esc(d.hostname)}">
      <label>Заметка</label><input name="note" value="${esc(d.note)}" placeholder="Например: бухгалтерия, 2 этаж">
      <label>Владелец</label><select name="owner"><option value="">—</option>${users.map(u => `<option ${u.name === d.owner ? 'selected' : ''}>${esc(u.name)}</option>`).join('')}</select>
      <h3 style="margin-top:16px">Последние сеансы</h3>
      <div class="small">${det.sessions.filter(s => s.authorized_at).slice(0, 8).map(s => `<div>${esc(fmtTime(s.started_at))} — ${esc(s.peer_name || s.peer_id || '?')} (${esc(fmtDur(s.authorized_at, s.closed_at))})</div>`).join('') || '<span class="muted">нет</span>'}</div>
      <div style="margin-top:14px"><button type="button" class="btn sm danger" id="ddel">Удалить устройство из списка</button></div>`,
      async (f) => { await api('/devices/' + d.id, { method: 'PUT', body: { alias: f.alias.value, note: f.note.value, owner: f.owner.value } }); toast('Сохранено'); load(); });
    $('#ddel').onclick = async () => {
      $('#dlg').close();
      if (await confirmDlg(`Удалить <b>${esc(d.alias || d.hostname || d.id)}</b> из списка? Если клиент продолжит работать, устройство появится снова.`)) {
        await api('/devices/' + d.id, { method: 'DELETE' }); toast('Удалено'); load();
      }
    };
  }
}

// ------------------------------------------------------------------ сеансы
async function pageSessions(main) {
  main.innerHTML = `
    <div class="head"><h1>Журнал сеансов</h1>
      <label class="row" style="margin:0;color:var(--ink)"><input type="checkbox" id="au" checked style="width:auto"> только состоявшиеся</label>
      <input class="search" id="q" placeholder="Устройство или кто подключался">
    </div>
    <div class="card tbl"><table>
      <thead><tr><th>Начало</th><th>К устройству</th><th>Кто подключился</th><th class="hide-m">Тип</th><th class="hide-m">IP</th><th>Длительность</th></tr></thead>
      <tbody id="tb"><tr><td colspan="6" class="empty">Загрузка…</td></tr></tbody>
    </table></div>`;
  const load = async () => {
    const qs = new URLSearchParams({ device: $('#q').value.trim(), authorized: $('#au').checked ? '1' : '' });
    try {
      const rows = await api('/audit/conn?' + qs);
      $('#tb').innerHTML = rows.length ? rows.map(r => `
        <tr>
          <td class="small">${esc(fmtTime(r.started_at))}</td>
          <td><b>${esc(r.alias || r.hostname || '')}</b> <span class="mono muted small">${esc(fmtId(r.device_id))}</span></td>
          <td>${r.authorized_at ? `${esc(r.peer_name || '—')} <span class="mono muted small">${esc(fmtId(r.peer_id))}</span>` : '<span class="badge gray">не подтверждён</span>'}</td>
          <td class="hide-m small">${esc(r.conn_type_name)}</td>
          <td class="hide-m mono small">${esc(r.ip)}</td>
          <td class="small">${r.authorized_at ? (r.closed_at ? esc(fmtDur(r.authorized_at, r.closed_at)) : '<span class="badge">идёт · ' + esc(fmtDur(r.authorized_at)) + '</span>') : '—'}</td>
        </tr>`).join('') : '<tr><td colspan="6" class="empty">Сеансов пока не было</td></tr>';
    } catch (e) { toast(e.message); }
  };
  let t; $('#q').oninput = () => { clearTimeout(t); t = setTimeout(load, 300); };
  $('#au').onchange = load;
  await load();
  state.timer = setInterval(load, 15000);
}

// ------------------------------------------------------------------ файлы
async function pageFiles(main) {
  main.innerHTML = `<div class="head"><h1>Передача файлов</h1></div>
    <div class="card tbl"><table>
      <thead><tr><th>Время</th><th>Устройство</th><th>Направление</th><th>Путь</th><th class="hide-m">Кто</th></tr></thead>
      <tbody id="tb"><tr><td colspan="5" class="empty">Загрузка…</td></tr></tbody></table></div>`;
  try {
    const rows = await api('/audit/file');
    $('#tb').innerHTML = rows.length ? rows.map(r => {
      let info = {}; try { info = JSON.parse(r.info); } catch { }
      const n = info.num ? ` <span class="muted small">(${info.num} шт.)</span>` : '';
      return `<tr>
        <td class="small">${esc(fmtTime(r.created_at))}</td>
        <td><b>${esc(r.alias || r.hostname || '')}</b> <span class="mono muted small">${esc(fmtId(r.device_id))}</span></td>
        <td class="small">${r.direction === 0 ? '⬇ с устройства' : r.direction === 1 ? '⬆ на устройство' : '—'}</td>
        <td class="mono small" style="word-break:break-all">${esc(r.path)}${n}</td>
        <td class="hide-m small">${esc(info.name || '')} <span class="mono muted">${esc(fmtId(r.peer_id))}</span></td>
      </tr>`;
    }).join('') : '<tr><td colspan="5" class="empty">Файлы не передавались</td></tr>';
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ пользователи
async function pageUsers(main) {
  main.innerHTML = `
    <div class="head"><h1>Пользователи</h1><button class="btn primary" id="add" style="margin-left:auto">+ Добавить</button></div>
    <p class="muted small" style="margin-top:-6px">Под этими учётками специалисты входят в программе (кнопка «Войти»), и у них появляется список устройств. Администраторы видят все устройства и могут входить в эту панель.</p>
    <div class="card tbl"><table>
      <thead><tr><th>Логин</th><th class="hide-m">Имя</th><th>Роль</th><th class="hide-m">Устройств</th><th class="hide-m">Последний вход</th><th></th></tr></thead>
      <tbody id="tb"></tbody></table></div>`;
  const load = async () => {
    const rows = await api('/users');
    $('#tb').innerHTML = rows.map(u => `
      <tr>
        <td><b>${esc(u.name)}</b>${u.status !== 1 ? ' <span class="badge gray">отключён</span>' : ''}</td>
        <td class="hide-m">${esc(u.display_name)}<div class="muted small">${esc(u.email)}</div></td>
        <td>${u.is_admin ? '<span class="badge">администратор</span>' : '<span class="badge gray">специалист</span>'}</td>
        <td class="hide-m">${u.devices}</td>
        <td class="hide-m small">${esc(fmtTime(u.last_login))}</td>
        <td><div class="actions"><button class="btn sm" data-e="${u.id}">Изменить</button>${u.name !== state.me.name ? `<button class="btn sm danger" data-d="${u.id}">Удалить</button>` : ''}</div></td>
      </tr>`).join('');
    document.querySelectorAll('[data-e]').forEach(b => b.onclick = () => edit(rows.find(u => u.id == b.dataset.e)));
    document.querySelectorAll('[data-d]').forEach(b => b.onclick = async () => {
      const u = rows.find(x => x.id == b.dataset.d);
      if (await confirmDlg(`Удалить пользователя <b>${esc(u.name)}</b>?`)) {
        try { await api('/users/' + u.id, { method: 'DELETE' }); toast('Удалено'); load(); } catch (e) { toast(e.message); }
      }
    });
  };
  const form = (u = {}) => `
    <h2>${u.id ? 'Пользователь ' + esc(u.name) : 'Новый пользователь'}</h2>
    ${u.id ? '' : '<label>Логин</label><input name="name" required pattern="[\\w.\\-@]{2,64}">'}
    <label>Имя</label><input name="display_name" value="${esc(u.display_name)}">
    <label>E-mail</label><input name="email" type="email" value="${esc(u.email)}">
    <label>${u.id ? 'Новый пароль (оставьте пустым, чтобы не менять)' : 'Пароль'}</label>
    <input name="password" type="password" minlength="8" autocomplete="new-password" ${u.id ? '' : 'required'}>
    <label>Роль</label><select name="is_admin"><option value="0">Специалист</option><option value="1" ${u.is_admin ? 'selected' : ''}>Администратор</option></select>
    ${u.id ? `<label>Статус</label><select name="status"><option value="1">Активен</option><option value="0" ${u.status !== 1 ? 'selected' : ''}>Отключён</option></select>` : ''}`;
  const edit = (u) => dialog(form(u), async (f) => {
    const body = { display_name: f.display_name.value, email: f.email.value, is_admin: f.is_admin.value === '1', status: +f.status.value };
    if (f.password.value) body.password = f.password.value;
    await api('/users/' + u.id, { method: 'PUT', body }); toast('Сохранено'); load();
  });
  $('#add').onclick = () => dialog(form(), async (f) => {
    await api('/users', { method: 'POST', body: { name: f.name.value, display_name: f.display_name.value, email: f.email.value, password: f.password.value, is_admin: f.is_admin.value === '1' } });
    toast('Пользователь создан'); load();
  });
  await load().catch(e => toast(e.message));
}

// ------------------------------------------------------------------ подключение
async function pageConnect(main) {
  const d = await api('/server');
  const link = location.origin + '/';
  main.innerHTML = `
    <div class="head"><h1>Подключение клиентов</h1></div>
    <div class="card" style="margin-bottom:14px">
      <h2>Ссылка для сотрудников</h2>
      <p class="muted small" style="margin-top:0">Отправьте её пользователю: там кнопка скачивания под его систему и короткая инструкция.</p>
      <div class="row"><code class="block grow">${esc(link)}</code><button class="btn" id="cl">Копировать</button></div>
      <h3 style="margin-top:18px">Установочные файлы</h3>
      ${d.downloads.length ? `<table><tbody>${d.downloads.map(f => `<tr><td>${esc(f.platform)}</td><td class="mono small">${esc(f.name)}</td><td class="small muted">${(f.size / 1048576).toFixed(1)} МБ</td></tr>`).join('')}</tbody></table>`
      : '<p class="muted small">Пока пусто. Положите собранные клиенты в папку <span class="mono">server/downloads</span> на сервере — они появятся здесь и на странице загрузки.</p>'}
    </div>
    <div class="card">
      <h2>Параметры сервера</h2>
      <p class="muted small" style="margin-top:0">В фирменной сборке они уже вшиты. Для стандартного клиента: Настройки → Сеть → Сервер ID/ретрансляции.</p>
      <div class="kv">
        <div class="k">ID-сервер</div><div class="v mono copy">${esc(d.id_server)}</div>
        <div class="k">Сервер ретрансляции</div><div class="v mono copy">${esc(d.relay_server)}</div>
        <div class="k">API-сервер</div><div class="v mono copy">${esc(d.api_server)}</div>
        <div class="k">Ключ</div><div class="v mono copy">${esc(d.key || '— ключ не найден: проверьте, что hbbs запущен')}</div>
        <div class="k">Схема ссылок</div><div class="v mono">${esc(d.uri_scheme)}://connect/&lt;ID&gt;</div>
      </div>
      <label>Строка конфигурации (импорт в клиенте одной вставкой)</label>
      <code class="block copy">${esc(d.config)}</code>
    </div>`;
  $('#cl').onclick = () => copy(link);
  main.querySelectorAll('.copy').forEach(el => el.onclick = () => copy(el.textContent));
}

// ------------------------------------------------------------------ аккаунт
async function pageAccount(main) {
  main.innerHTML = `
    <div class="head"><h1>Мой аккаунт</h1></div>
    <form class="card" id="pf" style="max-width:420px">
      <h2>Смена пароля</h2>
      <label>Текущий пароль</label><input type="password" name="old" required autocomplete="current-password">
      <label>Новый пароль</label><input type="password" name="new" required minlength="8" autocomplete="new-password">
      <label>Повторите новый пароль</label><input type="password" name="new2" required minlength="8" autocomplete="new-password">
      <div class="err" id="pe"></div>
      <button class="btn primary">Сменить пароль</button>
    </form>`;
  $('#pf').onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.new.value !== f.new2.value) { $('#pe').textContent = 'Пароли не совпадают'; return; }
    try { await api('/me/password', { method: 'POST', body: { old: f.old.value, new: f.new.value } }); f.reset(); $('#pe').textContent = ''; toast('Пароль изменён'); }
    catch (err) { $('#pe').textContent = err.message; }
  };
}

boot();
