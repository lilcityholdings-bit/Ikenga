"use strict";
// AI Ladder front end. Plain JavaScript, no build step, no third-party code,
// so it runs on locked-down school Chromebooks and old iPads alike.

const app = document.getElementById("app");
const nav = document.getElementById("nav");
let CUR = null; // curriculum: levels, skills

// ── tiny helpers ──
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function show(...nodes) { app.replaceChildren(...nodes); app.focus({ preventScroll: true }); window.scrollTo(0, 0); }
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};

async function api(method, path, body, auth) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth === "student") headers["X-Student-Code"] = store.get("studentCode") || "";
  if (auth === "teacher") headers["X-Teacher-Key"] = store.get("teacherKey") || "";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const isJson = (res.headers.get("Content-Type") || "").includes("json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) { const e = new Error((data && data.error) || "Something went wrong"); e.status = res.status; throw e; }
  return data;
}

// Read-aloud for early readers, using the browser's built-in voice.
function sayBtn(text) {
  if (!("speechSynthesis" in window)) return null;
  return h("button", { class: "say", type: "button", "aria-label": "Read aloud", title: "Read aloud",
    onclick: () => { speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(text); u.rate = .9; speechSynthesis.speak(u); } }, "🔊");
}
const levelName = (i) => CUR.levels[i].name;
const skillById = (id) => CUR.skills.find((s) => s.id === id);
function levelTag(i) { return h("span", { class: `tag lv${i}` }, `${levelName(i)} · ${CUR.levels[i].grades}`); }
function bar(frac) { const s = h("span"); s.style.width = `${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%`; return h("div", { class: "bar" }, s); }
function ladder(level) {
  return h("div", { class: "ladder", "aria-label": `Level ${level + 1} of 6: ${levelName(level)}` },
    CUR.levels.map((lv, i) => h("div", { class: `lv${i} ${i <= level ? "on" : ""} ${i === level ? "here" : ""}` }, lv.name)));
}
function setNav(items) { nav.replaceChildren(...items.map(([label, fn]) => h("button", { class: "link", onclick: fn }, label))); }
function errBox() { return h("p", { class: "error", role: "alert" }); }

// ────────────────────────────── home ──────────────────────────────
function home() {
  document.body.classList.remove("young");
  setNav([]);
  show(
    h("div", { class: "hero" },
      h("h1", {}, "Learn AI by playing 🪜"),
      h("p", { class: "muted" }, "Games and challenges from kindergarten to a bachelor's degree. Climb the ladder one skill at a time.")),
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("h2", {}, "🎒 I'm a student"),
        h("p", {}, "Join your class with the code your teacher gives you."),
        h("button", { class: "primary big", onclick: studentJoin }, "Join my class"),
        h("p", {}), h("button", { class: "choice", onclick: studentLogin }, "I already have a login code"),
        h("button", { class: "choice", onclick: soloStart }, "Practice on my own")),
      h("div", { class: "card" }, h("h2", {}, "🍎 I'm a teacher"),
        h("p", {}, "Make a class in 30 seconds. See every student's skills on one screen."),
        h("button", { class: "primary big", onclick: teacherCreate }, "Create a class"),
        h("p", {}), h("button", { class: "choice", onclick: teacherOpen }, "Open my class with my teacher key"))),
    h("h2", {}, "The ladder"),
    h("div", { class: "grid" }, CUR.levels.map((lv, i) =>
      h("div", { class: "card" }, h("span", { class: `tag lv${i}` }, `Level ${i + 1}`), h("h2", {}, lv.name),
        h("p", { class: "muted" }, `${lv.grades} · ages ${lv.ages}`), h("p", { class: "small" }, `${CUR.counts[i]} challenges`)))),
    h("h2", {}, "Six skills"),
    h("div", { class: "grid" }, CUR.skills.map((s) =>
      h("div", { class: "card" }, h("strong", {}, `${s.icon} ${s.name}`), h("p", { class: "muted small" }, s.blurb)))),
  );
}

