// Геометрический аудит экрана WebApp (E2E, test_ui_layout_audit.py).
//
// Возвращает список нарушений строками. jsdom раскладку не считает, поэтому
// всё это меряется в настоящем Chromium на ширине телефона. Что считается
// нарушением:
//
//  * overflow-x — страница шире экрана, или элемент вылез за край, не будучи
//    внутри горизонтального скроллера;
//  * overlap — соседние элементы потока пересекаются (карточки «наехали»);
//  * spill — содержимое карточки/кнопки/строки вылезло за её рамку;
//  * clipped — подпись вкладки или кнопки обрезана (scrollWidth > clientWidth);
//  * seg-hidden — активная вкладка ряда не видна целиком;
//  * covered — интерактивный элемент, докрученный в зону видимости, закрыт
//    нижней панелью, шапкой или чем-то ещё (elementFromPoint в его центре);
//  * header — шапка прозрачная, и прокрученное содержимое просвечивает.
((rootSel) => {
  const T = 1;  // допуск в пикселях: субпиксельное округление
  const out = [];
  const W = document.documentElement.clientWidth;

  const desc = (el) => {
    if (!el || !el.tagName) return String(el);
    const cls = [...el.classList].slice(0, 3).join('.');
    const id = el.id ? `#${el.id}` : '';
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 40);
    return `${el.tagName.toLowerCase()}${id}${cls ? '.' + cls : ''}「${text}」`;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.01;
  };
  // Ближайший предок, который обрезает или прокручивает по горизонтали.
  const xClipper = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox !== 'visible') return p;
    }
    return null;
  };
  const flowPos = (el) => {
    const p = getComputedStyle(el).position;
    return p === 'static' || p === 'relative' || p === 'sticky';
  };

  // Последний из подходящих: формы открываются поверх друг друга.
  const roots = document.querySelectorAll(rootSel || '#content');
  const root = roots[roots.length - 1];
  if (!root) return [`нет ${rootSel || '#content'}`];
  // Докрутка ниже двигает страницу — запоминаем и возвращаем.
  const scrollY0 = window.scrollY;
  const all = [...root.querySelectorAll('*')].filter(el => !(el instanceof SVGElement) && visible(el));

  // ── overflow-x ──
  if (document.documentElement.scrollWidth > W + T) {
    out.push(`overflow-x: страница шире экрана (${document.documentElement.scrollWidth} > ${W})`);
  }
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.right <= W + T && r.left >= -T) continue;
    if (xClipper(el)) continue;
    out.push(`overflow-x: ${desc(el)} [${Math.round(r.left)}..${Math.round(r.right)}] за краем ${W}`);
  }

  // ── overlap: соседи в потоке ──
  const inter = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > T
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > T;
  const parents = new Set(all.map(el => el.parentElement));
  for (const p of parents) {
    if (!p) continue;
    const kids = [...p.children].filter(k => !(k instanceof SVGElement) && visible(k) && flowPos(k));
    for (let i = 1; i < kids.length; i++) {
      const a = kids[i - 1].getBoundingClientRect();
      const b = kids[i].getBoundingClientRect();
      // Inline-куски текста в одной строке не «карточки» — их проверяет spill.
      if (getComputedStyle(kids[i]).display.startsWith('inline') && getComputedStyle(kids[i - 1]).display.startsWith('inline')) continue;
      if (inter(a, b)) out.push(`overlap: ${desc(kids[i - 1])} ↔ ${desc(kids[i])}`);
    }
  }

  // ── glued: скруглённые поверхности подряд без зазора ──
  const surface = (el) => {
    const st = getComputedStyle(el);
    if (parseFloat(st.borderTopLeftRadius) < 4) return false;
    const bg = st.backgroundColor;
    const hasBg = bg && !/rgba\([^)]*,\s*0\)$/.test(bg) && bg !== 'transparent';
    return hasBg || parseFloat(st.borderTopWidth) > 0;
  };
  // Не только соседи по DOM: поле поиска в своей обёртке и первая карточка
  // списка в другой — тоже «склеены», если между ними нет зазора.
  const surfaces = all.filter(el => flowPos(el) && surface(el) && !el.closest('.seg, .cat-row, .machine-photos'));
  for (const a of surfaces) {
    const ra = a.getBoundingClientRect();
    for (const b of surfaces) {
      if (a === b || a.contains(b) || b.contains(a)) continue;
      const rb = b.getBoundingClientRect();
      const xOverlap = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      if (xOverlap < Math.min(ra.width, rb.width) / 2) continue;       // не друг под другом
      const gap = rb.top - ra.bottom;                                   // b ниже a
      if (gap < -T || gap >= 4) continue;                               // пересечение ловит overlap
      out.push(`glued: ${desc(a)} ↔ ${desc(b)} (зазор ${Math.round(gap)}px)`);
    }
  }

  // ── spill: содержимое вылезло из рамки карточки / кнопки / строки ──
  const BOXES = '.c-surface, .card, .debt-card, .order-card, .c-row, .stat, .debt-stat, .money-block, '
    + '.wh-pos, .wh-total, .seg, button, .form-input, .cur-btn, .toast';
  for (const box of root.querySelectorAll(BOXES)) {
    if (!visible(box)) continue;
    const st = getComputedStyle(box);
    const clips = st.overflowX !== 'visible' || st.overflowY !== 'visible';
    const br = box.getBoundingClientRect();
    for (const el of box.querySelectorAll('*')) {
      if (el instanceof SVGElement && el.tagName.toLowerCase() !== 'svg') continue;
      if (!visible(el) || !flowPos(el)) continue;
      // Внутри скроллера (лента фото, ряд чипов) содержимое законно шире.
      const clip = xClipper(el);
      if (clip && clip !== box && box.contains(clip)) continue;
      const r = el.getBoundingClientRect();
      const outside = r.left < br.left - T || r.right > br.right + T || r.top < br.top - T || r.bottom > br.bottom + T;
      if (!outside) continue;
      if (clips) {
        // Обрезано рамкой. Для скроллера это норма (по той оси, по которой он
        // листается), для прочего — потеря текста.
        const scrolls = (v) => v === 'auto' || v === 'scroll';
        const outX = r.left < br.left - T || r.right > br.right + T;
        const outY = r.top < br.top - T || r.bottom > br.bottom + T;
        if ((!outX || scrolls(st.overflowX)) && (!outY || scrolls(st.overflowY))) continue;
        out.push(`clipped: ${desc(el)} обрезан рамкой ${desc(box)}`);
      } else {
        out.push(`spill: ${desc(el)} вылез из ${desc(box)}`);
      }
      break;
    }
  }

  // ── clipped: подпись не влезла ──
  for (const el of root.querySelectorAll('.seg-item, .cur-btn, .btn-primary, .btn-secondary, .cat-btn')) {
    if (!visible(el)) continue;
    if (el.scrollWidth > el.clientWidth + T) out.push(`clipped: подпись ${desc(el)} (${el.scrollWidth} > ${el.clientWidth})`);
  }
  // Активная вкладка ряда — целиком в видимой части ряда.
  for (const seg of root.querySelectorAll('.seg')) {
    const act = seg.querySelector('.seg-item.active');
    if (!act || !visible(seg)) continue;
    const s = seg.getBoundingClientRect();
    const a = act.getBoundingClientRect();
    if (a.left < s.left - T || a.right > s.right + T) out.push(`seg-hidden: активная ${desc(act)} не видна целиком`);
  }

  // ── header ──
  const top = document.querySelector('.topbar');
  if (top) {
    const bg = getComputedStyle(top).backgroundColor;
    const m = bg.match(/rgba?\(([^)]+)\)/);
    const alpha = m ? (m[1].split(',')[3] === undefined ? 1 : Number(m[1].split(',')[3])) : 0;
    if (alpha < 1) out.push(`header: фон шапки прозрачный (${bg})`);
  }

  // ── covered: докрученный элемент закрыт панелью/шапкой ──
  const toastHost = document.querySelector('.toast-host');
  const toastVis = toastHost && toastHost.style.visibility;
  if (toastHost) toastHost.style.visibility = 'hidden';
  const html = document.documentElement;
  const prevBehavior = html.style.scrollBehavior;
  html.style.scrollBehavior = 'auto';
  const INTERACTIVE = 'button, input:not([type=hidden]), textarea, select, a[href], [role=button], .c-row--tap';
  const targets = [...root.querySelectorAll(INTERACTIVE)].filter(el => visible(el) && !el.closest('.seg--scroll, .cat-row, .machine-photos'));
  for (const el of targets.slice(0, 120)) {
    el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
    const r = el.getBoundingClientRect();
    // Центр по горизонтали; по вертикали — верхний и нижний край с отступом
    // 25%: элемент, закрытый наполовину, так же непригоден, как закрытый целиком.
    const x = Math.min(Math.max(r.left + r.width / 2, 0), W - 1);
    for (const fy of [0.25, 0.75]) {
      const y = r.top + r.height * fy;
      if (y < 0 || y >= innerHeight) {
        out.push(`covered: ${desc(el)} не докручивается в видимую область (y=${Math.round(y)}, h=${innerHeight})`);
        break;
      }
      const hit = document.elementFromPoint(x, y);
      if (!hit || !(hit === el || el.contains(hit) || hit.contains(el))) {
        out.push(`covered: ${desc(el)} закрыт ${desc(hit && (hit.closest('#bottom-nav, .topbar') || hit))}`);
        break;
      }
    }
  }
  html.style.scrollBehavior = prevBehavior;
  if (toastHost) toastHost.style.visibility = toastVis || '';
  window.scrollTo(0, scrollY0);
  return [...new Set(out)];
})
