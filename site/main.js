// T3 event workflow landing page. Everything here is an enhancement: without it every section shows its
// finished content (the thread after its last wake, all three states of a task, every case of the
// merge rule, and both install commands).

const root = document.documentElement;
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const finePointer = window.matchMedia('(pointer: fine)').matches;
const rtl = root.dir === 'rtl';
const WORDS = root.lang === 'ar'
  ? {
    theme: { light: 'التبديل إلى الوضع الفاتح', dark: 'التبديل إلى الوضع الداكن' },
    copied: 'تم النسخ',
    copiedAll: 'نُسخ الأمران. الصقهما في الطرفية.',
    copiedOne: 'نُسخ: ',
    blocked: 'منع متصفحك النسخ. حدّد الأمر وانسخه بنفسك.',
  }
  : {
    theme: { light: 'Switch to the light theme', dark: 'Switch to the dark theme' },
    copied: 'Copied',
    copiedAll: 'Copied both commands. Paste them into your terminal.',
    copiedOne: 'Copied: ',
    blocked: 'Your browser blocked copying. Select the command and copy it yourself.',
  };

// ---------- theme: dark by default, light on request, remembered on this device ----------
const themeButton = document.querySelector('[data-theme-toggle]');
const themeColor = document.querySelector('meta[name="theme-color"]');

function applyTheme(theme) {
  root.dataset.theme = theme;
  const next = theme === 'dark' ? 'light' : 'dark';
  const label = WORDS.theme[next];
  themeButton?.setAttribute('aria-label', label);
  themeButton?.setAttribute('title', label);
  themeColor?.setAttribute('content', theme === 'dark' ? '#000000' : '#fcfcfc');
}

function saveTheme(theme) {
  try { localStorage.setItem('t3wf-theme', theme); } catch (e) { /* private mode: the choice lasts this visit */ }
}

applyTheme(root.dataset.theme === 'light' ? 'light' : 'dark');

themeButton?.addEventListener('click', () => {
  const next = root.dataset.theme === 'light' ? 'dark' : 'light';
  saveTheme(next);
  if (!document.startViewTransition || reduceMotion) { applyTheme(next); return; }
  // the new theme opens as a circle from the switch
  const box = themeButton.getBoundingClientRect();
  const x = box.left + box.width / 2;
  const y = box.top + box.height / 2;
  const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  const change = document.startViewTransition(() => applyTheme(next));
  change.ready.then(() => {
    root.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
      { duration: 720, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => {});
});

// ---------- entrances: once, as each part enters ----------
const reveal = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    entry.target.classList.add('is-in');
    reveal.unobserve(entry.target);
  }
}, { rootMargin: '0px 0px -10% 0px', threshold: 0.08 });
document.querySelectorAll('[data-reveal]').forEach((el) => reveal.observe(el));

// ---------- loops pause when they are off screen ----------
const loops = new IntersectionObserver((entries) => {
  for (const entry of entries) entry.target.classList.toggle('is-paused', !entry.isIntersecting);
});
document.querySelectorAll('.panel, .app').forEach((el) => loops.observe(el));

// ---------- the hero thread plays its wakes once ----------
const app = document.querySelector('[data-replay]');
if (app && !reduceMotion) {
  const steps = [...app.querySelectorAll('[data-step]')];
  const threads = [...app.querySelectorAll('.th[data-at]')];
  const dots = (th) => th.querySelector('[data-swap]');
  // back to the moment before your message: the DevOps worker still merging, the next task waiting
  threads.forEach((th) => {
    th.classList.remove('is-on');
    const dot = dots(th);
    const [from, to] = dot.dataset.swap.split(' ');
    dot.classList.remove(to);
    dot.classList.add(from);
  });
  app.classList.add('is-replay');
  const times = [380, 720, 1060, 1320, 1560];
  steps.forEach((step, i) => {
    setTimeout(() => {
      step.classList.add('is-shown');
      threads.filter((th) => Number(th.dataset.at) === i).forEach((th) => {
        th.classList.add('is-on');
        const dot = dots(th);
        const [from, to] = dot.dataset.swap.split(' ');
        dot.classList.replace(from, to);
      });
    }, times[i] ?? 900 * (i + 1));
  });
}

// ---------- status: notes in the margin light the part they explain ----------
const board = document.querySelector('[data-notes]');
if (board) {
  const panel = board.querySelector('.panel');
  const notes = [...board.querySelectorAll('.note')];
  const partOf = (note) => panel.querySelector(`[data-part="${note.dataset.part}"]`);
  let pinned = null;

  const light = (note) => {
    panel.querySelectorAll('.is-lit').forEach((el) => el.classList.remove('is-lit'));
    const target = note || pinned;
    if (target) partOf(target)?.classList.add('is-lit');
  };

  notes.forEach((note) => {
    note.addEventListener('mouseenter', () => light(note));
    note.addEventListener('mouseleave', () => light(null));
    note.addEventListener('focus', () => light(note));
    note.addEventListener('blur', () => light(null));
    note.addEventListener('click', () => {
      pinned = pinned === note ? null : note;
      notes.forEach((n) => n.setAttribute('aria-pressed', String(n === pinned)));
      light(pinned);
    });
  });

  // on a wide screen each note sits level with the part it explains
  const wide = window.matchMedia('(min-width: 1101px)');
  const place = () => {
    if (!wide.matches) { board.classList.remove('is-placed'); return; }
    board.classList.add('is-placed');
    for (const column of board.querySelectorAll('.notes')) {
      const top = column.getBoundingClientRect().top;
      let floor = 0;
      for (const note of column.querySelectorAll('.note')) {
        const part = partOf(note);
        if (!part) continue;
        const y = Math.max(floor, Math.round(part.getBoundingClientRect().top - top + 10));
        note.style.setProperty('--top', `${y}px`);
        floor = y + note.offsetHeight + 16;
      }
    }
  };
  place();
  wide.addEventListener('change', place);
  window.addEventListener('resize', place);
  document.fonts?.ready.then(place);
}