// ─────────────────────────── students ────────────────────────────
function studentJoin() {
  const code = h("input", { class: "code", autocomplete: "off", maxlength: 6, placeholder: "ABC234", "aria-label": "Class code" });
  const nick = h("input", { maxlength: 24, autocomplete: "off", placeholder: "e.g. Rocket Maya", "aria-label": "Nickname" });
  const err = errBox();
  const go = async () => {
    err.textContent = "";
    try {
      const me = await api("POST", "/api/join", { join_code: code.value, nickname: nick.value });
      store.set("studentCode", me.login_code);
      welcome(me);
    } catch (e) { err.textContent = e.message; }
  };
  setNav([["← Home", home]]);
  show(h("div", { class: "card" }, h("h1", {}, "Join your class"),
    h("label", {}, "Class code"), code,
    h("label", {}, "Pick a nickname ", h("span", { class: "muted small" }, "(don't use your full real name)")), nick,
    err, h("button", { class: "primary big", onclick: go }, "Join")));
  code.focus();
}

function soloStart() {
  const nick = h("input", { maxlength: 24, autocomplete: "off", placeholder: "Nickname", "aria-label": "Nickname" });
  const band = h("select", { "aria-label": "Grade band" }, CUR.levels.map((lv, i) => h("option", { value: i }, `${lv.grades} — ${lv.name}`)));
  const err = errBox();
  const go = async () => {
    try {
      const me = await api("POST", "/api/join", { nickname: nick.value, band: Number(band.value) });
      store.set("studentCode", me.login_code);
      welcome(me);
    } catch (e) { err.textContent = e.message; }
  };
  setNav([["← Home", home]]);
  show(h("div", { class: "card" }, h("h1", {}, "Practice on your own"),
    h("label", {}, "Nickname"), nick, h("label", {}, "Where are you in school?"), band, err,
    h("button", { class: "primary big", onclick: go }, "Start")));
}

function studentLogin() {
  const code = h("input", { class: "code", autocomplete: "off", maxlength: 9, placeholder: "ABCD-2345", "aria-label": "Login code" });
  const err = errBox();
  const go = async () => {
    try {
      const me = await api("POST", "/api/login", { code: code.value });
      store.set("studentCode", me.login_code);
      studentHome();
    } catch (e) { err.textContent = e.message; }
  };
  setNav([["← Home", home]]);
  show(h("div", { class: "card" }, h("h1", {}, "Welcome back!"), h("label", {}, "Your login code"), code, err,
    h("button", { class: "primary big", onclick: go }, "Log in")));
  code.focus();
}

function welcome(me) {
  show(h("div", { class: "card hero" }, h("h1", {}, `Welcome, ${me.nickname}! 🎉`),
    h("p", {}, "This is your login code. Write it down or ask your teacher to print it — you'll need it on a new computer."),
    h("p", { class: "joincode" }, me.login_code),
    h("button", { class: "primary big", onclick: studentHome }, "Let's go!")));
}

async function studentHome() {
  let me;
  try { me = await api("GET", "/api/me", undefined, "student"); }
  catch (e) { if (e.status === 401) { store.set("studentCode", null); return home(); } throw e; }
  document.body.classList.toggle("young", me.band <= 1);
  setNav([["Log out", () => { store.set("studentCode", null); home(); }]]);
  const lvl = me.level;
  const nextName = lvl < 5 ? levelName(lvl + 1) : "mastery";
  const boardSlot = h("div");
  show(
    h("div", { class: "card" },
      h("div", { class: "row" }, h("h1", {}, `Hi, ${me.nickname}!`)),
      h("p", {}, "You're working at ", levelTag(lvl)),
      ladder(lvl),
      h("p", { class: "small muted" }, `${Math.round(me.level_progress * 100)}% of the way to ${nextName}`), bar(me.level_progress),
      h("p", {}),
      h("button", { class: "primary big", onclick: playNext }, me.attempts ? "Next challenge ▶" : "Start my first challenge ▶"),
      h("p", { class: "small muted" }, `${me.attempts} challenges done · rating ${me.overall}`,
        me.growth ? ` · ${me.growth > 0 ? "+" : ""}${me.growth} since you started` : "",
        me.class ? ` · class: ${me.class.name}` : "")),
    h("h2", {}, "My skills"),
    h("div", { class: "grid" }, CUR.skills.map((s) => {
      const k = me.skills[s.id];
      return h("div", { class: "card skill" }, h("strong", {}, `${s.icon} ${s.name}`), h("span", { class: `tag lv${k.level}` }, levelName(k.level)),
        bar(k.progress), h("span", { class: "small muted" }, k.n ? `${k.n} tried · rating ${k.rating}` : "not tried yet"));
    })),
    boardSlot,
    h("p", { class: "small muted" }, `Your login code: ${me.login_code}`));
  if (me.class && me.class.leaderboard) {
    try {
      const board = await api("GET", "/api/leaderboard", undefined, "student");
      boardSlot.replaceChildren(h("h2", {}, "🏆 Class leaderboard"), h("div", { class: "card tablewrap" },
        h("table", {}, h("thead", {}, h("tr", {}, ["#", "Nickname", "Level", "Rating", "Growth"].map((t) => h("th", {}, t)))),
          h("tbody", {}, board.map((r, i) => h("tr", {},
            h("td", {}, i + 1), h("td", {}, r.nickname === me.nickname ? h("strong", {}, `${r.nickname} (you)`) : r.nickname),
            h("td", {}, h("span", { class: `tag lv${r.level}` }, levelName(r.level))), h("td", {}, r.overall),
            h("td", {}, `${r.growth > 0 ? "+" : ""}${r.growth}`)))))));
    } catch { /* leaderboard turned off meanwhile */ }
  }
}

