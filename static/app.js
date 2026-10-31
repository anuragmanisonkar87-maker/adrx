(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const EXAMPLES = {
    en: `Dr. Rajesh Mehta - Mumbai's No.1 Hair Transplant Surgeon!
100% guaranteed results with our painless FUE technique. 10,000+ happy patients.
⭐⭐⭐⭐⭐ Rated 4.9 on Google - see the before & after photos of our patients.
DIWALI OFFER: Flat 40% OFF + FREE consultation. Limited slots - book your appointment now!
Call 98XXX XXXXX, Andheri West, Mumbai`,
    hi: `डॉ. सुनील वर्मा - शहर के सबसे अच्छे बवासीर विशेषज्ञ!
बवासीर का जड़ से इलाज, बिना ऑपरेशन, 100% गारंटी।
कोई साइड इफेक्ट नहीं। 5000+ संतुष्ट मरीज।
दिवाली ऑफर: मुफ्त जांच। अभी कॉल करें: 98XXXXXXXX`,
    ok: `Shah Family Clinic
Dr. Anita Shah, MBBS, MD (General Medicine)
Reg. No. MMC 2009/04/1234, Maharashtra Medical Council

We have moved. From 1 November 2026 the clinic is at:
2nd floor, Sai Plaza, SV Road, Andheri West, Mumbai 400058.

OPD timings: Mon-Sat, 10 am-1 pm and 5-8 pm
Consultation fee: ₹600
Appointments: 022-XXXX XXXX`,
  };

  // Everything that is not a document or picture is treated as audio/video (the server asks ffmpeg).
  const DOC_EXT = /\.(pdf|docx|txt|md|csv|html?|srt|vtt|ass|ssa|sbv|png|jpe?g|jfif|pjpeg|webp|bmp|gif|tiff?|apng|avif|hei[cf])$/i;
  const isMedia = (file) => /^(audio|video)\//.test(file.type) || !DOC_EXT.test(file.name);
  const STAMP_TEXT = { compliant: "Compliant", needs_changes: "Needs changes", non_compliant: "Not compliant" };
  const SEVERITY_LABEL = { high: "Prohibited", medium: "Fix before posting", low: "Advisory" };
  const SOURCE_LABEL = { rules: "Phrase rule", ai: "AI review", "rules+ai": "Phrase rule, confirmed by AI" };
  const KIND_LABEL = { text: "pasted text", pdf: "a PDF", image: "an image", docx: "a Word file", audio: "an audio recording", video: "a video" };
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const form = $("#check-form");
  const textInput = $("#text-input");
  const captionInput = $("#caption-input");
  const fileInput = $("#file-input");
  const dropzone = $("#dropzone");
  const languageField = $("#language-field");
  const submitBtn = $("#submit-btn");
  const formError = $("#form-error");
  const progress = $("#progress");
  const progressText = $("#progress-text");
  const result = $("#result");
  const state = { mode: "text", file: null, health: null, timer: null };

  /* ---------- input ---------- */

  function setMode(mode) {
    state.mode = mode;
    for (const [tab, panel, m] of [["#tab-text", "#panel-text", "text"], ["#tab-file", "#panel-file", "file"]]) {
      const on = m === mode;
      $(tab).classList.toggle("is-on", on);
      $(tab).setAttribute("aria-selected", String(on));
      $(panel).hidden = !on;
    }
    hideError();
  }
  $("#tab-text").addEventListener("click", () => setMode("text"));
  $("#tab-file").addEventListener("click", () => setMode("file"));

  function formatSize(bytes) {
    return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }

  function pickFile(file) {
    if (!file) return;
    state.file = file;
    dropzone.classList.add("has-file");
    $("#dropzone-main").textContent = file.name;
    $("#dropzone-hint").textContent = `${formatSize(file.size)}. Click to choose a different file.`;
    languageField.hidden = !isMedia(file);
    hideError();
  }
  fileInput.addEventListener("change", () => pickFile(fileInput.files[0]));
  ["dragenter", "dragover"].forEach((type) =>
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((type) =>
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.remove("is-over"); }));
  dropzone.addEventListener("drop", (e) => pickFile(e.dataTransfer.files[0]));

  $$("[data-example]").forEach((btn) =>
    btn.addEventListener("click", () => {
      textInput.value = EXAMPLES[btn.dataset.example];
      setMode("text");
      textInput.focus();
    }));

  function showError(message) {
    formError.textContent = message;
    formError.hidden = false;
  }
  function hideError() {
    formError.hidden = true;
  }

  fetch("/api/health?verify_ai=true")
    .then((r) => r.json())
    .then((h) => {
      state.health = h;
      $("#max-mb").textContent = h.max_upload_mb;
      if (h.public && h.max_media_minutes) {
        const minutes = h.max_media_minutes;
        $("#media-limit").textContent = `; audio and video up to ${minutes} minute${minutes === 1 ? "" : "s"}`;
      }
      if (h.speech_service) {
        $("#privacy-note").textContent += ` Speech in audio and video is turned into text by ${h.speech_service}.`;
      }
      if (h.ai_status === "ok") {
        $("#privacy-note").textContent += " For the AI review the content is also sent to Anthropic's API.";
      }
      const note = $("#mode-note");
      // visitors of the public website never see server set-up details
      note.classList.toggle("mode-note--warn", h.ai_status === "error" && !h.public);
      if (h.ai_status === "ok") {
        note.textContent = `AI review is on (${h.ai_model_name || h.ai_model}): phrase rules plus a full read by Claude, with a ready-to-post rewrite.`;
      } else if (h.public) {
        note.textContent = "Quick check: every phrase is compared with the NMC 2026 advertising rules.";
      } else if (h.ai_status === "error") {
        note.textContent = `AI review is set up but not working: ${h.ai_message} Until it's fixed, checks use the phrase rules only.`;
      } else {
        note.textContent = "Quick check mode: phrase rules only. Add an Anthropic API key on the server for the full AI review and rewrite.";
      }
    })
    .catch(() => {});

  /* ---------- progress ---------- */

  function startProgress(kind) {
    const steps = {
      media: ["Uploading...", "Pulling out the audio...", "Transcribing the speech...", "Reading text shown on screen...", "Checking against the NMC guidelines..."],
      doc: ["Uploading...", "Reading the text...", "Checking against the NMC guidelines..."],
      text: ["Checking against the NMC guidelines..."],
    }[kind];
    let i = 0;
    progressText.textContent = steps[0];
    progress.hidden = false;
    clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (i < steps.length - 1) progressText.textContent = steps[++i];
    }, 4500);
  }
  function stopProgress() {
    clearInterval(state.timer);
    progress.hidden = true;
  }

  /* ---------- submit ---------- */

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError();
    const data = new FormData();
    let kind = "text";
    if (state.mode === "file") {
      if (!state.file) return showError('Choose a file to check, or switch to "Paste text".');
      data.append("file", state.file);
      const caption = captionInput.value.trim();
      if (caption) data.append("text", caption);
      data.append("language", $("#language").value);
      kind = isMedia(state.file) ? "media" : "doc";
    } else {
      const text = textInput.value.trim();
      if (!text) return showError("Paste the text you want to check.");
      data.append("text", text);
    }
    data.append("publisher", form.querySelector('input[name="publisher"]:checked').value);
    data.append("channel", $("#channel").value);
    data.append("ai_used", form.querySelector('input[name="ai_used"]:checked').value);

    submitBtn.disabled = true;
    result.hidden = true;
    startProgress(kind);
    try {
      const res = await fetch("/api/check", { method: "POST", body: data });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
        throw new Error(detail || `The check failed (HTTP ${res.status}).`);
      }
      renderResult(body);
    } catch (err) {
      showError(err.message === "Failed to fetch" ? "Could not reach the server. Check your connection and try again." : err.message);
    } finally {
      stopProgress();
      submitBtn.disabled = false;
    }
  });

  /* ---------- result ---------- */

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("data-") || key.startsWith("aria-")) node.setAttribute(key, value);
      else node[key] = value;
    }
    node.append(...children.filter((c) => c != null));
    return node;
  }

  const numOf = (id) => id.replace(/^\D+/, "");

  function appendPlain(target, chunk) {
    chunk.split(/(\n)/).forEach((part) => {
      if (/^\[[^\]\n]{3,80}\]$/.test(part)) target.append(el("span", { class: "doc-section", text: part.slice(1, -1) }));
      else if (part) target.append(document.createTextNode(part));
    });
  }

  function renderMarkedText(container, text, highlights) {
    if (!text) {
      container.append(el("span", { class: "doc-section", text: "No text was found in this file. The review is based on the images." }));
      return;
    }
    const rank = { low: 1, medium: 2, high: 3 };
    let pos = 0;
    let runIds = [];
    let runSev = "low";
    highlights.forEach((h, i) => {
      if (h.start > pos) appendPlain(container, text.slice(pos, h.start));
      container.append(el("mark", { class: `hl hl--${h.severity}`, "data-ids": h.finding_ids.join(" "),
        "data-start": h.start, "data-end": h.end, text: text.slice(h.start, h.end) }));
      h.finding_ids.forEach((id) => { if (!runIds.includes(id)) runIds.push(id); });
      if (rank[h.severity] > rank[runSev]) runSev = h.severity;
      const next = highlights[i + 1];
      if (!next || next.start !== h.end) {
        const sup = el("sup", { class: `ref ref--${runSev}`, "data-ids": runIds.join(" ") });
        runIds.forEach((id, k) => {
          if (k) sup.append(",");
          sup.append(el("a", { href: `#note-${id}`, text: numOf(id), "aria-label": `Note ${numOf(id)}` }));
        });
        container.append(sup);
        runIds = [];
        runSev = "low";
      }
      pos = h.end;
    });
    if (pos < text.length) appendPlain(container, text.slice(pos));
  }

  function renderNote(f) {
    const n = numOf(f.id);
    const li = el("li", { class: `note note--${f.severity}`, id: `note-${f.id}`, "data-id": f.id });
    li.append(el("span", { class: "note__num", "aria-hidden": "true", text: n }));
    li.append(el("div", { class: "note__head" },
      el("h4", { class: "note__title", text: f.title }),
      el("span", { class: "note__sev", text: SEVERITY_LABEL[f.severity] })));
    if (f.location === "visual") {
      li.append(el("span", { class: "note__where", text: "Seen in the image or video frames" }));
    } else if (f.location === "context") {
      li.append(el("span", { class: "note__where", text: "Based on your answers above" }));
    } else if (f.quotes && f.quotes.length) {
      const quotes = el("div", { class: "note__quotes" });
      f.quotes.forEach((q) => {
        if (!q.spans.length) {
          quotes.append(el("span", { class: "note__quote", text: q.text }),
            el("span", { class: "note__where", text: "not found word-for-word in the text" }));
          return;
        }
        const label = q.spans.length > 1 ? `${q.text} (${q.spans.length}×)` : q.text;
        quotes.append(el("button", { type: "button", class: "note__quote", text: label, "data-id": f.id,
          "data-start": q.spans[0][0], title: "Show in the text" }));
      });
      li.append(quotes);
    }
    if (f.why) li.append(el("p", { class: "note__why", text: f.why }));
    if (f.fix) li.append(el("p", { class: "note__fix" }, el("strong", { text: "Instead: " }), f.fix));
    const foot = el("div", { class: "note__foot" });
    f.clauses.forEach((c) => foot.append(el("span", { class: "clause", title: "NMC guideline clause", text: `Clause ${c}` })));
    foot.append(el("span", { class: "source", text: SOURCE_LABEL[f.source] || "" }));
    li.append(foot);
    if (f.ai_note && f.source === "rules") li.append(el("p", { class: "note__ai", text: `AI note: ${f.ai_note}` }));
    return li;
  }

  function countsLine(c) {
    const parts = [];
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    if (c.high) parts.push(el("span", { class: "c-high", text: `${c.high} prohibited` }));
    if (c.medium) parts.push(el("span", { class: "c-medium", text: `${c.medium} to fix before posting` }));
    if (c.disclosures) parts.push(el("span", { class: "c-disc", text: plural(c.disclosures, "missing detail", "missing details") }));
    if (c.low) parts.push(el("span", { text: plural(c.low, "advisory note", "advisory notes") }));
    if (!parts.length) return [document.createTextNode("Nothing to fix.")];
    return parts.flatMap((p, i) => (i ? [document.createTextNode(", "), p] : [p]));
  }

  function metaLine(data) {
    const src = data.source || {};
    const meta = src.meta || {};
    let read = `Read from ${KIND_LABEL[src.kind] || "your content"}`;
    if (meta.duration_s) read += ` (${Math.round(meta.duration_s)} s)`;
    if (meta.pages > 1) read += ` (${meta.pages} pages)`;
    if (src.how && src.how.length && src.kind !== "text") read += ` using ${src.how.join(" and ")}`;
    const a = data.analysis || {};
    let how = a.mode === "rules+ai" ? `Phrase rules and AI review (${a.model})` : "Quick check with phrase rules";
    if (a.ai_error) how = `AI review unavailable (${a.ai_error}); showing the phrase-rule check`;
    const took = data.elapsed_ms < 1000 ? `${data.elapsed_ms} ms` : `${(data.elapsed_ms / 1000).toFixed(1)} s`;
    return `${read}. ${how}. Checked in ${took}.`;
  }

  function renderExtras(box, data) {
    if (data.missing_disclosures.length) {
      const list = el("ul", { class: "checklist" });
      data.missing_disclosures.forEach((m) =>
        list.append(el("li", {}, el("span", { class: "x", "aria-hidden": "true", text: "✕" }),
          el("div", {}, el("strong", { text: m.label }), el("span", { class: "d", text: `${m.detail} (clause ${m.clause})` })))));
      box.append(el("section", { class: "panel-box" },
        el("h3", { text: "Details the post must show" }),
        el("p", { class: "muted", text: "Posts on electronic media must show the doctor's name, qualifications and registration details; AI-made content must say so." }),
        list));
    }
    if (data.compliant_rewrite) {
      const copyBtn = el("button", { type: "button", class: "btn btn--quiet", text: "Copy text" });
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(data.compliant_rewrite);
          copyBtn.textContent = "Copied";
          setTimeout(() => (copyBtn.textContent = "Copy text"), 1800);
        } catch {
          copyBtn.textContent = "Select the text to copy";
        }
      });
      box.append(el("section", {},
        el("div", { class: "box-head" }, el("h3", { text: "A compliant version" }), copyBtn),
        el("pre", { class: "rewrite", lang: "und", text: data.compliant_rewrite }),
        el("p", { class: "muted", text: "Drafted by AI from your content. Fill in the [placeholders] and check every fact before you post." })));
    }
    if (data.penalty) {
      const ladder = el("ul", { class: "ladder" });
      data.penalty.ladder.forEach((step) =>
        ladder.append(el("li", { class: data.penalty.serious && step.startsWith("Serious") ? "is-serious" : null, text: step })));
      box.append(el("section", { class: "panel-box" },
        el("h3", { text: "If this is published as it is" }),
        el("p", { text: data.penalty.serious
          ? "It contains a serious-violation type of claim. The State Medical Council can take graded action (clause 10.1):"
          : "The State Medical Council can take graded action (clause 10.1):" }),
        ladder,
        el("p", { class: "muted", text: data.penalty.note })));
    }
    if (data.dismissed && data.dismissed.length) {
      const list = el("ul");
      data.dismissed.forEach((d) => list.append(el("li", { text: `"${d.quote}": ${d.note || "not a conflict in this context"}` })));
      box.append(el("details", { class: "dismissed panel-box" },
        el("summary", { text: `Phrases the AI checked and cleared (${data.dismissed.length})` }), list));
    }
  }

  function activate(ids, on) {
    ids.forEach((id) => {
      $$(`.note[data-id="${id}"]`, result).forEach((n) => n.classList.toggle("is-active", on));
      $$("mark.hl", result).forEach((m) => { if (m.dataset.ids.split(" ").includes(id)) m.classList.toggle("is-active", on); });
    });
  }

  function flash(node) {
    node.classList.add("is-active");
    setTimeout(() => node.classList.remove("is-active"), 1400);
  }

  function wire(root) {
    $$("mark.hl, sup.ref", root).forEach((m) => {
      const ids = m.dataset.ids.split(" ");
      m.addEventListener("mouseenter", () => activate(ids, true));
      m.addEventListener("mouseleave", () => activate(ids, false));
      if (m.tagName === "MARK") {
        m.addEventListener("click", () => {
          const note = $(`#note-${ids[0]}`, root);
          if (note) { note.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" }); flash(note); }
        });
      }
    });
    $$("a[href^='#note-']", root).forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const note = $(a.getAttribute("href"), root);
        if (note) { note.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" }); flash(note); }
      }));
    $$(".note", root).forEach((n) => {
      n.addEventListener("mouseenter", () => activate([n.dataset.id], true));
      n.addEventListener("mouseleave", () => activate([n.dataset.id], false));
    });
    $$("button.note__quote", root).forEach((b) =>
      b.addEventListener("click", () => {
        const at = Number(b.dataset.start);
        const mark = $$("mark.hl", root).find((m) => Number(m.dataset.start) <= at && at < Number(m.dataset.end))
          || $$("mark.hl", root).find((m) => m.dataset.ids.split(" ").includes(b.dataset.id));
        if (mark) { mark.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" }); flash(mark); }
      }));
    $("[data-action='again']", root).addEventListener("click", () => {
      result.hidden = true;
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
      (state.mode === "file" ? dropzone : textInput).focus();
    });
    $("[data-action='print']", root).addEventListener("click", () => window.print());
  }

  function renderResult(data) {
    const frag = $("#tpl-result").content.cloneNode(true);
    const stamp = $(".stamp", frag);
    stamp.dataset.verdict = data.verdict;
    $(".stamp__text", frag).textContent = STAMP_TEXT[data.verdict];
    $(".verdict__headline", frag).textContent = data.headline;
    $(".verdict__summary", frag).textContent = data.summary;
    $(".verdict__counts", frag).append(...countsLine(data.counts));
    $(".verdict__meta", frag).textContent = metaLine(data);

    if (data.warnings && data.warnings.length) {
      const ul = $(".warnings", frag);
      data.warnings.forEach((w) => ul.append(el("li", { text: w })));
      ul.hidden = false;
    }
    renderMarkedText($(".sheet__text", frag), data.text, data.highlights);

    const notes = $(".notes", frag);
    if (data.findings.length) data.findings.forEach((f) => notes.append(renderNote(f)));
    else notes.replaceWith(el("p", { class: "empty-notes", text: "Nothing to change. No phrase in this content conflicts with the NMC advertising guidelines." }));
    if (!data.highlights.length) $(".legend", frag).hidden = true;
    renderExtras($(".extras", frag), data);

    result.replaceChildren(frag);
    result.hidden = false;
    wire(result);
    const shown = $(".stamp", result);
    shown.classList.remove("is-pressed");
    void shown.offsetWidth;
    shown.classList.add("is-pressed");
    result.focus({ preventScroll: true });
    result.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  }
})();
