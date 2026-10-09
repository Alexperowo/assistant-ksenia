// Центр управления Ксенией: «Управление», «Память», «Состояние». Для человека, а не программиста:
// всё словами, крупно; каждое действие — короткое «готово» на экране и для TalkBack.
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const KS = () => window.KS;
  let data = null, view = 'talk', timer = null, busy = false;

  // ---------- маленькие помощники разметки (без innerHTML для данных: текст из памяти — не код) ----------
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else if (v !== false && v != null) e.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    return e;
  }
  const card = (title, ...kids) => el('section', { class: 'card' }, el('h2', { text: title }), ...kids);
  const pill = (ok, word) => el('span', { class: 'pill ' + (ok === true ? 'ok' : ok === false ? 'bad' : 'warn'), text: word });

  function toast(text) {
    const t = $('toast');
    t.textContent = text; t.hidden = false;
    t.style.animation = 'none'; void t.offsetWidth; t.style.animation = '';
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
  }

  async function act(body, opts) {
    if (busy && !(opts && opts.parallel)) return null;
    busy = true;
    try {
      const r = await KS().api('/api/control/act', body);
      const say = (r.data && r.data.say) || (r.ok ? 'Готово.' : 'Не получилось.');
      toast(say);
      KS().signal(r.ok ? 'done' : 'error');
      await refresh();
      return r;
    } finally { busy = false; }
  }

  // ---------- элементы управления ----------
  function switchRow(label, sub, checked, onChange) {
    const input = el('input', { type: 'checkbox', role: 'switch' });
    input.checked = !!checked;
    input.addEventListener('change', () => onChange(input.checked));
    return el('label', { class: 'switch-row' }, el('span', { class: 'txt' }, el('span', { text: label }), sub ? el('span', { class: 'sub', text: sub }) : null), input);
  }
  function segmented(label, sub, options, value, onPick) {
    const name = 'seg-' + Math.random().toString(36).slice(2);
    return el('div', {},
      el('p', { class: 'field-label' }, label, sub ? el('span', { class: 'sub', text: sub }) : null),
      el('div', { class: 'seg', role: 'radiogroup', 'aria-label': label },
        options.map(([v, text]) => {
          const input = el('input', { type: 'radio', name });
          input.checked = String(v) === String(value);
          input.addEventListener('change', () => { if (input.checked) onPick(v); });
          return el('label', { class: 'seg-opt' }, input, el('span', { text }));
        })));
  }
  function settingControl(s) {
    if (s.type === 'bool') return switchRow(s.label, s.hint, s.value, (v) => act({ action: 'set', key: s.key, value: v }));
    return segmented(s.label, s.hint, s.options, s.value, (v) => act({ action: 'set', key: s.key, value: v }));
  }
  const settingsIn = (group) => data.settings.filter((s) => s.group === group).map(settingControl);

  // ---------- «Управление» ----------
  function renderControl() {
    const h = data.headset || {};
    const vol = data.volume;
    const m = data.music || {};
    const body = $('control-body');
    body.replaceChildren(
      card('Разговор',
        el('div', { class: 'two' },
          el('button', { class: 'btn primary', type: 'button', onclick: () => act({ action: 'talk' }) }, 'Слушай меня в наушниках'),
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'stop' }) }, 'Замолчи')),
        ...settingsIn('talk')),
      card('Наушники и звук',
        el('p', {}, h.connected ? pill(true, h.mode === 'talk' ? 'Режим разговора' : 'Режим музыки') : pill(false, 'Не подключены')),
        segmented('Режим наушников', 'Разговор — слышу всегда и можно перебивать; музыка — звук лучше, слушаю после сигнала',
          [['talk', 'Разговор'], ['music', 'Музыка']], h.mode || '', (v) => act({ action: 'headset_mode', mode: v })),
        el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'headset_fix' }) }, 'Проверить и починить звук'),
        el('p', { class: 'field-label' }, 'Громкость компьютера', el('span', { class: 'sub', text: vol != null ? `сейчас ${vol}%` + (data.muted ? ', звук выключен' : '') : '' })),
        el('div', { class: 'two' },
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'volume', step: 'down' }) }, 'Тише'),
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'volume', step: 'up' }) }, 'Громче'))),
      card('Музыка',
        el('p', {}, m.station ? `${m.paused ? 'На паузе' : 'Играет'}: ${m.station}` : 'Сейчас ничего не играет'),
        m.station ? el('div', { class: 'two' },
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'music', do: m.paused ? 'resume' : 'pause' }) }, m.paused ? 'Продолжить' : 'Пауза'),
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'music', do: 'stop' }) }, 'Выключить')) : null,
        m.station ? el('div', { class: 'two' },
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'music', do: 'volume_down' }) }, 'Музыку тише'),
          el('button', { class: 'btn', type: 'button', onclick: () => act({ action: 'music', do: 'volume_up' }) }, 'Музыку громче')) : null),
      card('Чужие голоса',
        segmented('Когда говорит не Александр', null,
          [['guest', 'Гость: говорю, но не выполняю'], ['owner_only', 'Слушаю только тебя']],
          data.voice_mode, (v) => act({ action: 'voice_mode', mode: v })),
        el('p', {}, data.voice_enrolled ? pill(true, 'Образец твоего голоса записан') :
          pill(null, data.voice_enrolling ? `Записываю образец: осталось фраз — ${data.voice_enrolling}` : 'Образец голоса не записан')),
        el('p', { class: 'hint left', text: 'Без образца я не отличаю твой голос от чужого — режимы начнут работать после записи.' }),
        data.voice_enrolled
          ? el('button', { class: 'btn danger-ghost', type: 'button', onclick: () => { if (confirm('Удалить образец твоего голоса?')) act({ action: 'voice_clear' }); } }, 'Удалить образец голоса')
          : el('button', { class: 'btn primary', type: 'button', onclick: () => act({ action: 'voice_enroll' }) }, 'Записать образец голоса')),
      card('Ночь', ...settingsIn('night')),
      card('Сообщения и напоминания', ...settingsIn('messages')),
      card('Дневник', ...settingsIn('memory')));
  }

  // ---------- «Память» ----------
  function listOrEmpty(items, empty, render) {
    return items.length ? el('ul', { class: 'list' }, items.map(render)) : el('p', { class: 'empty', text: empty });
  }
  function renderMemory() {
    const factInput = el('input', { type: 'text', 'aria-label': 'Новый факт', placeholder: 'Например: люблю чай без сахара' });
    const ruleInput = el('input', { type: 'text', 'aria-label': 'От кого', placeholder: 'Имя или чат, как во ВКонтакте' });
    $('memory-body').replaceChildren(
      card('Что я помню о тебе',
        listOrEmpty(data.memory, 'Пока ничего.', (f) => el('li', {}, el('span', { class: 'grow', text: f }),
          el('button', { class: 'btn small', type: 'button', 'aria-label': 'Забыть: ' + f,
            onclick: () => { if (confirm('Забыть: «' + f + '»?')) act({ action: 'memory_forget', fact: f }); } }, 'Забыть'))),
        el('div', { class: 'add-row' }, factInput,
          el('button', { class: 'btn small', type: 'button', onclick: () => { if (factInput.value.trim()) act({ action: 'memory_add', fact: factInput.value }); } }, 'Запомнить'))),
      card('О ком сообщать сразу',
        listOrEmpty(data.rules, 'Никого — о сообщениях говорю только по вопросу «что пришло?».', (r) => el('li', {},
          el('span', { class: 'grow' }, r.who, el('span', { class: 'sub', text: (r.source === 'vk' ? 'ВКонтакте' : 'Telegram') + (r.remind ? ', напоминаю, пока не прочитаешь' : '') })),
          el('button', { class: 'btn small', type: 'button', 'aria-label': 'Убрать правило: ' + r.who, onclick: () => act({ action: 'rule_remove', who: r.who }) }, 'Убрать'))),
        el('div', { class: 'add-row' }, ruleInput,
          el('button', { class: 'btn small', type: 'button', onclick: () => { if (ruleInput.value.trim()) act({ action: 'rule_add', who: ruleInput.value, remind: true }); } }, 'Добавить'))),
      card('Напоминания',
        listOrEmpty(data.reminders, 'Напоминаний нет.', (r) => el('li', {},
          el('span', { class: 'grow' }, r.text, el('span', { class: 'sub', text: r.when })),
          el('button', { class: 'btn small', type: 'button', 'aria-label': 'Отменить: ' + r.text, onclick: () => act({ action: 'reminder_cancel', id: r.id }) }, 'Отменить')))),
      card('Дневник разговоров',
        listOrEmpty(data.diary, 'Записей пока нет — появятся после разговоров.', (d) => el('li', {},
          el('span', { class: 'grow' }, d.summary, el('span', { class: 'sub', text: d.date + (d.followup ? ' · спросить: ' + d.followup : '') })))),
        data.diary.length ? el('button', { class: 'btn danger-ghost', type: 'button', onclick: () => { if (confirm('Очистить дневник?')) act({ action: 'diary_clear' }); } }, 'Очистить дневник') : null),
      card('Заметки для разработчика',
        el('p', { class: 'hint left', text: 'Скажи Ксении «Заметка: …» — замечание попадёт сюда.' }),
        listOrEmpty(data.notes, 'Заметок нет.', (n) => el('li', {}, el('span', { class: 'grow' }, n.text, el('span', { class: 'sub', text: n.when })))),
        data.notes.length ? el('button', { class: 'btn danger-ghost', type: 'button', onclick: () => { if (confirm('Убрать заметки в архив?')) act({ action: 'notes_clear' }); } }, 'Убрать заметки в архив') : null));
  }

  // ---------- «Состояние» ----------
  let lastCheck = null;
  function renderStatus() {
    const k = data.ksenia || {};
    const word = k.speaking ? 'Говорит' : k.busy ? 'Думает' : k.conversation ? 'Слушает' : 'Отдыхает';
    $('status-body').replaceChildren(
      card('Ксения сейчас', el('p', {}, pill(true, word)),
        el('button', { class: 'btn primary', type: 'button', onclick: async () => {
          const r = await act({ action: 'selfcheck' });
          if (r && r.data) { lastCheck = r.data; renderStatus(); }
        } }, 'Проверь себя'),
        lastCheck ? el('ul', { class: 'list' },
          (lastCheck.problems || []).map((p) => el('li', {}, pill(false, 'Проблема'), el('span', { class: 'grow', text: p }))),
          (lastCheck.fine || []).map((p) => el('li', {}, pill(true, 'Хорошо'), el('span', { class: 'grow', text: p })))) : null),
      card('Части Ксении',
        el('ul', { class: 'list' }, data.services.map((s) => el('li', {},
          el('span', { class: 'grow', text: s.label }), pill(s.ok, s.word),
          el('button', { class: 'btn small', type: 'button', 'aria-label': 'Перезапустить: ' + s.label,
            onclick: () => { if (confirm('Перезапустить «' + s.label + '»?')) act({ action: 'restart', unit: s.unit }); } }, 'Перезапустить'))))),
      data.report ? el('details', { class: 'card' }, el('summary', { text: 'Сводка за неделю' }), el('pre', { class: 'report', text: data.report.text })) : null);
  }

  function render() {
    if (!data) return;
    if (view === 'control') renderControl();
    else if (view === 'memory') renderMemory();
    else if (view === 'status') renderStatus();
  }
  async function refresh() {
    const r = await KS().api('/api/control/state');
    if (!r.ok) { if (view !== 'talk') $(view + '-body').replaceChildren(el('p', { class: 'empty', text: 'Компьютер не отвечает. Попробую ещё раз.' })); return; }
    // не перерисовывать, пока человек вводит текст или держит фокус в элементе — иначе собьётся
    const focused = document.activeElement;
    const typing = focused && focused.tagName === 'INPUT' && focused.type === 'text';
    data = r.data;
    if (!typing) {
      const label = focused && focused.getAttribute && (focused.getAttribute('aria-label') || focused.textContent);
      render();
      if (label && view !== 'talk') { // вернуть фокус на ту же кнопку после перерисовки (TalkBack не теряет место)
        const same = [...document.querySelectorAll('#view-' + view + ' button, #view-' + view + ' input')]
          .find((b) => (b.getAttribute('aria-label') || b.textContent) === label);
        if (same) same.focus({ preventScroll: true });
      }
    }
  }
  window.KSControl = {
    onView(name) {
      view = name;
      clearInterval(timer);
      if (name !== 'talk') { refresh(); timer = setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 6000); }
    },
  };
})();