// ─────────────────────────── challenges ──────────────────────────
async function playNext() {
  const item = await api("GET", "/api/next", undefined, "student");
  const skill = skillById(item.skill);
  const err = errBox();
  const checkBtn = h("button", { class: "primary big", disabled: true }, "Check my answer");
  const after = h("div");
  const r = RENDER[item.type](item, (ready) => { checkBtn.disabled = !ready; }, () => checkBtn.click());
  let inflight = false;
  checkBtn.addEventListener("click", async () => {
    if (inflight) return;
    inflight = true; checkBtn.disabled = true; err.textContent = "";
    let res;
    try { res = await api("POST", "/api/answer", { item_id: item.id, response: r.value() }, "student"); }
    catch (e) { err.textContent = e.message; checkBtn.disabled = false; inflight = false; return; }
    r.lock(res.feedback);
    checkBtn.remove();
    after.replaceChildren(result(res));
    after.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  setNav([["← My progress", studentHome]]);
  show(h("div", { class: "card" },
    h("div", { class: "row" }, levelTag(item.level), h("span", { class: "tag" }, `${skill.icon} ${skill.name}`)),
    h("p", { class: "prompt" }, item.prompt, sayBtn(item.prompt)),
    r.node, err, r.hideCheck ? null : checkBtn), after);
}

function result(res) {
  const pct = Math.round(res.score * 100);
  const good = res.correct, ok = res.score >= 0.5;
  const d = res.rating_after - res.rating_before;
  const stars = res.score >= 0.999 ? "⭐⭐⭐" : res.score >= 0.66 ? "⭐⭐" : res.score > 0 ? "⭐" : "";
  return h("div", {},
    res.level_after > res.level_before ? h("div", { class: "card celebrate" }, `🎉 Level up! You reached ${levelName(res.level_after)}!`) : null,
    h("div", { class: `card feedback ${good ? "good" : ok ? "" : "bad"}` },
      h("h2", {}, good ? `Correct! ${stars}` : ok ? `Nice try — ${pct}% ${stars}` : `Not quite — ${pct}% ${stars}`),
      h("p", {}, res.why, sayBtn(res.why)),
      h("p", { class: "small" }, `${skillById(res.skill).name}: ${res.rating_before} → ${res.rating_after} `,
        h("span", { class: d >= 0 ? "delta-up" : "delta-down" }, `(${d >= 0 ? "+" : ""}${d})`))),
    h("button", { class: "primary big", onclick: playNext }, "Next challenge ▶"),
    h("p", {}), h("button", { class: "choice", onclick: studentHome }, "See my progress"));
}

// Each renderer returns { node, value(), lock(feedback) }. onReady(bool) enables Check.
const RENDER = {
  mc(item, onReady, submit) {
    let pick = null;
    const btns = item.choices.map((c, i) => h("button", { class: "choice", onclick: () => {
      pick = i; btns.forEach((b, j) => b.classList.toggle("selected", j === i)); onReady(true); submit();
    } }, c));
    return {
      node: h("div", {}, btns), hideCheck: true, value: () => pick,
      lock(fb) { btns.forEach((b, i) => { b.disabled = true; if (i === fb.correct_choice) b.classList.add("right"); else if (i === pick) b.classList.add("wrong"); }); },
    };
  },

  sort(item, onReady) {
    const picks = item.cards.map(() => null);
    const rows = item.cards.map((text, ci) => {
      const bs = item.bins.map((bin, bi) => h("button", { type: "button", onclick: () => {
        picks[ci] = bi; bs.forEach((b, j) => b.classList.toggle("selected", j === bi)); onReady(picks.every((p) => p !== null));
      } }, bin));
      const card = h("div", { class: "sortcard" }, h("div", {}, h("strong", {}, text)), h("div", { class: "row" }, bs));
      card._bs = bs;
      return card;
    });
    return {
      node: h("div", {}, rows), value: () => picks,
      lock(fb) { rows.forEach((row, i) => { row._bs.forEach((b) => (b.disabled = true)); row.classList.add(fb.right[i] ? "right" : "wrong");
        if (!fb.right[i]) row.append(h("p", { class: "small" }, `Answer: ${item.bins[fb.truth[i]]}`)); }); },
    };
  },

  order(item, onReady) {
    const chosen = [];
    const pool = h("div", { class: "chips" });
    const list = h("ol", { class: "orderlist" });
    let locked = false;
    const draw = () => {
      pool.replaceChildren(...item.steps.map((s, i) => chosen.includes(i) ? null :
        h("button", { onclick: () => { if (!locked) { chosen.push(i); draw(); } } }, s)).filter(Boolean));
      list.replaceChildren(...chosen.map((i, k) => h("li", {}, h("button", { class: "selected", disabled: locked,
        onclick: () => { chosen.splice(k, 1); draw(); } }, item.steps[i]))));
      onReady(chosen.length === item.steps.length);
    };
    draw();
    return {
      node: h("div", {}, h("p", { class: "small muted" }, "Tap the steps in order. Tap a placed step to take it back."),
        h("strong", {}, "Your order:"), list, h("p", {}), pool),
      value: () => chosen,
      lock(fb) {
        locked = true; draw();
        list.after(h("div", { class: "card" }, h("strong", {}, "Correct order:"),
          h("ol", {}, fb.correct_order.map((i) => h("li", {}, item.steps[i])))));
      },
    };
  },

  spot(item, onReady) {
    const marked = new Set();
    const btns = item.sentences.map((s, i) => h("button", { class: "choice", "aria-pressed": "false", onclick: (e) => {
      marked.has(i) ? marked.delete(i) : marked.add(i);
      e.currentTarget.classList.toggle("selected", marked.has(i));
      e.currentTarget.setAttribute("aria-pressed", String(marked.has(i)));
      e.currentTarget.firstChild.textContent = marked.has(i) ? "❌ " : "";
    } }, h("span", {}), s));
    onReady(true);
    return {
      node: h("div", {}, h("p", { class: "small muted" }, "Tap a sentence to mark it false ❌. Tap again to unmark."), btns),
      value: () => [...marked],
      lock(fb) { btns.forEach((b, i) => { b.disabled = true; b.classList.remove("selected"); b.classList.add(fb.right[i] ? "right" : "wrong");
        b.append(h("span", { class: "small" }, fb.false_sentences.includes(i) ? "  — FALSE" : "  — true")); }); },
    };
  },

  prompt(item, onReady) {
    const picks = item.slots.map(() => null);
    const preview = h("div", { class: "promptpreview", "aria-live": "polite" });
    const upd = () => {
      preview.textContent = picks.map((p, i) => p === null ? "…" : item.slots[i].options[p]).filter((t) => !/^\(skip/.test(t)).join(" ");
      onReady(picks.every((p) => p !== null));
    };
    const groups = item.slots.map((slot, si) => {
      const bs = slot.options.map((o, oi) => h("button", { onclick: () => { picks[si] = oi; bs.forEach((b, j) => b.classList.toggle("selected", j === oi)); upd(); } }, o));
      const g = h("div", { class: "sortcard" }, h("strong", {}, slot.label), h("div", { class: "chips" }, bs));
      g._bs = bs;
      return g;
    });
    upd();
    return {
      node: h("div", {}, groups, h("strong", {}, "Your prompt:"), preview),
      value: () => picks,
      lock(fb) {
        groups.forEach((g, i) => { g._bs.forEach((b) => (b.disabled = true)); const t = fb.slots[i];
          g.classList.add(t.pts === 2 ? "right" : "wrong");
          if (t.pts < 2) g.append(h("p", { class: "small" }, `Stronger: “${t.best}”`)); });
      },
    };
  },

  teach(item, onReady) {
    const labels = item.train.map(() => null);
    const rows = item.train.map((text, ti) => {
      const bs = item.classes.map((c, ci) => h("button", { onclick: () => {
        labels[ti] = ci; bs.forEach((b, j) => b.classList.toggle("selected", j === ci)); onReady(labels.every((l) => l !== null));
      } }, c));
      const r = h("div", { class: "sortcard" }, h("strong", {}, text), h("div", { class: "row" }, bs));
      r._bs = bs;
      return r;
    });
    const test = h("div", { class: "card" }, h("strong", {}, "🤖 After training, the robot will be tested on:"),
      h("ul", {}, item.test.map((t) => h("li", {}, t))));
    return {
      node: h("div", {}, h("p", { class: "small muted" }, "Step 1: Label every training example."), rows, test),
      value: () => labels,
      lock(fb) {
        rows.forEach((r, i) => { r._bs.forEach((b) => (b.disabled = true)); if (!fb.labels_right[i]) r.classList.add("wrong"); });
        test.replaceChildren(h("strong", {}, `🤖 Test results: the robot got ${fb.right.filter(Boolean).length} of ${fb.right.length} right`),
          h("ul", {}, item.test.map((t, i) => h("li", {}, `${t} → robot said ${item.classes[fb.predictions[i]]} `, fb.right[i] ? "✅" : `❌ (it's ${item.classes[fb.truth[i]]})`))));
        if (fb.labels_right.some((x) => !x)) test.append(h("p", { class: "small" }, "Some training labels were wrong (marked in red) — the robot learned your mistakes!"));
      },
    };
  },

  tune(item, onReady) {
    const out = h("strong", {}, "0.10");
    const slider = h("input", { type: "range", min: item.lr_min, max: item.lr_max, step: "0.01", value: "0.10", "aria-label": "Learning rate",
      oninput: () => { out.textContent = Number(slider.value).toFixed(2); } });
    const canvas = h("canvas", { width: 640, height: 260, "aria-label": "Loss curve" });
    onReady(true);
    return {
      node: h("div", {}, h("p", {}, `Start: w = ${item.start} · Goal: w = ${item.target} · Steps: ${item.steps}`),
        h("label", {}, "Learning rate: ", out), slider,
        h("p", { class: "small muted" }, "Tip: each step moves w by −(learning rate) × 2 × (w − 3).")),
      value: () => Number(slider.value),
      lock(fb) { slider.disabled = true; slider.after(canvas, h("p", {}, `Final w = ${fb.path[fb.path.length - 1].toPrecision(4)}, loss = ${fb.final_loss.toPrecision(3)}`)); drawLoss(canvas, fb.path, item.target); },
    };
  },
};

function drawLoss(canvas, path, target) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height, pad = 36;
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--ink").trim(), acc = css.getPropertyValue("--accent").trim(), line = css.getPropertyValue("--line").trim();
  const loss = path.map((w) => Math.log10(1 + (w - target) ** 2));
  const maxL = Math.max(...loss, 1);
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = line; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, pad / 2); ctx.lineTo(pad, H - pad); ctx.lineTo(W - pad / 2, H - pad); ctx.stroke();
  ctx.fillStyle = ink; ctx.font = "13px system-ui, sans-serif";
  ctx.fillText("loss (log scale)", pad + 4, pad / 2 + 10); ctx.fillText("step", W - pad - 10, H - 10);
  const x = (i) => pad + (i / Math.max(1, path.length - 1)) * (W - pad * 1.5);
  const y = (l) => H - pad - (l / maxL) * (H - pad * 1.5);
  ctx.strokeStyle = acc; ctx.lineWidth = 3; ctx.beginPath();
  loss.forEach((l, i) => (i ? ctx.lineTo(x(i), y(l)) : ctx.moveTo(x(i), y(l)))); ctx.stroke();
  ctx.fillStyle = acc; loss.forEach((l, i) => { ctx.beginPath(); ctx.arc(x(i), y(l), 4, 0, 7); ctx.fill(); });
}

// ─────────────────────────── teachers ────────────────────────────
function teacherCreate() {
  const name = h("input", { maxlength: 60, placeholder: "e.g. Ms. Rivera — 3rd period", "aria-label": "Class name" });
  const band = h("select", { "aria-label": "Grade band" }, CUR.levels.map((lv, i) => h("option", { value: i }, `${lv.grades} — ${lv.name}`)));
  const lb = h("input", { type: "checkbox" });
  const syncLb = () => { lb.checked = Number(band.value) >= 2; };
  band.addEventListener("change", syncLb); syncLb();
  const err = errBox();
  const go = async () => {
    try {
      const r = await api("POST", "/api/classes", { name: name.value, band: Number(band.value), leaderboard: lb.checked });
      store.set("teacherKey", r.teacher_key);
      setNav([["← Home", home]]);
      show(h("div", { class: "card" }, h("h1", {}, "Your class is ready ✅"),
        h("p", {}, "Students join with this code:"), h("p", { class: "joincode" }, r.join_code),
        h("h2", {}, "⚠️ Save your teacher key"),
        h("p", {}, "This is the only way to open your dashboard from another computer. Keep it private — anyone with it can see your class."),
        h("p", { class: "keybox" }, r.teacher_key),
        h("button", { class: "primary big", onclick: teacherDash }, "Go to my dashboard")));
    } catch (e) { err.textContent = e.message; }
  };
  setNav([["← Home", home]]);
  show(h("div", { class: "card" }, h("h1", {}, "Create a class"),
    h("label", {}, "Class name"), name, h("label", {}, "Grade band"), band,
    h("p", { class: "small muted" }, "Challenges adapt to each student, from one level below this band to two above."),
    h("label", { class: "check" }, lb, "Show students a class leaderboard"),
    h("p", { class: "small muted" }, "Off by default for grades K-5. Growth is shown next to rank so improvement counts, not just raw score."),
    err, h("button", { class: "primary big", onclick: go }, "Create class")));
  name.focus();
}

function teacherOpen() {
  const key = h("input", { autocomplete: "off", placeholder: "T-…", "aria-label": "Teacher key" });
  const err = errBox();
  const go = async () => {
    store.set("teacherKey", key.value.trim());
    try { await api("GET", "/api/teacher/class", undefined, "teacher"); teacherDash(); }
    catch (e) { store.set("teacherKey", null); err.textContent = e.message; }
  };
  setNav([["← Home", home]]);
  show(h("div", { class: "card" }, h("h1", {}, "Open my class"), h("label", {}, "Teacher key"), key, err,
    h("button", { class: "primary big", onclick: go }, "Open dashboard")));
}

async function teacherDash() {
  let c;
  try { c = await api("GET", "/api/teacher/class", undefined, "teacher"); }
  catch (e) { if (e.status === 401) { store.set("teacherKey", null); return home(); } throw e; }
  document.body.classList.remove("young");
  setNav([["↻ Refresh", teacherDash], ["Sign out", () => { store.set("teacherKey", null); home(); }]]);
  const lb = h("input", { type: "checkbox", checked: c.leaderboard, onchange: async () => {
    await api("PATCH", "/api/teacher/class", { leaderboard: lb.checked }, "teacher");
  } });
  const exportCsv = async () => {
    const res = await fetch("/api/teacher/export.csv", { headers: { "X-Teacher-Key": store.get("teacherKey") } });
    const url = URL.createObjectURL(await res.blob());
    const a = h("a", { href: url, download: "ai-ladder-class.csv" }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  };
  const s = c.students;
  const avg = s.length ? Math.round(s.reduce((t, p) => t + p.overall, 0) / s.length) : null;
  const weakest = s.length ? CUR.skills.map((sk) => [sk, s.reduce((t, p) => t + p.skills[sk.id].rating, 0) / s.length]).sort((a, b) => a[1] - b[1])[0][0] : null;
  const cards = h("div", { class: "cards" }, s.map((p) => h("div", { class: "lc" },
    h("strong", {}, "AI Ladder login"), h("p", {}, p.nickname), h("p", { class: "joincode" }, p.login_code), h("p", { class: "small" }, c.name))));
  show(
    h("div", { class: "card" }, h("h1", {}, c.name), h("p", {}, "Grade band: ", levelTag(c.band)),
      h("p", { class: "muted" }, "Class code for students:"), h("p", { class: "joincode" }, c.join_code),
      h("label", { class: "check" }, lb, "Students can see the class leaderboard")),
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("p", { class: "muted small" }, "Students"), h("p", { class: "joincode" }, s.length)),
      h("div", { class: "card" }, h("p", { class: "muted small" }, "Class average"),
        avg === null ? h("p", {}, "—") : h("p", {}, levelTag(CUR.level_floor.filter((f) => avg >= f).length - 1), ` rating ${avg}`)),
      h("div", { class: "card" }, h("p", { class: "muted small" }, "Skill to focus on next"),
        weakest ? h("p", {}, h("strong", {}, `${weakest.icon} ${weakest.name}`), h("br"), h("span", { class: "small muted" }, weakest.blurb)) : h("p", {}, "—"))),
    h("div", { class: "row" },
      h("button", { onclick: exportCsv, disabled: !s.length }, "⬇ Download CSV (gradebook)"),
      h("button", { onclick: () => window.print(), disabled: !s.length }, "🖨 Print login cards")),
    h("h2", {}, "Students"),
    s.length === 0 ? h("div", { class: "card" }, h("p", {}, `No students yet. Put the code ${c.join_code} on the board and have students choose “Join my class.”`)) :
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Nickname", "Login", "Level", "Rating", "Growth", "Done", "Avg"].map((t) => h("th", {}, t)),
        CUR.skills.map((sk) => h("th", { title: sk.name }, sk.icon)), h("th", {}, ""))),
      h("tbody", {}, s.map((p) => h("tr", {},
        h("td", {}, h("strong", {}, p.nickname)), h("td", {}, p.login_code),
        h("td", {}, h("span", { class: `tag lv${p.level}` }, levelName(p.level))), h("td", {}, p.overall),
        h("td", {}, `${p.growth > 0 ? "+" : ""}${p.growth}`), h("td", {}, p.attempts), h("td", {}, `${Math.round(p.avg_score * 100)}%`),
        CUR.skills.map((sk) => { const k = p.skills[sk.id];
          return h("td", { class: `cell lv${k.level}`, title: `${sk.name}: ${levelName(k.level)}, rating ${k.rating}, ${k.n} tried` }, k.rating); }),
        h("td", {}, h("button", { class: "link danger", onclick: async () => {
          if (!confirm(`Permanently delete ${p.nickname} and all their data?`)) return;
          await api("DELETE", `/api/teacher/students/${p.id}`, undefined, "teacher"); teacherDash();
        } }, "Delete"))))))),
    h("p", { class: "small muted" }, "Cell color = level for that skill. Hover a cell for details. Ratings share one scale from kindergarten (≈500) to a bachelor's degree (≈2000+)."),
    cards);
}

// ── boot ──
(async function boot() {
  try { CUR = await api("GET", "/api/curriculum"); }
  catch { app.textContent = "Can't reach the AI Ladder server. Check your connection and reload."; return; }
  document.getElementById("brand").addEventListener("click", (e) => { e.preventDefault(); home(); });
  if (store.get("studentCode")) studentHome();
  else if (store.get("teacherKey")) teacherDash();
  else home();
})();
window.addEventListener("unhandledrejection", (e) => { const m = document.querySelector(".error"); if (m) m.textContent = (e.reason && e.reason.message) || "Something went wrong"; });