// ---------- how: the pinned fragment follows the statement in the middle of the screen ----------
const how = document.querySelector('.how');
if (how) {
  const steps = [...how.querySelectorAll('.how-step')];
  const set = (i) => {
    how.dataset.state = String(i);
    steps.forEach((s, n) => s.classList.toggle('is-active', n === i));
  };
  set(0);
  // narrow screens: each state sits under its own statement instead of on the pinned stage
  const card = how.querySelector('.stage-card');
  const views = [...card.querySelectorAll('.state-view')];
  const narrow = window.matchMedia('(max-width: 900px)');
  const arrange = () => {
    if (narrow.matches) views.forEach((view, i) => steps[i]?.append(view));
    else views.forEach((view) => card.append(view));
  };
  arrange();
  narrow.addEventListener('change', arrange);
  const watch = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) set(steps.indexOf(entry.target));
  }, { rootMargin: '-45% 0px -45% 0px' });
  steps.forEach((s) => watch.observe(s));
}

// ---------- the merge rule: pick what happened, see the task's path ----------
const tabsRoot = document.querySelector('[data-tabs]');
if (tabsRoot) {
  const tabs = [...tabsRoot.querySelectorAll('[role="tab"]')];
  const panels = tabs.map((t) => document.getElementById(t.getAttribute('aria-controls')));
  panels.forEach((p) => p.querySelectorAll('.band').forEach((b, i) => b.style.setProperty('--b', i)));

  const select = (index, focus) => {
    tabs.forEach((tab, i) => {
      const on = i === index;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      panels[i].hidden = !on;
    });
    const panel = panels[index];
    panel.classList.remove('is-entering');
    void panel.offsetWidth;
    panel.classList.add('is-entering');
    if (focus) tabs[index].focus();
  };

  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => select(i));
    tab.addEventListener('keydown', (e) => {
      const last = tabs.length - 1;
      const fwd = rtl ? 'ArrowLeft' : 'ArrowRight';
      const back = rtl ? 'ArrowRight' : 'ArrowLeft';
      const to = { ArrowDown: i + 1, [fwd]: i + 1, ArrowUp: i - 1, [back]: i - 1, Home: 0, End: last }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      select((to + tabs.length) % tabs.length, true);
    });
  });
  panels.forEach((p, i) => { p.hidden = i !== 0; });
}

// ---------- install: uv or pipx, and copy ----------
const COMMANDS = {
  uv: 'uv tool install git+https://github.com/Mahmoud6901/t3-event-workflow',
  pipx: 'pipx install git+https://github.com/Mahmoud6901/t3-event-workflow',
};
const SETUP = 't3-workflow install';
let tool = 'uv';
const installCode = document.querySelector('[data-cmd="install"]');
const status = document.querySelector('.copy-status');

// a long command wraps only at a space or after a slash, never inside a name
function showCommand(el, text) {
  const nodes = [];
  text.split(/(\s+)/).forEach((word) => {
    if (!word) return;
    if (/^\s+$/.test(word)) { nodes.push(word); return; }
    word.split(/(?<=\/)(?=[^/])/).forEach((part, i) => {
      if (i) nodes.push(document.createElement('wbr'));
      const span = document.createElement('span');
      span.textContent = part;
      nodes.push(span);
    });
  });
  el.replaceChildren(...nodes);
}

document.querySelectorAll('[data-tool]').forEach((button, _, all) => {
  button.addEventListener('click', () => {
    tool = button.dataset.tool;
    all.forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
    if (installCode) showCommand(installCode, COMMANDS[tool]);
  });
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    area.remove();
    return ok;
  }
}

document.querySelectorAll('[data-copy]').forEach((button) => {
  const label = button.textContent;
  button.addEventListener('click', async () => {
    const what = button.dataset.copy;
    const text = what === 'install' ? COMMANDS[tool] : what === 'setup' ? SETUP : `${COMMANDS[tool]}\n${SETUP}`;
    const ok = await copyText(text);
    if (status) {
      status.textContent = ok
        ? (what === 'all' ? WORDS.copiedAll : WORDS.copiedOne + text)
        : WORDS.blocked;
    }
    if (!ok) return;
    button.classList.add('is-done');
    button.textContent = WORDS.copied;
    clearTimeout(button.__reset);
    button.__reset = setTimeout(() => { button.classList.remove('is-done'); button.textContent = label; }, 1800);
  });
});

// ---------- weighted wheel scrolling; touch and reduced motion keep their own ----------
document.addEventListener('DOMContentLoaded', () => {
  if (!window.Lenis || reduceMotion || !finePointer) return;
  // eslint-disable-next-line no-new
  new window.Lenis({ autoRaf: true, lerp: 0.12, anchors: true });
});
