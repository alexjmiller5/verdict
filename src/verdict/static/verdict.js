/* Verdict client: turns a published page into a review form.
 *
 * Page contract (what the agent writes):
 *   [data-item="id"]                   something to answer or annotate
 *   [data-choices="a|b|c"]             pick one; data-default picks the recommendation (else the first)
 *   [data-input]                       free text answer
 *   [data-section="name"]              gets an "accept recommendations" button for its items
 * Everything else is annotatable in comment mode (tap any element).
 */
(() => {
  const id = location.pathname.split("/")[2];
  const api = `/api/reviews/${id}`;
  let state = null;
  let saveTimer = null;
  let commentMode = false;

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) node.append(c);
    return node;
  };

  // Swap a container's children around `keep` without detaching it: re-rendering
  // after every autosave must not blur the text box the reviewer is typing in.
  const rebuild = (parent, keep, before, after) => {
    for (const c of [...parent.children]) if (c !== keep) c.remove();
    if (keep && keep.parentNode !== parent) parent.append(keep);
    if (keep) keep.before(...before);
    else parent.append(...before);
    parent.append(...after);
  };
  const typing = (box) => box === document.activeElement;

  const items = () => [...document.querySelectorAll("[data-item]")];
  const defaultFor = (item) => {
    const choices = (item.dataset.choices || "").split("|").filter(Boolean);
    return item.dataset.default || choices[0] || null;
  };
  const answer = (key) => state.answers[key] || {};

  // --- persistence -------------------------------------------------------

  const put = async (patch) => {
    const r = await fetch(`${api}/state`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!r.ok) throw new Error(`save failed: ${r.status}`);
    state = await r.json();
    render();
  };

  const setAnswer = (key, patch) => {
    state.answers[key] = { ...answer(key), ...patch, defaulted: false };
    queueSave({ answers: { [key]: state.answers[key] } });
  };

  const queueSave = (patch) => {
    pending = { ...pending, ...patch, answers: { ...(pending.answers || {}), ...(patch.answers || {}) } };
    status("saving…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, 400);
  };
  let pending = {};
  const flush = async () => {
    const patch = pending;
    pending = {};
    if (!Object.keys(patch).length) return;
    try {
      await put(patch);
      status("saved");
    } catch (e) {
      status("not saved - retrying");
      pending = { ...patch, ...pending };
      saveTimer = setTimeout(flush, 2000);
    }
  };

  // --- item slots ----------------------------------------------------------

  const slotFor = (item) => {
    const key = item.dataset.item;
    let slot = item.nextElementSibling;
    if (!slot || !slot.classList.contains("vd-slot") || slot.dataset.for !== key) {
      slot = el("div", { class: "vd-slot", "data-for": key });
      item.after(slot);
    }
    const current = answer(key);
    const before = [];
    const choices = (item.dataset.choices || "").split("|").filter(Boolean);
    if (choices.length) {
      const def = defaultFor(item);
      const group = el("div", { class: "vd-choices" });
      for (const choice of choices) {
        const picked = current.choice === choice;
        const btn = el(
          "button",
          {
            type: "button",
            class: `vd-choice${picked ? " vd-picked" : ""}${choice === def ? " vd-default" : ""}`,
            onclick: () => setAnswer(key, { choice }),
          },
          choice
        );
        group.append(btn);
      }
      before.push(group);
    }
    let box = null;
    if ("input" in item.dataset) {
      box =
        slot.querySelector(":scope > textarea") ||
        el("textarea", {
          class: "vd-text",
          placeholder: item.dataset.input || "your answer",
          oninput: (e) => setAnswer(key, { text: e.target.value }),
        });
      if (!typing(box)) box.value = current.text || "";
    }
    const mine = state.comments.filter((c) => c.item === key);
    const noteBtn = el(
      "button",
      { type: "button", class: "vd-note", onclick: () => compose(item, key) },
      mine.length ? `notes (${mine.length})` : "note"
    );
    const after = [noteBtn];
    if (current.defaulted) after.push(el("span", { class: "vd-tag" }, "accepted recommendation"));
    rebuild(slot, box, before, after);
  };

  const acceptAll = (scope) => {
    const patch = {};
    for (const item of scope.querySelectorAll("[data-item][data-choices]")) {
      const key = item.dataset.item;
      if (!answer(key).choice) {
        state.answers[key] = { ...answer(key), choice: defaultFor(item), defaulted: true };
        patch[key] = state.answers[key];
      }
    }
    if (Object.keys(patch).length) queueSave({ answers: patch });
    render();
  };

  // --- comments ------------------------------------------------------------

  const cssPath = (node) => {
    const parts = [];
    while (node && node.nodeType === 1 && node !== document.body) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`#${node.id}`);
        break;
      }
      const siblings = [...node.parentNode.children].filter((n) => n.tagName === node.tagName);
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
      node = node.parentNode;
    }
    return parts.join(" > ");
  };

  const compose = (target, itemKey = null, existingIndex = null) => {
    const existing = existingIndex === null ? null : state.comments[existingIndex];
    const snippet = (target.innerText || target.alt || target.tagName).trim().slice(0, 120);
    const sheet = el("div", { class: "vd-sheet" });
    const box = el("textarea", { class: "vd-text", placeholder: "what should change here?" });
    box.value = existing ? existing.text : "";
    const save = () => {
      const text = box.value.trim();
      const comment = {
        item: itemKey || (target.closest("[data-item]") || {}).dataset?.item || null,
        selector: cssPath(target),
        snippet,
        text,
        at: new Date().toISOString(),
      };
      if (existing) {
        if (text) state.comments[existingIndex] = { ...existing, text };
        else state.comments.splice(existingIndex, 1);
      } else if (text) state.comments.push(comment);
      queueSave({ comments: state.comments });
      sheet.remove();
      render();
    };
    sheet.append(
      el("div", { class: "vd-sheet-head" }, `Note on: ${snippet || target.tagName}`),
      box,
      el(
        "div",
        { class: "vd-row" },
        el("button", { type: "button", class: "vd-primary", onclick: save }, existing ? "Update" : "Add note"),
        el("button", { type: "button", onclick: () => sheet.remove() }, "Cancel"),
        existing ? el("button", { type: "button", class: "vd-danger", onclick: () => { box.value = ""; save(); } }, "Delete") : ""
      )
    );
    document.body.append(sheet);
    box.focus();
  };

  const renderPins = () => {
    document.querySelectorAll(".vd-pin").forEach((p) => p.remove());
    state.comments.forEach((c, i) => {
      let target = null;
      try { target = c.selector ? document.querySelector(c.selector) : null; } catch { target = null; }
      if (!target && c.item) target = document.querySelector(`[data-item="${CSS.escape(c.item)}"]`);
      if (!target) return;
      target.classList.add("vd-annotated");
      const pin = el("button", { type: "button", class: "vd-pin", title: c.text, onclick: (e) => { e.stopPropagation(); compose(target, c.item, i); } }, String(i + 1));
      target.append(pin);
    });
  };

  const onPageClick = (e) => {
    if (!commentMode) return;
    if (e.target.closest(".vd-bar, .vd-sheet, .vd-slot, .vd-pin")) return;
    e.preventDefault();
    e.stopPropagation();
    compose(e.target);
  };

  // --- toolbar -------------------------------------------------------------

  let bar, statusEl, general;
  const status = (text) => { if (statusEl) statusEl.textContent = text; };
  const toggleCommentMode = () => {
    commentMode = !commentMode;
    document.body.classList.toggle("vd-comment-mode", commentMode);
    renderBar();
  };

  const renderBar = () => {
    if (!bar) {
      bar = el("div", { class: "vd-bar" });
      general = el("textarea", {
        class: "vd-text vd-general",
        placeholder: "general notes for the agent",
        oninput: (e) => queueSave({ general: e.target.value }),
      });
      document.body.append(bar);
    }
    const done = state.status === "done";
    const closed = state.status === "closed";
    if (!typing(general)) general.value = state.general || "";
    const answered = items().filter((i) => answer(i.dataset.item).choice || answer(i.dataset.item).text).length;
    const total = items().filter((i) => i.dataset.choices || "input" in i.dataset).length;
    statusEl = el("span", { class: "vd-status" }, closed ? "closed by the agent" : done ? "done - the agent has your answers" : `${answered}/${total} answered · ${state.comments.length} notes`);
    rebuild(
      bar,
      general,
      [el("div", { class: "vd-row" }, el("strong", {}, state.title), statusEl)],
      [el("div", { class: "vd-row" },
        el("button", { type: "button", class: `vd-toggle${commentMode ? " vd-on" : ""}`, onclick: toggleCommentMode, title: "Ctrl+Option+C" }, commentMode ? "tap anything to add a note…" : "comment mode"),
        el("button", { type: "button", onclick: () => acceptAll(document) }, "accept all recommendations"),
        closed ? "" : done
          ? el("button", { type: "button", onclick: () => put({ status: "open" }) }, "reopen")
          : el("button", { type: "button", class: "vd-primary", onclick: () => { acceptAll(document); flush().then(() => put({ status: "done" })); } }, "Done")
      )]
    );
  };

  const render = () => {
    items().forEach(slotFor);
    document.querySelectorAll("[data-section]").forEach((section) => {
      let btn = section.querySelector(":scope > .vd-section-accept");
      if (!btn) {
        btn = el("button", { type: "button", class: "vd-section-accept", onclick: () => acceptAll(section) }, "accept this section's recommendations");
        section.append(btn);
      }
    });
    renderPins();
    renderBar();
    document.body.classList.toggle("vd-done", state.status !== "open");
  };

  const boot = async () => {
    const r = await fetch(api);
    if (!r.ok) {
      document.body.prepend(el("div", { class: "vd-bar" }, "This review no longer exists."));
      return;
    }
    state = await r.json();
    document.addEventListener("click", onPageClick, true);
    // Ctrl+Option+C toggles comment mode; e.code, since Option changes e.key
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.altKey && !e.metaKey && e.code === "KeyC") {
        e.preventDefault();
        toggleCommentMode();
      }
    });
    render();
    // keep in sync when the agent closes it or another device edits
    const poll = async () => {
      try {
        const s = await (await fetch(`${api}?wait=30&version=${state.version}`)).json();
        if (s.version !== state.version && !Object.keys(pending).length) { state = s; render(); }
      } catch {}
      setTimeout(poll, 1000);
    };
    poll();
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
