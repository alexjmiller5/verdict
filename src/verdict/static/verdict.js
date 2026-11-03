/* Verdict client: turns a published page into a review form.
 *
 * Page contract (what the agent writes):
 *   [data-item="id"]                   something to answer or annotate
 *   [data-choices="a|b|c"]             pick one; data-default picks the recommendation (else the first)
 *   [data-input]                       free text answer
 *   [data-section="name"]              gets an "accept recommendations" button for its items
 * Everything else is annotatable in comment mode (tap any element).
 * Done sends only what the reviewer touched; the accept buttons are the way to
 * take a recommendation. A page with nothing to answer gets "Got it" instead.
 */
(() => {
  const id = location.pathname.split("/")[2];
  const api = `/api/reviews/${id}`;
  const feedFrame = window.frameElement?.classList.contains("vd-feed-frame") ? window.frameElement : null;
  if (feedFrame) document.body.classList.add("vd-feed-embedded");
  let state = null;
  let saveTimer = null;
  let commentMode = false;
  let finishing = false;

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

  const confirmDialog = (title, details, label, action, cancelLabel = "Cancel") => {
    document.querySelectorAll(".vd-confirm").forEach((dialog) => { dialog.close(); dialog.remove(); });
    const dialog = el("dialog", { class: "vd-sheet vd-confirm", "aria-labelledby": "vd-confirm-title" });
    const dismiss = () => { dialog.close(); dialog.remove(); };
    dialog.addEventListener("close", () => dialog.remove());
    dialog.append(
      el("div", { class: "vd-sheet-head", id: "vd-confirm-title" }, title),
      ...details,
      el("div", { class: "vd-row" },
        el("button", { type: "button", onclick: dismiss, autofocus: "" }, cancelLabel),
        el("button", { type: "button", class: "vd-primary", onclick: () => { dismiss(); action(); } }, label))
    );
    document.body.append(dialog);
    dialog.showModal();
  };

  const bootIndex = () => {
    const index = document.querySelector(".vd-index");
    const list = document.querySelector("#vd-list"), feed = document.querySelector("#vd-feed");
    let feedReady = false, feedFocus = null;
    const emptyFeed = () => { feed.querySelector(".vd-feed-empty").hidden = !!feed.querySelector(".vd-feed-post"); };
    index.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
      const showFeed = button.dataset.view === "feed";
      if (showFeed) {
        feedReady = true;
        list.querySelectorAll('[data-status="open"]').forEach((row) => {
          if (feed.querySelector(`[data-review="${row.dataset.review}"]`)) return;
          const link = row.querySelector("a");
          const post = el("article", { class: "vd-feed-post", "data-review": row.dataset.review });
          const frame = el("iframe", { class: "vd-feed-frame", src: link.getAttribute("href"), title: link.textContent });
          const title = el("a", { href: link.getAttribute("href"), target: "_blank", rel: "noopener" }, link.textContent);
          post.append(el("h2", {}, title), frame);
          frame.addEventListener("load", () => {
            const body = frame.contentDocument?.body;
            if (!body) return;
            frame.contentDocument.addEventListener("focusin", (e) => { feedFocus = e.target; });
            const resize = () => { frame.style.height = `${Math.ceil(body.getBoundingClientRect().height)}px`; };
            const observer = new ResizeObserver(resize);
            observer.observe(body);
            frame.addEventListener("load", () => observer.disconnect(), { once: true });
            resize();
          });
          frame.addEventListener("verdict:completed", (event) => {
            post.remove();
            row.dataset.status = event.detail.status;
            row.classList.add("vd-finished");
            row.querySelector("small").textContent = event.detail.status;
            const button = row.querySelector("button");
            button.dataset.action = event.detail.status === "closed" ? "reopen" : "close";
            button.textContent = event.detail.status === "closed" ? "Reopen" : "Close";
            button.setAttribute("aria-label", `${button.textContent} review`);
            emptyFeed();
            (feed.querySelector(".vd-feed-post h2 a") || index.querySelector('[data-view="feed"]')).focus();
          });
          feed.append(post);
        });
      }
      list.hidden = showFeed; feed.hidden = !showFeed;
      index.classList.toggle("vd-feed-view", showFeed);
      index.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      emptyFeed();
      if (showFeed && feedFocus?.isConnected) feedFocus.focus();
    }));
    document.querySelectorAll(".vd-index [data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("[data-review]");
        const action = button.dataset.action;
        const verb = action === "close" ? "Close" : "Reopen";
        confirmDialog(`${verb} ${row.querySelector("a").textContent}?`,
          [el("div", {}, action === "close"
            ? "It stays in this list and can be reopened. Answers and notes are kept."
            : "The review becomes open again. Existing answers and notes are kept.")],
          `${verb} review`, async () => {
            button.disabled = true;
            row.querySelector(".vd-index-error")?.remove();
            try {
              const post = feed.querySelector(`[data-review="${row.dataset.review}"]`);
              const frame = post?.querySelector("iframe");
              if (frame && action === "close") await frame.verdictClose();
              else {
                const r = await fetch(`/api/reviews/${row.dataset.review}/${action}`, { method: "POST" });
                if (!r.ok) throw new Error(`request failed: ${r.status}`);
              }
              if (!feedReady) return location.reload();
              row.dataset.status = action === "close" ? "closed" : "open";
              row.classList.toggle("vd-finished", action === "close");
              row.querySelector("small").textContent = row.dataset.status;
              button.dataset.action = action === "close" ? "reopen" : "close";
              button.textContent = action === "close" ? "Reopen" : "Close";
              button.setAttribute("aria-label", `${button.textContent} review`);
              button.disabled = false;
              post?.remove(); emptyFeed();
            } catch {
              button.disabled = false;
              row.append(el("div", { class: "vd-index-error", role: "alert" }, `Could not ${action} this review. Try again.`));
            }
          });
      });
    });
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
  const submitKey = (e) => e.key === "Enter" && e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.isComposing;

  const items = () => [...document.querySelectorAll("[data-item]")];
  const answerable = () => items().filter((i) => i.dataset.choices || "input" in i.dataset);
  const requiredItems = () => answerable().filter((item) => !("optional" in item.dataset));
  const hasChoices = (scope) => !!scope.querySelector("[data-item][data-choices]");
  let loadedRevision = null;
  // `verdict update` replaced the page: show the new one (answers are kept server-side)
  const replaced = (s) => loadedRevision !== null && s.revision !== loadedRevision;
  const defaultFor = (item) => {
    const choices = (item.dataset.choices || "").split("|").filter(Boolean);
    return item.dataset.default || choices[0] || null;
  };
  const answer = (key) => state.answers[key] || {};

  const isAnswered = (item) => {
    const key = item.dataset.item;
    const current = answer(key);
    return !!(current.choice || current.text?.trim() ||
      state.comments.some((c) => c.item === key && c.text?.trim()));
  };

  // --- persistence -------------------------------------------------------

  const put = async (patch) => {
    const r = await fetch(`${api}/state`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!r.ok) throw new Error(`save failed: ${r.status}`);
    const saved = await r.json();
    state = { ...saved, ...pending, answers: { ...saved.answers, ...pending.answers } };
    if (replaced(state)) return location.reload();
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
  let saving = null;
  const flush = async () => {
    clearTimeout(saveTimer);
    // Done must wait for the autosave already on the wire, including its failure.
    while (saving) if (!await saving) return false;
    const patch = pending;
    pending = {};
    if (!Object.keys(patch).length) return true;
    saving = (async () => {
      try {
        await put(patch);
        status("saved");
        return true;
      } catch (e) {
        pending = { ...patch, ...pending, answers: { ...patch.answers, ...pending.answers } };
        if (paneBody) collapsePane(false);
        status("not saved - retrying; answers have not been sent");
        saveTimer = setTimeout(flush, 2000);
        return false;
      } finally {
        saving = null;
      }
    })();
    if (!await saving) return false;
    return flush();
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
    if (finishing) return;
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
    box.addEventListener("keydown", (e) => {
      if (submitKey(e)) { e.preventDefault(); save(); }
    });
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
    if (finishing || !commentMode) return;
    if (e.target.closest(".vd-bar, .vd-sheet, .vd-slot, .vd-pin")) return;
    e.preventDefault();
    e.stopPropagation();
    compose(e.target);
  };

  // --- toolbar -------------------------------------------------------------

  let bar, statusEl, general, paneBody, paneToggle;
  let paneCollapsed = false;
  const collapsePane = (collapsed) => {
    paneCollapsed = collapsed;
    general.hidden = collapsed;
    bar.classList.toggle("vd-collapsed", collapsed);
    document.body.classList.toggle("vd-pane-collapsed", collapsed);
    paneToggle.setAttribute("aria-expanded", String(!collapsed));
    paneToggle.setAttribute("aria-label", `${collapsed ? "Expand" : "Collapse"} review controls`);
  };
  const status = (text) => { if (statusEl) statusEl.textContent = text; };
  // An agent waiting on this review heartbeats as a waiter (`verdict wait`).
  const listening = () => (state.waiters || []).some((w) => Date.now() - Date.parse(w.seen) < 120000);
  const confirmDone = () => {
    const left = requiredItems().filter((i) => !isAnswered(i)).length;
    const details = [];
    if (left) details.push(el("div", {}, `${left} left unanswered; the agent will ask about ${left === 1 ? "it" : "them"}.`));
    details.push(el("div", {}, listening()
      ? "The agent is listening and gets them right away."
      : "No agent is listening right now; it reads them the next time it checks."));
    confirmDialog("Send your answers to the agent?", details, "Send", finish, "Keep editing");
  };
  const finish = async () => {
    if (finishing) return;
    finishing = true;
    let sent = false;
    render();
    try {
      if (!await flush()) return;
      await put({ status: "done" });
      sent = state.status === "done";
    } catch {
      collapsePane(false);
      status("not sent - try Done again");
    } finally {
      const feedback = statusEl?.textContent;
      finishing = false;
      render();
      if (state.status === "open" && feedback) status(feedback);
    }
    if (sent) collapsePane(false);
    if (sent && feedFrame) feedFrame.dispatchEvent(new CustomEvent("verdict:completed", { detail: { status: state.status } }));
    // Only a locally acknowledged submission may close its own top-level tab.
    if (sent && window.top === window.self) {
      try { window.close(); } catch {}
    }
  };
  const toggleCommentMode = () => {
    if (finishing) return;
    commentMode = !commentMode;
    document.body.classList.toggle("vd-comment-mode", commentMode);
    renderBar();
  };

  const renderBar = () => {
    if (!bar) {
      bar = el("aside", { class: "vd-bar", "aria-label": "Review controls" });
      general = el("textarea", {
        class: "vd-text vd-general",
        placeholder: "general notes for the agent",
        "aria-label": "General notes for the agent",
        oninput: (e) => { state.general = e.target.value; queueSave({ general: state.general }); },
      });
      paneBody = el("div", { class: "vd-pane-body", id: "vd-pane-body" }, general);
      paneToggle = el("button", {
        type: "button", id: "vd-pane-toggle", "data-action": "toggle-review-controls", class: "vd-pane-toggle", "aria-expanded": "true", "aria-controls": "vd-pane-body",
        "aria-label": "Collapse review controls", onclick: () => collapsePane(!paneCollapsed),
      }, el("span", { class: "vd-pane-arrow", "aria-hidden": "true" }));
      statusEl = el("span", { class: "vd-status" });
      bar.append(el("div", { class: "vd-pane-head" },
        el("div", { class: "vd-pane-heading" }, el("strong", { class: "vd-title" }, state.title), statusEl), paneToggle), paneBody);
      document.body.classList.add("vd-review");
      document.body.append(bar);
      new ResizeObserver(() => document.body.style.setProperty("--vd-pane-height", `${bar.getBoundingClientRect().height}px`)).observe(bar);
    }
    const done = state.status === "done";
    const closed = state.status === "closed";
    if (!typing(general)) general.value = state.general || "";
    const questions = answerable();
    const required = requiredItems();
    const total = required.length;
    const answered = required.filter(isAnswered).length;
    const progress = total ? `${answered}/${total} answered` : questions.length ? "no required answers" : "nothing to answer";
    statusEl.textContent = closed ? "closed" : done ? "sent to the agent" : `${progress} · ${state.comments.length} notes`;
    rebuild(
      paneBody,
      general,
      [],
      [el("div", { class: "vd-row" },
        el("button", { type: "button", class: `vd-toggle${commentMode ? " vd-on" : ""}`, onclick: toggleCommentMode, title: "Ctrl+Option+C" }, commentMode ? "tap anything to add a note…" : "comment mode"),
        hasChoices(document) ? el("button", { type: "button", onclick: () => acceptAll(document) }, "accept all recommendations") : "",
        closed ? "" : done
          ? el("button", { type: "button", onclick: () => put({ status: "open" }) }, "reopen")
          : questions.length
            ? el("button", { type: "button", class: "vd-primary", onclick: confirmDone }, "Done")
            : el("button", { type: "button", class: "vd-primary", onclick: finish }, "Got it"),
        done ? el("button", { type: "button", class: "vd-close-tab", onclick: () => window.close() }, "Close tab") : ""
      ), ...(done ? [el("div", { class: "vd-sent-help", role: "status" }, "Sent. You can close this tab if your browser keeps it open.")] : [])]
    );
  };

  const render = () => {
    items().forEach(slotFor);
    document.querySelectorAll("[data-section]").forEach((section) => {
      let btn = section.querySelector(":scope > .vd-section-accept");
      if (!btn && hasChoices(section)) {
        btn = el("button", { type: "button", class: "vd-section-accept", onclick: () => acceptAll(section) }, "accept this section's recommendations");
        section.append(btn);
      }
    });
    renderPins();
    renderBar();
    document.body.classList.toggle("vd-done", state.status !== "open");
    if (feedFrame && state.status !== "open" && !finishing && !saving && !Object.keys(pending).length) {
      feedFrame.dispatchEvent(new CustomEvent("verdict:completed", { detail: { status: state.status } }));
    }
    document.querySelectorAll(".vd-slot button, .vd-slot textarea, .vd-bar button, .vd-bar textarea, .vd-section-accept, .vd-pin").forEach((control) => { control.disabled = finishing; });
  };

  const boot = async () => {
    const r = await fetch(api);
    if (!r.ok) {
      document.body.prepend(el("div", { class: "vd-bar" }, "This review no longer exists."));
      return;
    }
    state = await r.json();
    loadedRevision = state.revision;
    if (feedFrame) feedFrame.verdictClose = async () => {
      if (finishing) throw new Error("submission in progress");
      finishing = true; render();
      try {
        if (!await flush()) throw new Error("unsaved edits");
        const r = await fetch(`${api}/close`, { method: "POST" });
        if (!r.ok) throw new Error(`close failed: ${r.status}`);
        state = await r.json();
      } finally { finishing = false; render(); }
    };
    document.addEventListener("click", onPageClick, true);
    // Ctrl+Option+C toggles comment mode; e.code, since Option changes e.key
    document.addEventListener("keydown", (e) => {
      if (submitKey(e) && e.target.matches(".vd-slot textarea, .vd-general") && state.status === "open") {
        e.preventDefault();
        confirmDone();
      }
      if (e.ctrlKey && e.altKey && !e.metaKey && e.code === "KeyC") {
        e.preventDefault();
        toggleCommentMode();
      }
    });
    render();
    // keep in sync when the agent closes it or another device edits. Only a
    // visible tab long-polls: browsers open six connections per host, so polls
    // parked in background tabs would stall every save and page load.
    let polling = null;
    const poll = async () => {
      if (document.hidden || polling) return;
      polling = new AbortController();
      try {
        const s = await (await fetch(`${api}?wait=${feedFrame ? 0 : 30}&version=${state.version}`, { signal: polling.signal })).json();
        if (replaced(s)) { await flush(); return location.reload(); }
        if (s.version !== state.version && !saving && !Object.keys(pending).length) { state = s; render(); }
      } catch {}
      polling = null;
      setTimeout(poll, feedFrame ? 5000 : 1000);
    };
    document.addEventListener("visibilitychange", () => (document.hidden ? polling?.abort() : poll()));
    poll();
  };

  const start = () => location.pathname === "/" ? bootIndex() : boot();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
