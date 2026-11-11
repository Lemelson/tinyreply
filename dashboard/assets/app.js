/* Автоответ: панель. Статическая страница, ничего не отправляет на сервер.
   Под /panel/ (web.py, вход по паролю) читает настоящие данные data/report.json и переводит их в ту же форму,
   что у демо (fromReal); иначе, или с ?demo=..., читает придуманные data/demo.json.
   Любой текст из данных проходит через esc(): здесь настоящие комментарии и имена. */
(() => {
  'use strict'

  const $ = (sel, root = document) => root.querySelector(sel)
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))

  let D = null
  const S = { scenario: 'attention', q: '', variant: {}, phone: 'dm', draft: {}, lastFocus: null }

  // ---------- язык интерфейса ----------
  // ?lang=en|ru в адресе, иначе выбор этого браузера, иначе язык системы. Данные бота не переводятся, только оболочка.

  const LANGS = ['ru', 'en']
  function pickLang() {
    const asked = new URLSearchParams(location.search).get('lang')
    if (LANGS.includes(asked)) return asked
    try { const saved = localStorage.getItem('panel-lang'); if (LANGS.includes(saved)) return saved } catch (e) { /* приватный режим */ }
    return /^ru/i.test(navigator.language || '') ? 'ru' : 'en'
  }
  let LANG = pickLang()
  const tr = (ru, en) => (LANG === 'en' ? en : ru)

  // ---------- форматирование ----------

  const TZ = 'Europe/Moscow'
  let nf, fDay, fDate, fShort, fHour
  function setFormats() {
    const loc = tr('ru-RU', 'en-GB')
    nf = new Intl.NumberFormat(loc)
    fDay = new Intl.DateTimeFormat(loc, { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long' })
    fDate = new Intl.DateTimeFormat(loc, { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
    fShort = new Intl.DateTimeFormat(loc, { timeZone: TZ, day: 'numeric', month: 'short' })
    fHour = new Intl.DateTimeFormat(loc, { timeZone: TZ, hour: '2-digit', hour12: false })
  }
  setFormats()
  const n = (v) => (v == null || Number.isNaN(v) ? '—' : nf.format(v))
  const pct = (a, b, digits = 0) => (b ? (a / b * 100).toFixed(digits).replace('.', tr(',', '.')) + '%' : '—')
  const now = () => new Date(D.generated_at)
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1)
  const plural = (k, [one, few, many]) => {
    const a = Math.abs(k) % 100, b = a % 10
    return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many
  }
  // Русские три формы и английские две: pl(5, ['день', 'дня', 'дней'], ['day', 'days'])
  const pl = (k, ru, [one, other]) => (LANG === 'en' ? (Math.abs(k) === 1 ? one : other) : plural(k, ru))
  const enp = (k, one, other) => (Math.abs(k) === 1 ? one : other)   // английская форма внутри tr()
  const PEOPLE = ['человек', 'человека', 'человек']
  const RO = () => tr('Менять настройки — через агента', 'Settings are changed through the agent')
  const W = () => (D.real
    ? tr({ replied: 'Нажали кнопку', repliedShort: 'Нажали', replies: 'нажимают кнопку', person: 'Нажал кнопку «Получить»' },
      { replied: 'Tapped the button', repliedShort: 'Tapped', replies: 'tap the button', person: 'Tapped the “Get” button' })
    : tr({ replied: 'Ответили в директ', repliedShort: 'Ответили', replies: 'отвечают', person: 'Ответил вам в директ' },
      { replied: 'Replied by DM', repliedShort: 'Replied', replies: 'reply', person: 'Replied to you by DM' }))
  function ago(iso) {
    return agoSec((now() - new Date(iso)) / 1000)
  }

  const ICON = {
    home: '<path d="M3.5 9.2 10 3.8l6.5 5.4V16a.8.8 0 0 1-.8.8h-3.4v-4.6H7.7v4.6H4.3a.8.8 0 0 1-.8-.8z"/>',
    reels: '<rect x="3" y="3.5" width="14" height="13" rx="3"/><path d="M8.6 7.7v4.6l3.9-2.3z"/>',
    people: '<circle cx="10" cy="7" r="3"/><path d="M4 16.5c.6-3 2.9-4.5 6-4.5s5.4 1.5 6 4.5"/>',
    chart: '<path d="M4 16V9.5M10 16V4M16 16v-4.5"/>',
    settings: '<path d="M3.5 6h6M13.5 6h3M3.5 14h3M10.5 14h6"/><circle cx="11.5" cy="6" r="2"/><circle cx="8.5" cy="14" r="2"/>',
    link: '<path d="M8.5 11.5a3 3 0 0 0 4.2 0l2.4-2.4a3 3 0 0 0-4.2-4.2l-.9.9M11.5 8.5a3 3 0 0 0-4.2 0l-2.4 2.4a3 3 0 0 0 4.2 4.2l.9-.9"/>',
    key: '<circle cx="7" cy="13" r="3"/><path d="m9.2 10.8 6.3-6.3M13 7l2 2"/>',
    typo: '<path d="M4 15 8 5l4 10M5.6 11.5h4.8M14 9.5c2.6-1.2 3.6 1 2.5 2.5-1 1.300-3 1-3 2.300 0 1.100 2.500 1.500 3.500-.3"/>',
    video: '<rect x="3" y="5" width="10" height="10" rx="2"/><path d="m13 9 4-2.500v7L13 11z"/>',
    words: '<path d="M4 6h12M4 10h12M4 14h7"/>',
    close: '<path d="m5 5 10 10M15 5 5 15"/>',
  }
  const icon = (name) => `<svg viewBox="0 0 20 20" aria-hidden="true">${ICON[name]}</svg>`

  const STATUS = () => ({
    pending: { label: tr('В очереди', 'In queue'), tone: 'blue', hint: tr('Ждёт своей очереди. Бот отправляет не чаще 60 сообщений в час, чтобы Instagram не посчитал это спамом.', 'Waiting its turn. The bot sends at most 60 messages an hour so Instagram doesn’t treat it as spam.') },
    sent: { label: tr('Отправлено', 'Sent'), tone: 'green', hint: tr('Ссылка отправлена в директ, под комментарием оставлен ответ.', 'The link went out by DM, and a reply was left under the comment.') },
    uncertain: { label: tr('Нужна проверка', 'Needs a check'), tone: 'amber', hint: tr('Instagram не подтвердил доставку. Сообщение могло дойти, а могло и нет. Бот не пишет второй раз, чтобы человек не получил два сообщения. Откройте директ и посмотрите.', 'Instagram didn’t confirm delivery. The message may or may not have arrived. The bot won’t write again, so the person doesn’t get two messages. Open your DMs and take a look.') },
    expired: { label: tr('Не успели', 'Too late'), tone: 'red', hint: tr('С комментария прошло больше 7 дней. После этого срока Instagram не разрешает написать человеку первым.', 'More than 7 days have passed since the comment. After that, Instagram doesn’t let you message someone first.') },
    refused: { label: tr('Отказ Instagram', 'Refused by Instagram'), tone: 'red', hint: tr('Instagram отклонил сообщение: человек закрыл личные сообщения или ограничил аккаунт. Повторять бот не будет.', 'Instagram rejected the message: the person has closed their DMs or restricted the account. The bot won’t retry.') },
    already: { label: tr('Вы ответили сами', 'You replied yourself'), tone: '', hint: tr('Под этим комментарием уже есть ваш ответ, поэтому бот не стал писать.', 'There’s already your reply under this comment, so the bot didn’t write.') },
    dup_user: { label: tr('Уже получил', 'Already got it'), tone: '', hint: tr('Этот человек уже получил ссылку по другому своему комментарию. Второй раз бот не пишет.', 'This person already got the link from another comment of theirs. The bot doesn’t write twice.') },
    near: { label: tr('Похоже на опечатку', 'Looks like a typo'), tone: 'purple', hint: tr('Комментарий похож на кодовое слово, но не совпал. Если это опечатка, её можно добавить в список «Засчитывать тоже».', 'The comment looks like the code word but doesn’t match. If it’s a typo, you can add it to the “Also count” list.') },
  })
  const FILTERS = () => [['all', tr('Все', 'All')], ['pending', tr('В очереди', 'In queue')], ['sent', tr('Отправлено', 'Sent')], ['uncertain', tr('Нужна проверка', 'Needs a check')], ['expired', tr('Не успели', 'Too late')], ['refused', tr('Отказ', 'Refused')], ['near', tr('Опечатки', 'Typos')]]
  const NAV = () => [['#/', tr('Обзор', 'Overview'), 'home'], ['#/reels', tr('Ролики', 'Reels'), 'reels'], ['#/people', tr('Люди', 'People'), 'people'], ['#/analytics', tr('Аналитика', 'Analytics'), 'chart'], ['#/settings', tr('Настройки', 'Settings'), 'settings']]

  const tag = (text, tone = '') => `<span class="tag${tone ? ' tag-' + tone : ''}"><i></i>${esc(text)}</span>`
  const statusTag = (st) => tag((STATUS()[st] || { label: st }).label, (STATUS()[st] || {}).tone)
  const quote = (s) => tr(`«${s}»`, `“${s}”`)
  const ON = () => tag(tr('Работает', 'Active'), 'green'), OFF = () => tag(tr('Выключен', 'Off'))
  const initials = (name) => esc(String(name).replace(/[^a-zа-яё0-9]/gi, '').slice(0, 1) || '?')
  const avatar = (name, size = '') => `<span class="avatar ${size}" style="background:hsl(${hue(name)} 32% 46%)" aria-hidden="true">${initials(name)}</span>`
  const hue = (s) => [...String(s)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7)
  const thumb = (a, size = '') => `<span class="thumb ${size}" style="--h:${Number(a.hue) || 220}" aria-hidden="true"></span>`
  const demo = (msg) => `data-action="demo" data-msg="${esc(D && D.real ? RO() : msg)}"`

  // ---------- данные ----------

  const sc = () => D.scenarios[S.scenario]
  const auto = (id) => D.automations.find((a) => a.id === id)
  function totals() {
    const t = { comments: 0, asked: 0, sent: 0, waiting: 0, expired: 0, uncertain: 0, refused: 0, already: 0, replied: 0, clicked: 0 }
    for (const a of D.automations) for (const k in t) t[k] += a.stats[k] || 0
    return t
  }
  function health() {
    const s = sc().service, problems = sc().problems
    if (!s.running) return { tone: 'red', title: tr('Бот остановлен', 'The bot is stopped'), text: tr('ничего не читает и не отправляет', 'not reading or sending anything') }
    const bad = problems.find((p) => p.level === 'bad')
    if (bad) return { tone: 'red', title: tr('Бот не отправляет', 'The bot isn’t sending'), text: tr('нужно ваше действие', 'needs your action') }
    if (problems.some((p) => p.level === 'warn')) return { tone: 'amber', title: tr('Бот работает', 'The bot is running'), text: tr('нужна ваша проверка', 'needs your check') }
    return { tone: 'green', title: tr('Бот работает', 'The bot is running'), text: tr('всё в порядке', 'all good') }
  }
  const budgetText = (s) => (s.budget === 'unlimited' ? tr('без ограничения', 'no limit') : tr(`осталось ${n(s.budget)} из ${n(s.armed_n)}`, `${n(s.budget)} of ${n(s.armed_n)} left`))
  function draft(id) {
    if (!S.draft[id]) {
      S.draft[id] = id === 'new'
        ? { id: 'new', isNew: true, name: '', permalink: '', active: false, hue: 150, caption: '', keywords: [], also_match: [], max_words: 5, link: '',
            ab: { enabled: false }, variants: [{ id: 'A', share: 100, sent: 0, replied: 0, clicked: 0, text: '{greeting} ' }],
            stats: { comments: 0, asked: 0, sent: 0, waiting: 0, expired: 0, uncertain: 0, refused: 0, already: 0, replied: 0, clicked: 0 } }
        : JSON.parse(JSON.stringify(auto(id)))
    }
    return S.draft[id]
  }
  function message(a, v) {
    const body = String(v.text).replaceAll('{greeting}', D.limits.greeting).replaceAll('{link}', a.link)
    const linkLine = a.link && !body.includes(a.link) ? a.link + '\n' : ''
    return linkLine ? `${body.trim()}\n\n${linkLine.trim()}` : body.trim()   // как бот: текст как написан, ссылка в конце, если её нет
  }
  const linkify = (text) => esc(text).replace(/https?:\/\/[^\s<]+/g, (u) => `<a>${u}</a>`)

  // ---------- общие куски ----------

  function callouts() {
    const problems = sc().problems
    if (!problems.length) {
      return `<div class="callouts"><div class="callout callout-ok">${calloutIcon('ok')}<div class="callout-body">
        <h3>${tr('Всё в порядке', 'All good')}</h3><p>${tr('Бот читает комментарии и отправляет ссылки. От вас ничего не нужно.', 'The bot is reading comments and sending links. Nothing is needed from you.')}</p></div></div></div>`
    }
    return `<div class="callouts">${problems.map((p) => `
      <div class="callout callout-${esc(p.level)}">${calloutIcon(p.level)}<div class="callout-body">
        <h3>${esc(p.title)}</h3>
        ${D.real ? (plainLine(p.text) ? `<p>${esc(plainLine(p.text))}</p>` : '') : `<p>${esc(p.text)}</p>`}
        <p class="do"><b>${tr('Что сделать:', 'What to do:')}</b> ${esc(p.action)}</p>
        ${p.link || D.real ? `<div class="callout-foot">
          ${p.link ? `<a class="btn btn-sm" href="${esc(p.link)}">${esc(p.link_text)}<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M5 3l4 4-4 4"/></svg></a>` : ''}
          ${D.real && p.text ? `<details class="callout-more"><summary>${tr('Подробности для агента', 'Details for the agent')}</summary><p>${esc(p.text)}</p></details>` : ''}
        </div>` : ''}
      </div></div>`).join('')}</div>`
  }

  const CALLOUT_ICON = {
    ok: '<circle cx="10" cy="10" r="7.5"/><path d="m6.8 10.2 2.2 2.2 4.2-4.6"/>',
    bad: '<circle cx="10" cy="10" r="7.5"/><path d="M10 6v4.6M10 13.6v.1"/>',
    warn: '<path d="M10 3.2 17.4 16H2.6z"/><path d="M10 8v3.6M10 13.8v.1"/>',
    info: '<circle cx="10" cy="10" r="7.5"/><path d="M10 9v4.6M10 6.4v.1"/>',
  }
  // Настоящий отчёт бота пишет для агента: команды, ключи, файлы. Хозяину показываем только первую фразу, если в ней нет техники.
  const plainLine = (text) => {
    const first = String(text || '').split(/(?<=[.!?])\s+(?=[А-ЯЁA-Z])/)[0] || ''
    return /igbot|[a-z]+_[a-z]+|\w\.(lock|env|json|db)\b|\(|\bc\d+/i.test(first) ? '' : first
  }
  const calloutIcon = (level) => `<span class="callout-icon" aria-hidden="true"><svg viewBox="0 0 20 20">${CALLOUT_ICON[level] || CALLOUT_ICON.info}</svg></span>`

  // mode: 'compact' (обзор), 'full' (список роликов), 'stats' (аналитика)
  function reelsTable(list, mode) {
    const compact = mode === 'compact', full = mode === 'full', rates = mode !== 'compact'
    return `<div class="table-wrap"><table>
      <thead><tr><th>${tr('Ролик', 'Reel')}</th>${full ? `<th class="hide-sm">${tr('Кодовое слово', 'Code word')}</th>` : ''}<th class="r">${tr('Попросили', 'Asked')}</th><th class="r">${tr('Отправлено', 'Sent')}</th>
        ${rates ? `<th class="r hide-sm">${W().repliedShort}</th>`  + `<th class="r hide-sm">${tr('Перешли', 'Clicked')}</th>` : ''}${mode === 'stats' ? '' : `<th class="r hide-sm">${tr('Статус', 'Status')}</th>`}</tr></thead>
      <tbody>${list.map((a) => `
        <tr class="rowlink" tabindex="0" data-action="go" data-href="#/reels/${esc(a.id)}">
          <td><span class="cell-main">${thumb(a)}<span>
            <span class="cell-title">${esc(a.name)}${a.ab && a.ab.enabled ? ` <span class="tag tag-blue">${tr('A/B-тест', 'A/B test')}</span>` : ''}</span>
            <span class="sub">${full ? esc(a.caption) : quote(esc(a.keywords[0]))}</span></span></span></td>
          ${full ? `<td class="hide-sm"><span class="chips">${a.keywords.map((k) => `<span class="tag">${esc(k)}</span>`).join('')}</span></td>` : ''}
          <td class="r">${n(a.stats.asked)}</td>
          <td class="r">${n(a.stats.sent)} <span class="faint">${pct(a.stats.sent, a.stats.asked)}</span></td>
          ${rates ? `<td class="r hide-sm">${n(a.stats.replied)} <span class="faint">${pct(a.stats.replied, a.stats.sent)}</span></td>
          <td class="r hide-sm">${n(a.stats.clicked)} <span class="faint">${pct(a.stats.clicked, a.stats.sent)}</span></td>` : ''}
          ${mode === 'stats' ? '' : `<td class="r hide-sm">${a.active ? ON() : OFF()}</td>`}
        </tr>`).join('')}</tbody></table></div>`
  }

  // Страница «Ролики»: карточка на ролик — обложка, слово, три числа воронки и куда уходят люди
  function reelCards(list) {
    return `<div class="reel-cards">${list.map((a) => {
      const st = a.stats, steps = [[tr('Попросили', 'Asked'), st.asked, ''], [tr('Получили', 'Got it'), st.sent, pct(st.sent, st.asked)], [tr('Перешли', 'Clicked'), st.clicked, pct(st.clicked, st.sent)]]
      return `<a class="reel-card${a.active ? '' : ' is-off'}" href="#/reels/${esc(a.id)}">
        <span class="reel-card-cover" style="--h:${Number(a.hue) || 220}"><span class="reel-card-play" aria-hidden="true"></span>
          <span class="reel-card-status">${a.active ? ON() : OFF()}</span></span>
        <span class="reel-card-body">
          <span class="reel-card-title">${esc(a.name)}${a.ab && a.ab.enabled ? ` <span class="tag tag-blue">${tr('A/B-тест', 'A/B test')}</span>` : ''}</span>
          <span class="reel-card-caption">${esc(a.caption)}</span>
          <span class="chips">${a.keywords.map((k) => `<span class="tag">${quote(esc(k))}</span>`).join('')}</span>
          <span class="reel-card-steps">${steps.map(([label, value, rate]) => `<span><b class="num">${n(value)}</b><em>${label}</em>${rate ? `<i>${rate}</i>` : ''}</span>`).join('')}</span>
          <span class="reel-card-bar" aria-hidden="true"><i style="width:${st.asked ? (st.sent / st.asked * 100).toFixed(1) : 0}%"></i><i style="width:${st.asked ? (st.clicked / st.asked * 100).toFixed(1) : 0}%"></i></span>
        </span></a>`
    }).join('')}</div>`
  }

  // Маленький график без осей: просьбы по дням за неделю
  function spark(values) {
    const w = 120, h = 34, max = Math.max(...values, 1), step = values.length > 1 ? w / (values.length - 1) : w
    const pts = values.map((v, i) => [Math.round(i * step * 10) / 10, Math.round((h - 3 - v / max * (h - 6)) * 10) / 10])
    const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x} ${y}`).join(' ')
    const [lx, ly] = pts[pts.length - 1]
    return `<svg class="spark" viewBox="-2 0 ${w + 6} ${h}" aria-hidden="true"><path class="spark-area" d="${line} L${w} ${h} L0 ${h} Z"/><path class="spark-line" d="${line}"/><circle cx="${lx}" cy="${ly}" r="3"/></svg>`
  }

  function hoursChart() {
    const height = 146, limit = D.limits.max_sends_per_hour, max = Math.max(Math.ceil(limit * 1.1), ...D.hours.map((h) => h.sent), 1)
    const bars = D.hours.map((h, i) => {
      const hour = fHour.format(new Date(h.hour)).slice(0, 2)
      const label = i % 4 === 3 ? `<span${i % 8 === 3 ? ' class="minor"' : ''}>${hour}:00</span>` : ''
      return `<div class="bar${i === D.hours.length - 1 ? ' now' : ''}" tabindex="0" data-tip="${hour}:00 · ${h.sent} ${tr('отправлено', 'sent')}"
        aria-label="${hour}:00, ${tr('отправлено', 'sent')} ${h.sent}"><i style="height:${Math.round(h.sent / max * 100)}%">${i === D.hours.length - 1 ? `<b>${h.sent}</b>` : ''}</i>${label}</div>`
    }).join('')
    return `<div class="chart" role="group" aria-label="${tr('Отправлено по часам', 'Sent per hour')}">
      <div class="chart-limit" style="top:${Math.round((1 - limit / max) * height)}px"><span>${tr(`предел ${limit} в час`, `limit ${limit} an hour`)}</span></div>${bars}</div>`
  }

  function funnel(rows) {
    const top = rows[0][1] || 1
    return `<div class="funnel">${rows.map(([label, value, note]) => `
      <div class="funnel-row"><span>${esc(label)}</span>
        <span class="funnel-bar"><i style="width:${Math.max(0.5, value / top * 100).toFixed(1)}%"></i></span>
        <span class="num"><b>${n(value)}</b> <span class="faint">${esc(note || '')}</span></span></div>`).join('')}</div>`
  }

  // ---------- A/B ----------

  function abVerdict(a) {
    const [x, y] = a.variants
    if (Math.min(x.sent, y.sent) < 100) return tr('Пока мало данных: нужно хотя бы по 100 отправок на вариант, чтобы сравнивать.', 'Not enough data yet: each variant needs at least 100 sends before you can compare.')
    const part = (key, what) => {
      const p1 = x[key] / x.sent, p2 = y[key] / y.sent, p = (x[key] + y[key]) / (x.sent + y.sent)
      const se = Math.sqrt(p * (1 - p) * (1 / x.sent + 1 / y.sent))
      const z = se ? (p2 - p1) / se : 0
      const [best, hi, lo] = z >= 0 ? [y, p2, p1] : [x, p1, p2]
      return { sure: Math.abs(z) >= 1.96, best, text: `${what} — ${Math.round(hi * 100)}% ${tr('против', 'vs')} ${Math.round(lo * 100)}%` }
    }
    const click = part('clicked', tr('переходят по ссылке', 'click the link')), reply = part('replied', W().replies)
    if (click.sure && reply.sure && click.best === reply.best) {
      return tr(`Вариант ${click.best.id} лучше: ${click.text}, ${reply.text}. Разница надёжная, случайностью её не объяснить.`,
        `Variant ${click.best.id} is better: ${click.text}, ${reply.text}. The difference is solid, not down to chance.`)
    }
    if (click.sure) return tr(`По переходам лучше вариант ${click.best.id}: ${click.text}. По ответам вывод делать рано.`, `Variant ${click.best.id} wins on clicks: ${click.text}. Too early to call on replies.`)
    if (reply.sure) return tr(`По ответам лучше вариант ${reply.best.id}: ${reply.text}. По переходам вывод делать рано.`, `Variant ${reply.best.id} wins on replies: ${reply.text}. Too early to call on clicks.`)
    return tr('Варианты пока идут почти вровень: разница может быть случайной. Пусть тест поработает ещё.', 'The variants are neck and neck so far: the difference may be chance. Let the test run a while longer.')
  }

  function abTable(a) {
    const best = (key) => a.variants.reduce((m, v) => (v.sent && v[key] / v.sent > m.rate ? { id: v.id, rate: v[key] / v.sent } : m), { id: null, rate: -1 }).id
    const lead = best('clicked')
    const cell = (v, key) => `<td class="r"><span class="meter"><em class="abs">${n(v[key])}</em> <b>${pct(v[key], v.sent)}</b>
      <span><i style="width:${v.sent ? Math.round(v[key] / v.sent * 100) : 0}%"></i></span></span></td>`
    return `<div class="ab"><div class="table-wrap" style="margin:0;padding:0"><table>
      <thead><tr><th>${tr('Вариант', 'Variant')}</th><th class="r">${tr('Отправлено', 'Sent')}</th><th class="r">${W().repliedShort}</th><th class="r">${tr('Перешли по ссылке', 'Clicked the link')}</th></tr></thead>
      <tbody>${a.variants.map((v) => `<tr class="${v.id === lead ? 'lead-row' : ''}">
        <td><span class="cell-title"><span class="variant-letter">${esc(v.id)}</span>${v.id === lead ? tag(tr('лидирует', 'leading'), 'green') : ''}</span></td>
        <td class="r">${n(v.sent)}</td>${cell(v, 'replied')}${cell(v, 'clicked')}</tr>`).join('')}</tbody></table></div>
      <div class="ab-note">${esc(abVerdict(a))}</div></div>`
  }

  // ---------- страницы ----------

  function viewHome() {
    const h = health(), t = totals(), s = sc().service, today = D.days[D.days.length - 1]
    const state = [
      [tr('Отправка', 'Sending'), s.sending ? tag(tr('включена', 'on'), 'green') : tag(tr('выключена', 'off'))],
      [tr('Разрешено отправок', 'Sends allowed'), `<span class="num">${esc(budgetText(s))}</span>`],
      [tr('Скорость', 'Speed'), tr(`до ${D.limits.max_sends_per_hour} в час`, `up to ${D.limits.max_sends_per_hour} an hour`)],
      [tr('Доступ к Instagram', 'Instagram access'), s.meta_ok ? tag(tr('есть', 'granted'), 'green') : tag(tr('закрыт', 'closed'), 'red')],
      [tr('Оповещения в Telegram', 'Telegram alerts'), s.telegram_ok ? tag(tr('подключены', 'connected'), 'green') : tag(tr('не настроены', 'not set up'), 'amber')],
      [tr('Подписчики', 'Followers'), `<span class="num">${n(D.account.followers.now)}${D.account.followers.week_ago == null ? '' : ` <span class="faint">${D.account.followers.now >= D.account.followers.week_ago ? '+' : ''}${n(D.account.followers.now - D.account.followers.week_ago)} ${tr('за неделю', 'this week')}</span>`}</span>`],
    ]
    const tone = { ok: 'green', warn: 'amber', bad: 'red' }
    const sentToday = today ? today.sent : 0
    return `
      <header class="hero">
        <div class="hero-main">
          <div class="eyebrow">${esc(cap(fDay.format(now())))}</div>
          <h1>${tr('Привет', 'Hi')}, ${esc(D.account.name)}</h1>
          <p class="lead"><span class="pill pill-${h.tone}"><span class="dot dot-${h.tone}"></span>${esc(h.title)} · ${esc(h.text)}</span>
            <span class="faint">${s.last_cycle_sec == null ? tr('Бот ещё не проверял комментарии', 'The bot hasn’t checked comments yet') : `${tr('Проверял комментарии', 'Checked comments')} ${esc(agoSec(s.last_cycle_sec))}`}</span></p>
        </div>
        <div class="hero-today" aria-label="${tr('Сегодня', 'Today')}">
          <span class="hero-today-label">${tr('Сегодня', 'Today')}</span>
          <span class="hero-today-value num">${n(sentToday)}</span>
          <span class="hero-today-sub">${pl(sentToday, ['сообщение отправлено', 'сообщения отправлено', 'сообщений отправлено'], ['message sent', 'messages sent'])} · ${tr('новых просьб', 'new requests')} ${n(today ? today.asked : 0)}</span>
        </div>
      </header>
      ${callouts()}
      <section class="stats" aria-label="${tr('Итоги за всё время', 'All-time totals')}">
        ${[[tr('Попросили ссылку', 'Asked for the link'), t.asked, tr(`+${n(today ? today.asked : 0)} сегодня`, `+${n(today ? today.asked : 0)} today`), null],
           [tr('Получили сообщение', 'Got the message'), t.sent, tr('от попросивших', 'of those who asked'), pct(t.sent, t.asked)],
           [tr('Перешли по ссылке', 'Clicked the link'), t.clicked, tr('от получивших', 'of those who got it'), pct(t.clicked, t.sent)],
           [W().replied, t.replied, tr('от получивших', 'of those who got it'), pct(t.replied, t.sent)]].map(([label, value, sub, rate], i) => `
        <div class="stat${i === 0 ? ' stat-lead' : ''}">
          <div class="stat-label">${esc(label)}</div>
          <div class="stat-value">${n(value)}</div>
          ${i === 0 ? `<div class="stat-spark">${spark(D.days.map((d) => d.asked))}<span>${tr('за неделю', 'this week')}</span></div>`
            : `<div class="stat-meter"><i style="width:${t.asked ? Math.max(2, value / t.asked * 100).toFixed(1) : 0}%"></i></div>`}
          <div class="stat-sub">${rate ? `<b>${rate}</b> ` : ''}${esc(sub)}</div>
        </div>`).join('')}
      </section>
      ${D.real ? realStats(t) : ''}
      <div class="substats">
        <a href="#/people?status=pending">${tr('В очереди', 'In queue')} <b>${n(t.waiting)}</b></a>
        <a href="#/people?status=uncertain">${tr('Нужна проверка', 'Needs a check')} <b>${n(t.uncertain)}</b></a>
        <a href="#/people?status=expired">${tr('Не успели', 'Too late')} <b>${n(t.expired)}</b></a>
        <a href="#/people?status=refused">${tr('Отказ Instagram', 'Refused by Instagram')} <b>${n(t.refused)}</b></a>
      </div>

      <section class="section">
        <div class="section-head"><h2>${tr('Отправлено за последние 24 часа', 'Sent in the last 24 hours')}</h2><span class="muted num">${n(D.hours.reduce((sum, x) => sum + x.sent, 0))} ${pl(D.hours.reduce((sum, x) => sum + x.sent, 0), ['сообщений', 'сообщений', 'сообщений'], ['message', 'messages'])}</span></div>
        ${hoursChart()}
      </section>

      <div class="cols section">
        <section>
          <div class="section-head"><h2>${tr('Ролики', 'Reels')}</h2><a href="#/reels">${tr('Все ролики', 'All reels')}</a></div>
          ${reelsTable(D.automations, 'compact')}
        </section>
        <section>
          <div class="section-head"><h2>${tr('Состояние', 'Status')}</h2><a href="#/settings">${tr('Настройки', 'Settings')}</a></div>
          <dl class="kv">${state.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>
        </section>
      </div>

      <section class="section">
        <div class="section-head"><h2>${tr('Последние события', 'Recent activity')}</h2></div>
        <ul class="feed">${D.activity.slice(0, 8).map((e) => `
          <li><span class="dot${tone[e.level] ? ' dot-' + tone[e.level] : ''}"></span><span>${esc(e.text)}</span><time>${esc(ago(e.at))}</time></li>`).join('')}</ul>
      </section>`
  }

  function viewReels() {
    return `
      <header class="page-head">
        <div><h1>${tr('Ролики', 'Reels')}</h1><p class="lead">${tr('Под каким роликом какое слово ждём и что отвечаем.', 'Which word we wait for under each reel, and what we reply.')}</p></div>
        ${D.real ? '' : `<a class="btn btn-primary" href="#/reels/new"><svg viewBox="0 0 14 14" aria-hidden="true"><path d="M7 2v10M2 7h10"/></svg>${tr('Новый ролик', 'New reel')}</a>`}
      </header>
      ${reelCards(D.automations)}
      <p class="note-box">${tr('Проще всего сказать агенту: «новый ролик такой-то, слово такое-то, ссылка такая-то» — он настроит сам.', 'The easiest way is to tell the agent: “new reel such-and-such, word such-and-such, link such-and-such” — it sets everything up.')} ${D.real ? tr('Здесь видно, что получилось.', 'Here you can see the result.') : tr('Здесь можно посмотреть, что получилось, и поправить руками.', 'Here you can see the result and tweak it by hand.')}</p>`
  }

  function chipField(a, key, placeholder) {
    return `<div class="field-box"><span class="chips" data-chips="${key}">${a[key].map((word, i) => `
      <span class="chip">${esc(word)}<button type="button" data-action="chip-del" data-key="${key}" data-i="${i}" aria-label="${tr('Убрать', 'Remove')} ${quote(esc(word))}">×</button></span>`).join('')}
      <input class="chip-input" data-chip-input="${key}" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" enterkeyhint="done"></span></div>`
  }

  function phone(a) {
    const v = a.variants.find((x) => x.id === S.variant[a.id]) || a.variants[0]
    const who = 'katya.ux', word = a.keywords[0] ? cap(a.keywords[0]) : tr('Слово', 'Word')
    const body = {
      reel: `<div class="ph-reel" style="--h:${Number(a.hue) || 220}">${esc(a.caption || tr('Подпись к ролику', 'Reel caption'))}</div>`,
      comments: `
        <div class="ph-comment">${avatar(who, 'avatar-sm')}<div><b>${who}</b> ${esc(word)} 🔥<div class="meta">${tr('2 мин · Ответить', '2 min · Reply')}</div></div></div>
        ${D.limits.public_reply ? `<div class="ph-comment ph-reply">${avatar(D.account.username, 'avatar-sm')}<div><b>${esc(D.account.username)}</b> ${esc(D.limits.public_phrases[0])}<div class="meta">${tr('1 мин · Ответить', '1 min · Reply')}</div></div></div>` : ''}
        <span class="ph-note">${tr('Так это видят все под роликом', 'Everyone under the reel sees this')}</span>`,
      dm: `<span class="ph-note">${tr('Сегодня, 14:03', 'Today, 14:03')}</span>${opening(a, v)
        ? `<div class="bubble" id="dm-bubble">${linkify(opening(a, v).text)}<span class="bubble-btn">${esc(opening(a, v).button)}</span></div>`
        : `<div class="bubble" id="dm-bubble">${linkify(message(a, v))}</div>`}`,
    }[S.phone]
    const tabs = [['reel', tr('Ролик', 'Reel')], ['comments', tr('Комментарии', 'Comments')], ['dm', tr('Директ', 'DM')]]
    return `<div class="phone-wrap">
      <div class="tabs" role="tablist" aria-label="${tr('Что показать в телефоне', 'What to show on the phone')}">${tabs.map(([id, label]) =>
        `<button class="tab" type="button" role="tab" aria-selected="${S.phone === id}" data-action="phone" data-tab="${id}">${label}</button>`).join('')}</div>
      <div class="phone" aria-label="${tr('Как это выглядит у человека', 'How the person sees it')}"><div class="phone-screen">
        <div class="phone-top">${avatar(D.account.username, 'avatar-sm')}${esc(D.account.username)}</div>
        <div class="phone-body">${body}</div></div></div>
      ${S.phone === 'dm' && a.variants.length > 1 ? `<span class="hint">${tr('Вариант', 'Variant')} ${esc(v.id)}</span>` : ''}
    </div>`
  }

  function viewReel(id) {
    if (D.real && id === 'new') return `<h1>${tr('Новый ролик', 'New reel')}</h1><p class="lead">${RO()}${tr(': скажите «новый ролик такой-то, слово такое-то, ссылка такая-то».', ': say “new reel such-and-such, word such-and-such, link such-and-such”.')}</p>`
    if (id !== 'new' && !auto(id)) return `<h1>${tr('Такого ролика нет', 'There’s no such reel')}</h1><p class="lead"><a class="link" href="#/reels">${tr('Ко всем роликам', 'All reels')}</a></p>`
    const a = draft(id)
    const current = a.variants.find((x) => x.id === S.variant[id]) || a.variants[0]
    S.variant[id] = current.id
    const many = a.variants.length > 1
    return `
      <nav class="crumbs" aria-label="${tr('Путь', 'Breadcrumb')}"><a href="#/reels">${tr('Ролики', 'Reels')}</a><span>/</span><span>${esc(a.name || tr('Новый ролик', 'New reel'))}</span></nav>
      <div class="reel-head">
        ${thumb(a, 'thumb-lg')}
        <div class="reel-head-text">
          <h1><input class="title-input" data-field="name" value="${esc(a.name)}" placeholder="${tr('Название ролика', 'Reel name')}" aria-label="${tr('Название ролика', 'Reel name')}"></h1>
          ${a.isNew ? '' : `<p class="reel-head-meta">${a.active ? ON() : OFF()}
            ${a.keywords[0] ? `<span>${tr('Слово', 'Word')} <b>${quote(esc(a.keywords[0]))}</b></span>` : ''}
            <span><b class="num">${n(a.stats.asked)}</b> ${tr('попросили', 'asked')}</span>
            <span><b class="num">${pct(a.stats.clicked, a.stats.sent)}</b> ${tr('переходят по ссылке', 'click the link')}</span>
            ${many ? `<span class="tag tag-blue">${tr('A/B-тест идёт', 'A/B test running')}</span>` : ''}</p>`}
        </div>
      </div>

      <dl class="props">
        <dt>${icon('settings')}${tr('Статус', 'Status')}</dt>
        <dd><button class="switch" type="button" role="switch" aria-checked="${a.active}" data-action="toggle" data-key="active"><span class="switch-track"></span><span>${a.active ? tr('Работает', 'Active') : tr('Выключен', 'Off')}</span></button></dd>
        <dt>${icon('video')}${tr('Ролик', 'Reel')}</dt>
        <dd>${a.isNew
          ? `<input class="input" data-field="permalink" value="${esc(a.permalink)}" placeholder="${tr('Ссылка на ролик в Instagram', 'Link to the reel on Instagram')}" aria-label="${tr('Ссылка на ролик в Instagram', 'Link to the reel on Instagram')}" inputmode="url">`
          : `<span class="muted">${esc(a.caption)}</span>`}</dd>
        <dt>${icon('key')}${tr('Кодовое слово', 'Code word')}</dt>
        <dd class="stack">${chipField(a, 'keywords', tr('Добавить слово', 'Add a word'))}
          <span class="hint">${tr('Большие и маленькие буквы, эмодзи, знаки и пара слов рядом не мешают: «АРЕНА!!», «арена 🔥», «хочу арена» засчитываются сами.', 'Capital letters, emoji, punctuation and a couple of extra words don’t matter: “ARENA!!”, “arena 🔥” and “want arena” all count on their own.')}</span></dd>
        <dt>${icon('typo')}${tr('Засчитывать тоже', 'Also count')}</dt>
        <dd class="stack">${chipField(a, 'also_match', tr('Добавить опечатку', 'Add a typo'))}</dd>
        <dt>${icon('link')}${tr('Ссылка', 'Link')}</dt>
        <dd><input class="input" data-field="link" value="${esc(a.link)}" placeholder="https://…" aria-label="${tr('Ссылка, которую получит человек', 'Link the person will get')}" inputmode="url"></dd>
      </dl>

      <section class="section">
        <div class="section-head"><h2>${tr('Что получит человек', 'What the person gets')}</h2>
          ${D.real ? `<span class="muted">${RO()}</span>` : `<button class="btn btn-primary" type="button" ${demo(tr('Демо: изменения пока не сохраняются на сервере', 'Demo: changes aren’t saved to the server yet'))}>${tr('Сохранить', 'Save')}</button>`}</div>
        <div class="editor">
          <ol class="steps">
            <li class="step"><span class="step-n">1</span><div>
              <h3>${tr('Сообщение в директ', 'Direct message')}</h3>
              <div class="step-body">
                <div class="variant-bar">
                  ${many ? `<div class="tabs" role="tablist" aria-label="${tr('Варианты сообщения', 'Message variants')}">${a.variants.map((v) =>
                    `<button class="tab" type="button" role="tab" aria-selected="${v.id === current.id}" data-action="variant" data-v="${esc(v.id)}">${tr('Вариант', 'Variant')} ${esc(v.id)} <span class="n">${v.share}%</span></button>`).join('')}</div>
                    ${D.real ? '' : `<button class="btn btn-ghost" type="button" data-action="variant-remove">${tr('Закончить тест', 'End test')}</button>`}`
                  : D.real ? '' : `<button class="btn" type="button" data-action="variant-add">${tr('Сравнить с другим текстом (A/B-тест)', 'Compare with another text (A/B test)')}</button>`}
                </div>
                ${D.real ? variantNote(a, current) : ''}
                <textarea class="textarea" data-field="text" aria-label="${tr('Текст сообщения, вариант', 'Message text, variant')} ${esc(current.id)}" rows="7">${esc(current.text)}</textarea>
                <span class="hint">${tr(`{greeting} заменится на приветствие «${esc(D.limits.greeting)}».`, `{greeting} is replaced with the greeting “${esc(D.limits.greeting)}”.`)}${opening(a, current) ? tr(' Ссылку человек получит, когда нажмёт кнопку.', ' The person gets the link after tapping the button.') : tr(' Если ссылки нет в тексте, бот добавит её в конец.', ' If the text has no link, the bot adds it at the end.')}${many ? (D.real ? tr(' Каждый человек всегда попадает в один и тот же вариант.', ' Each person always lands in the same variant.') : tr(' Людей бот делит между вариантами поровну и случайно.', ' The bot splits people between the variants evenly and at random.')) : ''}</span>
              </div></div></li>
            <li class="step"><span class="step-n">2</span><div>
              <h3>${tr('Ответ под комментарием', 'Reply under the comment')}</h3>
              <div class="step-body">
                <details class="phrases"><summary>${tr(`Одна из ${D.limits.public_phrases.length} фраз, каждый раз другая`, `One of ${D.limits.public_phrases.length} ${enp(D.limits.public_phrases.length, 'phrase', 'phrases')}, a different one each time`)}</summary>
                  <ol>${D.limits.public_phrases.map((p) => `<li>${esc(p)}</li>`).join('')}</ol></details>
                <span class="hint">${tr('Фразы общие для всех роликов, меняются в настройках.', 'The phrases are shared by all reels and changed in Settings.')}</span>
              </div></div></li>
            <li class="step"><span class="step-n">3</span><div>
              <h3>${tr('Правила, которые бот держит сам', 'Rules the bot keeps on its own')}</h3>
              <ul class="rules step-body" style="gap:0">
                <li>${tr('Одному человеку — одно сообщение, даже если он написал слово несколько раз.', 'One message per person, even if they wrote the word several times.')}</li>
                <li>${tr(`Не больше ${D.limits.max_sends_per_hour} сообщений в час.`, `No more than ${D.limits.max_sends_per_hour} messages an hour.`)}</li>
                <li>${tr(`Только в течение ${D.limits.window_days} дней после комментария: позже Instagram не разрешает.`, `Only within ${D.limits.window_days} days of the comment: after that, Instagram doesn’t allow it.`)}</li>
              </ul></div></li>
          </ol>
          <div id="phone">${phone(a)}</div>
        </div>
      </section>

      ${a.isNew ? '' : `
      <section class="section">
        <div class="section-head"><h2>${many ? tr('A/B-тест: какой текст работает лучше', 'A/B test: which text works better') : tr('Результат', 'Results')}</h2>
          ${many && a.ab.started_at ? `<span class="muted">${tr('идёт с', 'running since')} ${esc(fShort.format(new Date(a.ab.started_at)))}</span>` : ''}</div>
        ${many ? abTable(a) : ''}
        ${D.real && a.flow ? variantFunnels(a) : ''}
        <div style="margin-top:${many || (D.real && a.flow) ? 28 : 0}px">${funnel([
          [tr('Комментариев под роликом', 'Comments under the reel'), a.stats.comments, ''],
          [tr('С кодовым словом', 'With the code word'), a.stats.asked, pct(a.stats.asked, a.stats.comments) + tr(' от всех', ' of all')],
          [tr('Получили сообщение', 'Got the message'), a.stats.sent, pct(a.stats.sent, a.stats.asked) + tr(' от попросивших', ' of those who asked')],
          [tr('Перешли по ссылке', 'Clicked the link'), a.stats.clicked, pct(a.stats.clicked, a.stats.sent) + tr(' от получивших', ' of those who got it')],
          [W().replied, a.stats.replied, pct(a.stats.replied, a.stats.sent) + tr(' от получивших', ' of those who got it')],
        ])}</div>
      </section>`}`
  }

  function peopleRows() {
    const status = S.status || 'all', q = S.q.trim().toLowerCase()
    const list = D.people.filter((p) => (status === 'all' || p.status === status) &&
      (!q || p.username.toLowerCase().includes(q) || p.text.toLowerCase().includes(q)))
    if (!list.length) return `<tr><td colspan="5" class="empty">${tr('Никого не нашлось', 'Nobody found')}</td></tr>`
    let last = ''
    return list.map((p) => {
      const group = dayGroup(p.ts), head = group !== last ? `<tr class="group-row"><th colspan="5" scope="colgroup">${esc(group)}</th></tr>` : ''
      last = group
      return head + personRow(p)
    }).join('')
  }
  function dayGroup(iso) {
    const key = (d) => new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d)
    const t = now(), day = key(new Date(iso))
    if (day === key(t)) return tr('Сегодня', 'Today')
    if (day === key(new Date(t - 864e5))) return tr('Вчера', 'Yesterday')
    return tr('Раньше', 'Earlier')
  }
  function personRow(p) {
    {
      const a = auto(p.automation)
      return `<tr class="rowlink" tabindex="0" data-action="person" data-id="${esc(p.comment_id)}">
        <td><span class="cell-main">${avatar(p.username)}<span class="cell-title">${esc(p.username)}</span></span></td>
        <td><span class="sub" style="color:inherit;font-size:14px;max-width:260px">${esc(p.text)}</span></td>
        <td class="hide-sm muted">${esc(a ? a.name : '')}${a && a.variants.length > 1 ? ` <span class="variant-letter">${esc(p.variant)}</span>` : ''}</td>
        <td>${statusTag(p.status)}</td>
        <td class="r muted nowrap">${esc(ago(p.ts))}</td></tr>`
    }
  }

  function viewPeople() {
    const count = (st) => (st === 'all' ? D.people.length : D.people.filter((p) => p.status === st).length)
    return `
      <header><h1>${tr('Люди', 'People')}</h1><p class="lead">${tr(`Кто написал кодовое слово и что с ним стало. Показаны последние ${D.people.length}.`, `Who wrote the code word and what happened next. Showing the latest ${D.people.length}.`)}</p></header>
      <div class="toolbar">
        <div class="tabs" role="group" aria-label="${tr('Показать', 'Show')}">${FILTERS().map(([id, label]) =>
          `<button class="tab" type="button" aria-pressed="${(S.status || 'all') === id}" data-action="status" data-status="${id}">${label} <span class="n">${count(id)}</span></button>`).join('')}</div>
        <input class="input search" id="search" type="search" placeholder="${tr('Имя или текст', 'Name or text')}" aria-label="${tr('Поиск по имени или тексту', 'Search by name or text')}" value="${esc(S.q)}">
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>${tr('Человек', 'Person')}</th><th>${tr('Комментарий', 'Comment')}</th><th class="hide-sm">${tr('Ролик', 'Reel')}</th><th>${tr('Что с ним', 'What happened')}</th><th class="r">${tr('Когда', 'When')}</th></tr></thead>
        <tbody id="people-rows">${peopleRows()}</tbody></table></div>`
  }

  function viewAnalytics() {
    const t = totals(), max = Math.max(...D.days.map((d) => Math.max(d.asked, d.sent)), 1)
    const tests = D.automations.filter((a) => a.variants.length > 1)
    const lost = [[tr('В очереди, ещё получат', 'In queue, will still get it'), t.waiting, 'pending'], [tr('Не успели за 7 дней', 'Missed the 7-day window'), t.expired, 'expired'], [tr('Вы ответили сами', 'You replied yourself'), t.already, ''],
      [tr('Отказ Instagram', 'Refused by Instagram'), t.refused, 'refused'], [tr('Нужна проверка', 'Needs a check'), t.uncertain, 'uncertain']]
    return `
      <header><h1>${tr('Аналитика', 'Analytics')}</h1><p class="lead">${tr('Что происходит с людьми после слова в комментарии. Всё время работы бота.', 'What happens to people after they comment the word. Since the bot started.')}</p></header>

      <section class="section" style="margin-top:32px">
        <div class="section-head"><h2>${tr('Путь человека', 'The person’s path')}</h2></div>
        ${funnel([
          [tr('Комментариев под роликами', 'Comments under reels'), t.comments, ''],
          [tr('С кодовым словом', 'With the code word'), t.asked, pct(t.asked, t.comments) + tr(' от всех', ' of all')],
          [tr('Получили сообщение', 'Got the message'), t.sent, pct(t.sent, t.asked) + tr(' от попросивших', ' of those who asked')],
          [tr('Перешли по ссылке', 'Clicked the link'), t.clicked, pct(t.clicked, t.sent) + tr(' от получивших', ' of those who got it')],
          [W().replied, t.replied, pct(t.replied, t.sent) + tr(' от получивших', ' of those who got it')],
        ])}
      </section>

      <section class="section">
        <div class="section-head"><h2>${tr('По дням, последняя неделя', 'By day, last week')}</h2>
          <span class="legend"><span><i class="l2"></i>${tr('попросили', 'asked')}</span><span><i></i>${tr('получили', 'got it')}</span></span></div>
        <div class="chart chart-duo" role="group" aria-label="${tr('По дням', 'By day')}">${D.days.map((d) => {
          const day = fShort.format(new Date(new Date(d.day).getTime() + 12 * 3600e3)).replace('.', '')
          return `<div class="bar" tabindex="0" data-tip="${esc(day)} · ${tr(`попросили ${d.asked}, получили ${d.sent}`, `asked ${d.asked}, got it ${d.sent}`)}" aria-label="${esc(day)}: ${tr(`попросили ${d.asked}, получили ${d.sent}`, `asked ${d.asked}, got it ${d.sent}`)}">
            <i style="height:${Math.round(d.asked / max * 100)}%"></i><i style="height:${Math.round(d.sent / max * 100)}%"></i><span>${esc(day)}</span></div>`
        }).join('')}</div>
      </section>

      <section class="section">
        <div class="section-head"><h2>${tr('По роликам', 'By reel')}</h2><a href="#/reels">${tr('Все ролики', 'All reels')}</a></div>
        ${reelsTable(D.automations, 'stats')}
      </section>

      ${tests.length ? `<section class="section">
        <div class="section-head"><h2>${tr('A/B-тесты', 'A/B tests')}</h2></div>
        ${tests.map((a) => `<p class="muted" style="margin-bottom:10px"><a class="link" href="#/reels/${esc(a.id)}">${esc(a.name)}</a></p>${abTable(a)}`).join('')}
      </section>` : ''}

      <section class="section">
        <div class="section-head"><h2>${tr('Кто попросил, но не получил', 'Asked but didn’t get it')}</h2><span class="muted num">${n(t.asked - t.sent)} ${tr('из', 'of')} ${n(t.asked)}</span></div>
        <dl class="kv">${lost.map(([label, value, st]) => `<dt>${st ? `<a href="#/people?status=${st}">${esc(label)}</a>` : esc(label)}</dt><dd class="num">${n(value)}</dd>`).join('')}</dl>
      </section>`
  }

  function viewSettings() {
    const s = sc().service, L = D.limits
    const stageName = (x) => (x === 'unlimited' ? tr('Без ограничения', 'No limit') : n(x))
    return `
      <header><h1>${tr('Настройки', 'Settings')}</h1><p class="lead">${tr('Общее для всех роликов.', 'Shared by all reels.')}</p></header>

      <section class="section" style="margin-top:32px">
        <dl class="props" style="margin-top:0">
          <dt>${tr('Язык интерфейса', 'Interface language')}</dt>
          <dd><span class="stage" role="group" aria-label="${tr('Язык интерфейса', 'Interface language')}">${[['ru', 'Русский'], ['en', 'English']].map(([id, label]) =>
            `<button class="tab" type="button" lang="${id}" aria-pressed="${LANG === id}" data-action="lang" data-lang="${id}">${label}</button>`).join('')}</span></dd>
        </dl>
      </section>

      <section class="section">
        <div class="section-head"><h2>${tr('Отправка', 'Sending')}</h2></div>
        <dl class="props" style="margin-top:0">
          <dt>${tr('Отправка сообщений', 'Sending messages')}</dt>
          <dd><button class="switch" type="button" role="switch" aria-checked="${s.sending}" ${demo(tr('Демо: отправку включает и выключает агент по вашей команде', 'Demo: the agent turns sending on and off when you ask'))}><span class="switch-track"></span><span>${s.sending ? tr('Включена', 'On') : tr('Выключена', 'Off')}</span></button></dd>
          <dt>${tr('Разрешено отправок', 'Sends allowed')}</dt>
          <dd class="stack"><span class="stage">${L.stages.map((x) => `<button class="tab" type="button" aria-pressed="${String(s.armed_n) === String(x)}" ${demo(tr('Демо: ступень запуска не меняется', 'Demo: the launch stage doesn’t change'))}>${stageName(x)}</button>`).join('')}
            <span class="muted num" style="align-self:center">${esc(budgetText(s))}</span></span>
            <span class="hint">${tr('Запуск идёт ступенями: сначала 1 сообщение, потом 10, 50 и без ограничения. Если настройки ролика изменились, бот останавливается и ждёт нового разрешения.', 'Launch goes in stages: first 1 message, then 10, 50 and no limit. If a reel’s settings change, the bot stops and waits for new permission.')}</span></dd>
          <dt>${tr('Скорость', 'Speed')}</dt><dd>${tr(`до ${L.max_sends_per_hour} сообщений в час`, `up to ${L.max_sends_per_hour} messages an hour`)}</dd>
          <dt>${tr('Проверка комментариев', 'Comment checks')}</dt><dd>${tr(`каждые ${L.poll_interval_sec} секунд`, `every ${L.poll_interval_sec} seconds`)}</dd>
        </dl>
      </section>

      <section class="section">
        <div class="section-head"><h2>Instagram</h2></div>
        <dl class="props" style="margin-top:0">
          <dt>${tr('Аккаунт', 'Account')}</dt><dd>@${esc(D.account.username)} ${s.meta_ok ? tag(tr('подключён', 'connected'), 'green') : tag(tr('доступ закрыт', 'access closed'), 'red')}</dd>
          <dt>${tr('Ключ доступа', 'Access key')}</dt><dd>${L.token_expires_in_days == null ? tr('срок не известен', 'expiry unknown') : tr(`действует ещё ${L.token_expires_in_days} ${plural(L.token_expires_in_days, ['день', 'дня', 'дней'])}, продлевается сам`, `valid for ${L.token_expires_in_days} more ${enp(L.token_expires_in_days, 'day', 'days')}, renews itself`)}</dd>
        </dl>
      </section>

      <section class="section">
        <div class="section-head"><h2>${tr('Оповещения в Telegram', 'Telegram alerts')}</h2></div>
        <dl class="props" style="margin-top:0">
          <dt>${tr('Канал', 'Channel')}</dt><dd>${s.telegram_ok ? tag(tr('подключён', 'connected'), 'green') : tag(tr('не настроен', 'not set up'), 'amber')}</dd>
          <dt>${tr('Напоминания', 'Reminders')}</dt><dd>${tr('о нерешённой проблеме — каждые', 'about an unresolved problem — every')} ${esc(String(L.alerts.repeat_min / 60).replace('.', tr(',', '.')))} ${tr('ч', 'h')}</dd>
          <dt>${tr('Сводка', 'Daily summary')}</dt><dd>${tr('каждый день в', 'every day at')} ${String(L.alerts.digest_hour_msk).padStart(2, '0')}:00 ${tr('по Москве', 'Moscow time')}</dd>
        </dl>
        <ul class="feed tg-feed" aria-label="${tr('Последние оповещения', 'Latest alerts')}">${D.telegram.slice(0, 4).map((m) => `
          <li><span class="dot"></span><span style="white-space:pre-line">${esc(m.text)}</span><time>${esc(ago(m.at))}</time></li>`).join('')}</ul>
      </section>

      <section class="section">
        <div class="section-head"><h2>${tr('Общие тексты', 'Shared texts')}</h2></div>
        <dl class="props" style="margin-top:0">
          <dt>${tr('Приветствие', 'Greeting')}</dt><dd><input class="input" value="${esc(L.greeting)}" aria-label="${tr('Приветствие', 'Greeting')}"></dd>
          <dt>${tr('Если отвечаем поздно', 'If we reply late')}</dt><dd class="stack"><input class="input" value="${esc(L.late_greeting)}" aria-label="${tr('Приветствие, если отвечаем поздно', 'Greeting if we reply late')}">
            <span class="hint">${tr(`Подставляется, если с комментария прошло больше ${L.late_after_minutes} минут.`, `Used when more than ${L.late_after_minutes} minutes have passed since the comment.`)}</span></dd>
          <dt>${tr('Ответы под комментарием', 'Replies under comments')}</dt><dd><details class="phrases"><summary>${L.public_phrases.length} ${tr('фраз', enp(L.public_phrases.length, 'phrase', 'phrases'))}</summary><ol>${L.public_phrases.map((p) => `<li>${esc(p)}</li>`).join('')}</ol></details></dd>
        </dl>
      </section>
      <p class="note-box">${D.real ? RO() + tr('. Здесь показано, как бот настроен сейчас.', '. This shows how the bot is set up right now.') : tr('Это демонстрация: данные придуманы, кнопки ничего не меняют на сервере. Настоящий бот к панели пока не подключён.', 'This is a demo: the data is made up, and the buttons don’t change anything on the server. The real bot isn’t connected to the panel yet.')}</p>`
  }

  // ---------- карточка человека ----------

  // quiet: перерисовать открытую карточку (смена языка), не трогая фокус
  function openPerson(id, quiet) {
    const p = D.people.find((x) => x.comment_id === id)
    if (!p) return
    S.peekId = id
    const a = auto(p.automation), st = STATUS()[p.status] || { label: p.status, hint: '' }
    const step = (text, at, tone = '') => `<li><span class="dot${tone ? ' dot-' + tone : ''}"></span><div>${text}${at ? `<time>${esc(fDate.format(new Date(at)))} · ${esc(ago(at))}</time>` : ''}</div></li>`
    const steps = [step(`${tr('Написал комментарий', 'Wrote a comment')}<div class="quote">${esc(p.text)}</div>`, p.ts)]
    if (p.status === 'sent') {
      steps.push(step(tr('Бот отправил сообщение в директ', 'The bot sent a DM'), p.dm_at, 'green'))
      if (p.public_phrase) steps.push(step(`${tr('Бот ответил под комментарием:', 'The bot replied under the comment:')} ${quote(esc(p.public_phrase))}`, p.public_at, 'green'))
      if (D.real) steps.push(...personFunnel(p, step))
      else {
        if (p.clicked) steps.push(step(tr('Перешёл по ссылке', 'Clicked the link'), null, 'green'))
        if (p.replied) steps.push(step(W().person, null, 'green'))
      }
    } else if (p.status === 'pending') {
      const place = D.people.filter((x) => x.status === 'pending' && x.ts <= p.ts).length
      steps.push(step(D.real ? tr('Ждёт своей очереди.', 'Waiting its turn.') : tr(`Ждёт очереди, примерно ${place} в списке. Получит сообщение в течение часа.`, `Waiting in line, about number ${place}. Will get the message within an hour.`), null))
    } else {
      const what = {
        uncertain: tr('Бот отправил сообщение, но Instagram не ответил вовремя', 'The bot sent the message, but Instagram didn’t answer in time'),
        expired: tr('Прошло 7 дней: написать уже нельзя', '7 days have passed: it’s too late to write'),
        refused: tr('Instagram отклонил сообщение', 'Instagram rejected the message'),
        already: tr('Вы сами ответили под комментарием', 'You replied under the comment yourself'),
        dup_user: tr('Ссылку уже получил по другому комментарию', 'Already got the link from another comment'),
        near: tr('Не засчитано как кодовое слово', 'Not counted as the code word'),
      }[p.status] || st.label
      steps.push(step(esc(what), null, st.tone === 'amber' ? 'amber' : st.tone === 'red' ? 'red' : ''))
    }
    const actions = {
      uncertain: `<a class="btn" href="https://www.instagram.com/direct/inbox/" target="_blank" rel="noopener">${tr('Открыть директ', 'Open DMs')}</a>
        ${D.real ? '' : `<button class="btn btn-primary" type="button" ${demo(tr('Демо: отметка «проверено» не сохраняется', 'Demo: the “checked” mark isn’t saved'))}>${tr('Я проверил', 'I checked')}</button>`}`,
      near: D.real ? '' : `<button class="btn btn-primary" type="button" ${demo(tr('Демо: слово не добавлено', 'Demo: the word wasn’t added'))}>${tr('Засчитывать', 'Count')} ${quote(esc(p.text.split(' ')[0]))}</button>`,
    }[p.status] || ''
    const peek = $('#peek')
    peek.innerHTML = `
      <div class="peek-head"><span class="muted" style="font-size:13px">${esc(a ? a.name : '')}</span>
        <button class="icon-btn" type="button" data-action="peek-close" aria-label="${tr('Закрыть', 'Close')}">${icon('close')}</button></div>
      <div class="cell-main">${avatar(p.username, 'avatar-lg')}<div><h2>${esc(p.username)}</h2><div style="margin-top:4px">${statusTag(p.status)}${a && a.variants.length > 1 ? ` <span class="tag">${tr('вариант', 'variant')} ${esc(p.variant)}</span>` : ''}</div></div></div>
      <p class="muted" style="margin-top:16px;font-size:14px">${esc(st.hint)}${p.status === 'near' && p.reason ? ` ${tr('Отличие', 'Difference')}: ${esc(p.reason)}.` : ''}</p>
      ${actions ? `<div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">${actions}</div>` : ''}
      <ol class="timeline">${steps.join('')}</ol>`
    if (quiet) return
    S.lastFocus = document.activeElement
    peek.hidden = false
    $('.peek-scrim').hidden = false
    $('[data-action="peek-close"]', peek).focus()
  }
  function closePeek() {
    if ($('#peek').hidden) return
    $('#peek').hidden = true
    $('.peek-scrim').hidden = true
    if (S.lastFocus && document.contains(S.lastFocus)) S.lastFocus.focus()
  }

  // ---------- маршруты и отрисовка ----------

  function route() {
    const [path, query = ''] = (location.hash.replace(/^#/, '') || '/').split('?')
    const parts = path.split('/').filter(Boolean)
    return { parts, params: new URLSearchParams(query) }
  }

  function render(keepScroll) {
    const { parts, params } = route()
    const [section, id] = parts
    let html, title, current = '#/' + (section || '')
    if (!section) { html = viewHome(); title = tr('Обзор', 'Overview') }
    else if (section === 'reels' && id) { html = viewReel(decodeURIComponent(id)); title = tr('Ролик', 'Reel'); current = '#/reels' }
    else if (section === 'reels') { html = viewReels(); title = tr('Ролики', 'Reels') }
    else if (section === 'people') { S.status = params.get('status') || S.status || 'all'; html = viewPeople(); title = tr('Люди', 'People') }
    else if (section === 'analytics') { html = viewAnalytics(); title = tr('Аналитика', 'Analytics') }
    else if (section === 'settings') { html = viewSettings(); title = tr('Настройки', 'Settings') }
    else { html = `<h1>${tr('Такой страницы нет', 'There’s no such page')}</h1><p class="lead"><a class="link" href="#/">${tr('На главную', 'Go to the overview')}</a></p>`; title = tr('Нет страницы', 'No page') }

    // Ступенчатое появление только при первом открытии; переходы между разделами — короткое проявление
    document.documentElement.classList.toggle('first-paint', !render.painted)
    document.documentElement.classList.toggle('no-enter', Boolean(keepScroll && render.painted))   // перерисовка на месте: без анимации
    render.painted = true
    $('#view').innerHTML = html
    if (D.real) lockEdits($('#view'))
    $('#topbar-title').textContent = title
    document.title = `${title} · ${tr('Автоответ', 'Autoreply')}`
    const need = D.people.filter((p) => p.status === 'uncertain').length
    $('#nav').innerHTML = NAV().map(([href, label, ic]) => `<a href="${href}"${href === current ? ' aria-current="page"' : ''}>${icon(ic)}<span>${label}</span>${href === '#/people' && need ? `<span class="count" title="${tr('Нужна проверка', 'Needs a check')}">${need}</span>` : ''}</a>`).join('')
    const h = health()
    $('#side-status').innerHTML = `<span class="dot dot-${h.tone}"></span><span><b>${esc(h.title)}</b><span class="muted">${esc(h.text)}</span></span>`
    closeMenu()
    if (!keepScroll) { closePeek(); window.scrollTo(0, 0) }
    if (section === 'people' && params.get('person')) openPerson(params.get('person'))
  }

  // Тема: «Авто» следует системе; выбор помнит только этот браузер
  function setTheme(theme, save) {
    const root = document.documentElement
    if (save) {
      root.classList.add('no-anim')          // иначе все цвета переливаются разом
      try { localStorage.setItem('panel-theme', theme) } catch (e) { /* приватный режим: без запоминания */ }
    }
    if (theme === 'auto') root.removeAttribute('data-theme')
    else root.dataset.theme = theme
    document.querySelectorAll('[data-action="theme"]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.theme === theme)))
    if (save) { void root.offsetHeight; requestAnimationFrame(() => root.classList.remove('no-anim')) }
  }
  try { setTheme(localStorage.getItem('panel-theme') || 'auto', false) } catch (e) { setTheme('auto', false) }

  // Тексты из index.html: data-i18n — текст элемента, data-i18n-aria — его aria-label
  const STATIC = {
    skip: ['К содержимому', 'Skip to content'],
    menu: ['Открыть меню', 'Open menu'],
    sections: ['Разделы', 'Sections'],
    sub: ['Instagram · автоответ', 'Instagram · autoreply'],
    theme: ['Тема', 'Theme'],
    'theme-auto': ['Авто', 'Auto'],
    'theme-light': ['Светлая', 'Light'],
    'theme-dark': ['Тёмная', 'Dark'],
    lang: ['Язык интерфейса', 'Interface language'],
    demo: ['Демо · данные придуманы', 'Demo · made-up data'],
    scenario: ['Какое состояние показать', 'Which state to show'],
    details: ['Подробности', 'Details'],
    noscript: ['Для панели нужен включённый JavaScript.', 'The panel needs JavaScript turned on.'],
  }
  function applyStatic() {
    const root = document.documentElement
    root.lang = LANG
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const text = tr(...STATIC[el.dataset.i18n])
      if (el.tagName === 'NOSCRIPT') el.innerHTML = `<p style="padding:24px;font:16px system-ui">${esc(text)}</p>`
      else el.textContent = text
    })
    document.querySelectorAll('[data-i18n-aria]').forEach((el) => el.setAttribute('aria-label', tr(...STATIC[el.dataset.i18nAria])))
    document.querySelectorAll('[data-action="lang"]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.lang === LANG)))
    if (!D) document.title = tr('Автоответ', 'Autoreply')
  }
  applyStatic()

  // Язык: запоминается в этом браузере; перерисовка на месте, без перезагрузки. В демо — свой файл данных на язык.
  function setLang(lang, refocus) {
    if (!LANGS.includes(lang)) return
    const changed = lang !== LANG
    LANG = lang
    try { localStorage.setItem('panel-lang', lang) } catch (e) { /* приватный режим: без запоминания */ }
    const url = new URL(location.href)
    if (url.searchParams.has('lang')) { url.searchParams.set('lang', lang); history.replaceState(null, '', url) }   // иначе ?lang= перебьёт выбор после обновления
    setFormats()
    applyStatic()
    if (!changed) return
    const after = () => {
      if (S.peekId && !$('#peek').hidden) openPerson(S.peekId, true)
      if (refocus) { const b = $(`#view [data-action="lang"][data-lang="${lang}"]`); if (b) b.focus() }
    }
    if (!D) boot()
    else if (real) { D = fromReal(RAW); fill(); render(true); after() }
    else { S.draft = {}; boot(after) }   // правки в демо привязаны к текстам прошлого языка
  }

  function toast(text) {
    const el = $('#toast')
    el.textContent = text
    el.classList.add('on')
    clearTimeout(toast.timer)
    toast.timer = setTimeout(() => el.classList.remove('on'), 2600)
  }
  function closeMenu() {
    $('#side').classList.remove('open')
    $('.scrim').hidden = true
    $('[data-action="menu"]').setAttribute('aria-expanded', 'false')
  }
  const reelId = () => decodeURIComponent(route().parts[1] || '')
  const refreshPhone = () => { const el = $('#phone'); if (el) el.innerHTML = phone(draft(reelId())) }

  document.addEventListener('click', (event) => {
    const el = event.target.closest('[data-action]')
    if (!el) return
    const action = el.dataset.action
    if (action === 'menu') {
      const open = $('#side').classList.toggle('open')
      $('.scrim').hidden = !open
      el.setAttribute('aria-expanded', String(open))
    } else if (action === 'menu-close') closeMenu()
    else if (action === 'go') location.hash = el.dataset.href
    else if (action === 'person') openPerson(el.dataset.id)
    else if (action === 'peek-close') closePeek()
    else if (action === 'demo') toast(el.dataset.msg)
    else if (action === 'theme') setTheme(el.dataset.theme, true)
    else if (action === 'lang') setLang(el.dataset.lang, Boolean(el.closest('#view')))
    else if (!D) return
    else if (D.real && ['variant-add', 'variant-remove', 'chip-del', 'toggle'].includes(action)) toast(RO())
    else if (action === 'status') { S.status = el.dataset.status; history.replaceState(null, '', '#/people' + (S.status === 'all' ? '' : '?status=' + S.status)); render(true) }
    else if (action === 'phone') { S.phone = el.dataset.tab; refreshPhone() }
    else if (action === 'variant') { S.variant[reelId()] = el.dataset.v; S.phone = 'dm'; render(true) }
    else if (action === 'variant-add') {
      const a = draft(reelId())
      a.variants[0].share = 50
      a.variants.push({ id: 'B', share: 50, sent: 0, replied: 0, clicked: 0, text: a.variants[0].text })
      a.ab = { enabled: true, started_at: D.generated_at }
      S.variant[a.id] = 'B'; S.phone = 'dm'
      render(true)
      toast(tr('Добавлен вариант B: измените его текст', 'Variant B added: change its text'))
    } else if (action === 'variant-remove') {
      const a = draft(reelId())
      a.variants = [Object.assign(a.variants[0], { share: 100 })]
      a.ab = { enabled: false }
      S.variant[a.id] = 'A'
      render(true)
      toast(tr('Тест закончен: остался вариант A', 'Test ended: variant A remains'))
    } else if (action === 'chip-del') { draft(reelId())[el.dataset.key].splice(Number(el.dataset.i), 1); render(true) }
    else if (action === 'toggle') { const a = draft(reelId()); a[el.dataset.key] = !a[el.dataset.key]; render(true) }
  })

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closePeek(); closeMenu() }
    const row = event.target.closest && event.target.closest('tr.rowlink')
    if (row && event.target === row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); row.click() }
    const chip = event.target.dataset && event.target.dataset.chipInput
    if (chip && !D.real && (event.key === 'Enter' || event.key === ',')) {
      event.preventDefault()
      const word = event.target.value.trim().toLowerCase()
      const list = draft(reelId())[chip]
      if (word && !list.includes(word)) list.push(word)
      render(true)
      const again = $(`[data-chip-input="${chip}"]`)
      if (again) again.focus()
    }
  })

  document.addEventListener('input', (event) => {
    const el = event.target
    if (el.id === 'search') { S.q = el.value; $('#people-rows').innerHTML = peopleRows(); return }
    const field = el.dataset && el.dataset.field
    if (!field || D.real) return
    const a = draft(reelId())
    if (field === 'text') (a.variants.find((v) => v.id === S.variant[a.id]) || a.variants[0]).text = el.value
    else a[field] = el.value
    if (field === 'text' || field === 'link') refreshPhone()
  })

  // ---------- настоящие данные: data/report.json (autoreply/panel.py) в форме демо ----------

  function agoSec(sec) {
    const s = Math.max(0, Math.round(sec))
    if (s < 60) return tr(`${s} с назад`, `${s} s ago`)
    if (s < 3600) return tr(`${Math.floor(s / 60)} мин назад`, `${Math.floor(s / 60)} min ago`)
    if (s < 86400) return tr(`${Math.floor(s / 3600)} ч назад`, `${Math.floor(s / 3600)} h ago`)
    const d = Math.floor(s / 86400)
    return d === 1 ? tr('вчера', 'yesterday') : tr(`${d} дн назад`, `${d} d ago`)
  }

  const AGENT = (ru, en) => tr(`Скажите агенту: «${ru}».`, `Tell the agent: “${en}”.`)
  const PROBLEM = () => ({   // ключ проблемы бота (SPEC 9) -> заголовок, что сделать, куда ссылка
    not_running: [tr('Бот остановлен', 'The bot is stopped'), AGENT('бот остановлен, посмотри', 'the bot is stopped, take a look')],
    'hold:account': [tr('Instagram закрыл доступ', 'Instagram closed access'), AGENT('Instagram закрыл доступ боту, посмотри', 'Instagram closed the bot’s access, take a look')],
    'hold:send': [tr('Отправка на паузе', 'Sending is paused'), tr('Бот продолжит сам, когда пауза закончится. Если пауза долгая — ', 'The bot will carry on by itself when the pause ends. If the pause is long — ') + AGENT('почему отправка на паузе', 'why is sending paused')],
    waiting: [tr('Люди ждут ссылку', 'People are waiting for the link'), tr('Если отправка выключена или кончилось разрешение — ', 'If sending is off or the allowance has run out — ') + AGENT('разреши отправку', 'allow sending'), 'pending', tr('Показать очередь', 'Show the queue')],
    expiring: [tr('Кто-то может не успеть получить ссылку', 'Someone may not get the link in time'), AGENT('люди не успевают получить ссылку', 'people aren’t getting the link in time'), 'pending', tr('Показать очередь', 'Show the queue')],
    uncertain_manual: [tr('Неизвестно, дошло ли сообщение', 'Not sure the message arrived'), tr('Откройте директ и проверьте переписку, потом скажите агенту, что проверили.', 'Open your DMs and check the chat, then tell the agent you’ve checked.'), 'uncertain', tr('Показать этих людей', 'Show these people')],
    unaccounted: [tr('Бот запутался в учёте', 'The bot lost track of its records'), AGENT('в панели проблема unaccounted', 'the panel shows the unaccounted problem')],
    refusals: [tr('Instagram часто отказывает', 'Instagram refuses often'), AGENT('Instagram отказывает в отправке, посмотри', 'Instagram refuses to send, take a look')],
    token: [tr('Ключ доступа Instagram', 'Instagram access key'), AGENT('проверь ключ Instagram', 'check the Instagram key')],
    no_alert_channel: [tr('Оповещения в Telegram не подключены', 'Telegram alerts aren’t connected'), AGENT('подключи оповещения в Telegram', 'connect Telegram alerts')],
    inbound_waiting: [tr('Нажатия кнопки ждут ответа', 'Button taps are waiting for a reply'), AGENT('люди нажимают кнопку, а ответа нет', 'people tap the button but get no reply')],
    restart_needed: [tr('Настройки изменены, а бот работает со старыми', 'Settings changed, but the bot runs on the old ones'), AGENT('перезапусти бота с новыми настройками', 'restart the bot with the new settings')],
    config_file_invalid: [tr('В файле настроек ошибка', 'There’s an error in the settings file'), AGENT('в настройках ошибка, посмотри', 'there’s an error in the settings, take a look')],
    no_active_reel: [tr('Ни один ролик не включён', 'No reel is turned on'), AGENT('включи ролик', 'turn on a reel')],
    unservable: [tr('Есть просьбы, которые бот не может обработать', 'Some requests the bot can’t handle'), AGENT('посмотри проблему unservable', 'look at the unservable problem')],
    cycle_error: [tr('Ошибка в работе бота', 'The bot hit an error'), AGENT('в боте ошибка, посмотри', 'the bot has an error, take a look')],
    env: [tr('Бот не может прочитать файл с ключами', 'The bot can’t read the keys file'), AGENT('посмотри файл .env', 'check the .env file')],
    clock: [tr('Сбились часы сервера', 'The server clock is off'), AGENT('посмотри часы сервера', 'check the server clock')],
  })
  const PERSON_STATUS = { done: 'sent', sending: 'pending' }   // статусы бота -> ближайший статус списка

  function fromReal(R) {
    const at = new Date(R.generated_at), bot = R.bot, cfg = R.settings
    const name = R.account.username || tr('аккаунт', 'account')
    const problems = bot.problems.map((p) => {
      const known = PROBLEM()[p.key]
      const [title, action, status, linkText] = known || (p.key.startsWith('reel:')
        ? [tr('Ролик не читается', 'A reel can’t be read'), AGENT('ролик не читается, посмотри', 'a reel can’t be read, take a look')]
        : [tr('Нужна проверка', 'Needs a check'), AGENT(`в панели проблема ${p.key}`, `the panel shows problem ${p.key}`)])
      return { key: p.key, level: p.level, title, text: p.text, action,
        link: status ? `#/people?status=${status}` : '', link_text: linkText || '' }
    })
    const service = {
      running: bot.running, sending: bot.sending, budget: bot.budget, armed_n: bot.armed_n ?? 0,
      account_hold: bot.account_hold, send_hold: bot.send_hold, meta_ok: !bot.account_hold, telegram_ok: bot.telegram_ok,
      last_cycle_sec: bot.last_cycle_ok_at ? (at - new Date(bot.last_cycle_ok_at)) / 1000 : null,
    }
    const automations = R.reels.map((r) => {
      const flow = r.flow, sum = (r.variants || []).reduce((s, v) => s + (v.weight || 0), 0) || 1
      const variants = (r.variants || []).map((v) => ({
        id: v.name, share: Math.round((v.weight || 0) / sum * 100), sent: v.first_dm, replied: v.pressed, clicked: v.clicked_people,
        text: v.opening && flow && flow.opening ? flow.opening.text : (v.text || r.text), raw: v,
      }))
      if (!variants.length) variants.push({ id: 'A', share: 100, sent: r.counts.sent, replied: 0, clicked: r.clicked_people, text: r.text })
      const c = r.counts
      return {
        id: r.media_id, name: r.name || `media ${r.media_id}`, media_id: r.media_id, permalink: r.permalink, active: r.active,
        caption: r.permalink || `media ${r.media_id}`, hue: hue(r.media_id), keywords: r.keywords.length ? r.keywords : ['—'],
        also_match: r.also_match, max_words: r.max_words, link: r.link, text: r.text, flow,
        stats: { comments: c.comments, asked: c.requested, sent: c.sent, waiting: c.pending, expired: c.expired,
          uncertain: c.uncertain, refused: c.refused, already: c.already,
          replied: (r.variants || []).reduce((s, v) => s + v.pressed, 0), clicked: r.clicked_people },
        ab: { enabled: variants.length > 1 }, variants,
      }
    })
    return {
      demo: false, real: true, generated_at: R.generated_at, realTotals: R.totals,
      account: { username: name, name, followers: R.account.followers },
      limits: {
        max_sends_per_hour: cfg.max_sends_per_hour, window_days: cfg.window_days, poll_interval_sec: cfg.poll_interval_sec,
        late_after_minutes: cfg.late_after_minutes, greeting: cfg.greeting, late_greeting: cfg.late_greeting,
        public_reply: cfg.public_reply, public_phrases: cfg.public_phrases,
        alerts: { repeat_min: cfg.alerts.repeat_min, digest_hour_msk: (cfg.alerts.digest_hour_utc + 3) % 24 },
        token_expires_in_days: bot.token_expires_in_sec == null ? null : Math.max(0, Math.floor(bot.token_expires_in_sec / 86400)),
        version: tr('бот', 'bot'), stages: [1, 10, 50, 'unlimited'],
      },
      scenarios: { now: { label: tr('Сейчас', 'Now'), service, problems } },
      automations,
      people: R.people.map((p) => ({
        ...p, username: p.username || tr('без имени', 'no name'), text: p.text || '', automation: p.media_id,
        status: PERSON_STATUS[p.status] || p.status, reason: (p.reason || '').replace(/^near:/, ''),
        clicked: p.clicks > 0, replied: !!p.pressed_at,
      })),
      hours: R.hourly.map((h) => ({ hour: h.hour, sent: h.ok })),
      days: R.daily.map((d) => ({ day: d.day, asked: d.requested, sent: d.sent })),
      activity: R.events.map((e) => ({ at: e.at, ...eventText(e) })),
      telegram: R.alerts,
    }
  }

  function eventText(e) {
    const who = e.username ? '@' + e.username : tr('человеку без имени', 'a person with no name')
    if (e.kind === 'tap') return { text: tr(`${e.username ? '@' + e.username : 'Человек'} нажал кнопку`, `${e.username ? '@' + e.username : 'Someone'} tapped the button`), level: 'ok' }
    const [what, done] = e.kind === 'dm' ? [tr('Сообщение', 'Message'), tr('отправлено', 'sent')] : e.kind === 'public' ? [tr('Ответ под комментарием', 'Reply under the comment'), tr('отправлен', 'sent')]
      : e.detail === 'link' ? [tr('Ссылка', 'Link'), tr('отправлена', 'sent')] : e.detail === 'followup' ? [tr('Напоминание', 'Reminder'), tr('отправлено', 'sent')] : [tr('Просьба подписаться', 'Follow request'), tr('отправлена', 'sent')]
    if (e.outcome == null) return { text: tr(`${what} для ${who}: отправляется`, `${what} to ${who}: sending`), level: '' }
    if (e.outcome === 'ok') return { text: tr(`${what} ${done} ${who}`, `${what} ${done} to ${who}`), level: 'ok' }
    const why = tr({ uncertain: 'неизвестно, дошло ли', not_sent: 'не удалось связаться с Instagram', rejected: 'Instagram отказал', global: 'Instagram попросил подождать' },
      { uncertain: 'not sure it arrived', not_sent: 'couldn’t reach Instagram', rejected: 'Instagram refused', global: 'Instagram asked to wait' })
    return { text: tr(`${what} для ${who}: ${why[e.outcome] || e.outcome}`, `${what} to ${who}: ${why[e.outcome] || e.outcome}`), level: e.outcome === 'rejected' ? 'bad' : 'warn' }
  }

  function realStats() {
    const t = D.realTotals
    return `<div class="substats">
      <a href="#/analytics">${tr('Получили ссылку', 'Got the link')} <b>${n(t.linked)}</b></a>
      <a href="#/analytics">${tr('Переходов по ссылкам', 'Link clicks')} <b>${n(t.clicks)}</b></a>
      ${t.robot_clicks ? `<a href="#/analytics">${tr('Открытий роботом Instagram, не считаются', 'Opened by Instagram’s robot, not counted')} <b>${n(t.robot_clicks)}</b></a>` : ''}
    </div>`
  }

  function opening(a, v) {
    const raw = v && v.raw
    if (!D.real || !raw || !raw.opening || !a.flow || !a.flow.opening) return null
    return { text: String(a.flow.opening.text).replaceAll('{greeting}', D.limits.greeting), button: a.flow.opening.button }
  }

  function variantWords(raw) {
    if (!raw) return ''
    if (raw.opening == null) return tr('вариант убран из настроек', 'variant removed from settings')
    return [raw.opening ? tr('сначала кнопка «Получить»', '“Get” button first') : tr('ссылка сразу', 'link right away'), raw.gate ? tr('ссылка после проверки подписки', 'link after a follow check') : ''].filter(Boolean).join(', ')
  }

  function variantNote(a, v) {
    if (!v.raw) return ''
    return `<span class="hint">${tr('Вариант', 'Variant')} ${esc(v.id)}: ${esc(variantWords(v.raw))}.${opening(a, v) ? tr(' Ниже текст первого сообщения с кнопкой.', ' Below is the text of the first message with the button.') : ''}</span>`
  }

  function variantFunnels(a) {
    return `<div class="variant-funnels">${a.variants.filter((v) => v.raw).map((v) => {
      const r = v.raw
      const rows = [[tr('Получили первое сообщение', 'Got the first message'), r.first_dm, '']]
      if (r.opening !== false) rows.push([tr('Нажали кнопку', 'Tapped the button'), r.pressed, pct(r.pressed, r.first_dm) + tr(' от получивших', ' of those who got it')])
      if (r.gate) {
        rows.push([tr('Подписаны при проверке', 'Following at the check'), r.checked_yes, pct(r.checked_yes, r.pressed) + tr(' от нажавших', ' of those who tapped')])
        rows.push([tr('Не подписаны при проверке', 'Not following at the check'), r.checked_no, pct(r.checked_no, r.pressed) + tr(' от нажавших', ' of those who tapped')])
      }
      rows.push([tr('Получили ссылку', 'Got the link'), r.linked, pct(r.linked, r.first_dm) + tr(' от получивших', ' of those who got it')])
      rows.push([tr('Перешли по ссылке', 'Clicked the link'), r.clicked_people, pct(r.clicked_people, r.linked) + tr(' от получивших ссылку', ' of those who got the link')])
      const more = [[tr('Получили напоминание', 'Got a reminder'), r.followup], [tr('Не подписались после трёх просьб', 'Didn’t follow after three asks'), r.stuck], [tr('Отписались после ссылки', 'Unfollowed after the link'), r.unfollowed]]
      return `<div class="variant-funnel">
        <h3><span class="variant-letter">${esc(v.id)}</span> ${v.share ? `${v.share}% ${tr('людей', 'of people')} · ` : ''}${esc(variantWords(r))}</h3>
        ${funnel(rows)}
        <p class="hint">${more.map(([label, value]) => `${esc(label)}: <b>${n(value)}</b>`).join(' · ')}</p></div>`
    }).join('')}</div>`
  }

  function personFunnel(p, step) {
    const out = []
    if (p.pressed_at) out.push(step(tr('Нажал кнопку «Получить»', 'Tapped the “Get” button'), p.pressed_at, 'green'))
    if (p.follow === 'yes') out.push(step(tr('Подписан на вас', 'Follows you'), null, 'green'))
    else if (p.follow === 'no') out.push(step(tr('На момент проверки не подписан', 'Wasn’t following at the time of the check'), null, 'amber'))
    if (p.state === 'stuck') out.push(step(tr('Так и не подписался: бот больше не просит', 'Never followed: the bot has stopped asking'), null, 'amber'))
    if (p.link_at) out.push(step(tr('Получил ссылку', 'Got the link'), p.link_at, 'green'))
    if (p.clicks) out.push(step(tr(`Перешёл по ссылке${p.clicks > 1 ? ` (${p.clicks} раза)` : ''}`, `Clicked the link${p.clicks > 1 ? ` (${p.clicks} times)` : ''}`), null, 'green'))
    if (p.followup_at) out.push(step(tr('Получил напоминание', 'Got a reminder'), p.followup_at, 'green'))
    if (p.unfollowed_at) out.push(step(tr('Отписался после ссылки', 'Unfollowed after the link'), p.unfollowed_at, 'amber'))
    return out
  }

  function lockEdits(root) {
    root.querySelectorAll('input:not(#search), textarea').forEach((el) => { el.readOnly = true; el.setAttribute('aria-readonly', 'true') })
    root.querySelectorAll('[data-action="chip-del"], [data-chip-input]').forEach((el) => el.remove())
    root.querySelectorAll('[data-action="toggle"]').forEach((el) => el.setAttribute('aria-disabled', 'true'))
  }


  window.addEventListener('hashchange', () => { render(false); $('#main').focus({ preventScroll: true }) })

  const asked = new URLSearchParams(location.search).get('demo')
  const real = /\/panel\//.test(location.pathname) && !asked
  let RAW = null, loads = 0   // RAW — настоящий отчёт как пришёл: при смене языка fromReal пересобирает тексты из него
  const getJson = (url) => fetch(url, { cache: 'no-store' }).then((r) => {
    if (real && r.status === 401) { location.href = 'login'; return new Promise(() => {}) }
    if (!r.ok) throw new Error(r.status)
    return r.json()
  })
  // Демо на английском — data/demo.en.json; если его нет, русское демо (переведена только оболочка)
  const loadData = () => (real ? getJson('data/report.json')
    : LANG === 'en' ? getJson('data/demo.en.json').catch(() => getJson('data/demo.json')) : getJson('data/demo.json'))

  function fill() {
    $('#acc-name').textContent = D.account.username
    $('#acc-initial').textContent = String(D.account.name).slice(0, 1).toUpperCase()
    const select = $('#scenario')
    select.innerHTML = Object.entries(D.scenarios).map(([id, s]) => `<option value="${esc(id)}">${esc(s.label)}</option>`).join('')
    select.value = S.scenario
    if (!D.demo) $('#demo-switch').hidden = true
  }
  $('#scenario').addEventListener('change', (event) => { S.scenario = event.target.value; render(true) })

  // after — после перерисовки при смене языка (первая загрузка идёт без него)
  function boot(after) {
    const ticket = ++loads
    loadData().then((data) => {
      if (ticket !== loads) return   // пока грузилось, язык переключили ещё раз
      const first = !D
      if (real) RAW = data
      D = real ? fromReal(data) : data
      if (first) {
        if (real) S.scenario = 'now'
        if (asked && D.scenarios[asked]) S.scenario = asked
      }
      fill()
      render(!first)
      if (after) after()
    }).catch(() => {
      if (ticket !== loads) return
      $('#view').innerHTML = `<h1>${tr('Не удалось загрузить данные', 'Couldn’t load the data')}</h1><p class="lead">${tr('Обновите страницу. Если не помогло, скажите агенту: «панель не открывает данные».', 'Refresh the page. If that doesn’t help, tell the agent: “the panel can’t open the data”.')}</p>`
    })
  }
  boot()
})()
