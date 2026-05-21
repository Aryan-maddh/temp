(function () {
  const API_BASE = "http://127.0.0.1:8001";
  const PAGE_HASH = "#/candidates?outlook=1";
  let candidates = [];
  let selectedId = "";
  let messages = [];
  let selectedMessage = null;
  let statusText = "";
  let sending = false;
  let loadingCandidates = false;
  let loadedCandidates = false;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function isOutlookPage() {
    return window.location.hash === PAGE_HASH || window.location.hash.includes("outlook=1");
  }

  async function api(path, options) {
    const response = await fetch(API_BASE + path, {
      headers: {
        "Content-Type": "application/json",
        ...((options && options.headers) || {}),
      },
      ...(options || {}),
    });
    if (!response.ok) {
      const text = await response.text();
      let detail = text || `Request failed: ${response.status}`;
      try {
        const parsed = JSON.parse(text);
        detail = typeof parsed.detail === "string" ? parsed.detail : detail;
      } catch {}
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  function navClass(active) {
    return `block w-full rounded-2xl px-4 py-3 text-left text-sm font-extrabold transition ${
      active
        ? "bg-zinc-950 text-white dark:bg-orange-500 dark:text-zinc-950"
        : "hover:bg-black/5 dark:hover:bg-white/10"
    }`;
  }

  function installMenu() {
    const nav = document.querySelector("aside nav");
    if (!nav || document.getElementById("outlook-email-menu-link")) return;
    const link = document.createElement("a");
    link.id = "outlook-email-menu-link";
    link.href = PAGE_HASH;
    link.textContent = "Outlook Email";
    link.className = navClass(isOutlookPage());
    link.addEventListener("click", function () {
      setTimeout(render, 0);
    });
    const manualLink = Array.from(nav.children).find((node) =>
      String(node.textContent || "").includes("Answer Manually")
    );
    nav.insertBefore(link, manualLink || null);
  }

  function updateMenuState() {
    const link = document.getElementById("outlook-email-menu-link");
    if (link) link.className = navClass(isOutlookPage());
  }

  function restoreMain(main) {
    const overlay = document.getElementById("outlook-email-page");
    if (overlay) overlay.remove();
    Array.from(main.children).forEach((child) => {
      child.style.display = child.dataset.outlookHiddenDisplay || "";
      delete child.dataset.outlookHiddenDisplay;
    });
  }

  async function loadCandidates(quiet) {
    if (loadingCandidates) return;
    loadingCandidates = true;
    try {
      candidates = await api("/api/outlook/candidates");
      loadedCandidates = true;
      if (!selectedId && candidates.length) selectedId = candidates[0].id;
      statusText = quiet ? statusText : "Candidates refreshed.";
    } catch (error) {
      loadedCandidates = true;
      statusText = error.message;
    } finally {
      loadingCandidates = false;
    }
    render();
  }

  async function startAuth(candidateId) {
    const input = document.querySelector(`[data-outlook-input="${candidateId}"]`);
    const outlookEmail = input ? input.value.trim() : "";
    if (!outlookEmail) {
      statusText = "Enter the candidate Outlook email first.";
      render();
      return;
    }
    try {
      const result = await api(`/api/outlook/candidates/${candidateId}/auth/start`, {
        method: "POST",
        body: JSON.stringify({ outlook_email: outlookEmail }),
      });
      statusText = "Microsoft authorization link created.";
      window.open(result.auth_url, "_blank", "noopener,noreferrer");
      await navigator.clipboard?.writeText(result.auth_url).catch(function () {});
      await loadCandidates(true);
    } catch (error) {
      statusText = error.message;
      render();
    }
  }

  async function loadMessages(unreadOnly) {
    if (!selectedId) return;
    selectedMessage = null;
    try {
      const unread = unreadOnly ? "&unread_only=true" : "";
      messages = await api(`/api/outlook/candidates/${selectedId}/messages?top=20${unread}`);
      statusText = messages.length ? `Loaded ${messages.length} message(s).` : "No messages found.";
    } catch (error) {
      statusText = error.message;
    }
    render();
  }

  async function openMessage(messageId) {
    try {
      selectedMessage = await api(`/api/outlook/candidates/${selectedId}/messages/${encodeURIComponent(messageId)}`);
      statusText = "Message loaded.";
    } catch (error) {
      statusText = error.message;
    }
    render();
  }

  async function sendMail(event) {
    event.preventDefault();
    if (!selectedId || sending) return;
    const form = event.currentTarget;
    const to = form.querySelector("[name='to']").value.split(",").map((item) => item.trim()).filter(Boolean);
    const cc = form.querySelector("[name='cc']").value.split(",").map((item) => item.trim()).filter(Boolean);
    const subject = form.querySelector("[name='subject']").value.trim();
    const body = form.querySelector("[name='body']").value;
    if (!to.length || !subject || !body.trim()) {
      statusText = "To, subject, and body are required.";
      render();
      return;
    }
    sending = true;
    render();
    try {
      const result = await api(`/api/outlook/candidates/${selectedId}/send`, {
        method: "POST",
        body: JSON.stringify({ to, cc, subject, body, content_type: "Text", save_to_sent_items: true }),
      });
      statusText = `Email sent from ${result.sent_from}.`;
      messages = [];
      selectedMessage = null;
    } catch (error) {
      statusText = error.message;
    } finally {
      sending = false;
      render();
    }
  }

  function candidateCards() {
    if (!candidates.length) {
      return `<div class="rounded-2xl bg-black/5 p-4 text-sm font-bold dark:bg-white/5">No candidates found.</div>`;
    }
    return candidates
      .map((candidate) => {
        const active = candidate.id === selectedId;
        const connected = candidate.connected;
        return `
          <div class="rounded-2xl border p-4 ${active ? "border-orange-500 bg-orange-500/10" : "border-black/10 bg-white/45 dark:border-white/10 dark:bg-white/5"}">
            <button class="block w-full text-left" type="button" data-select-candidate="${escapeHtml(candidate.id)}">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-extrabold">${escapeHtml(candidate.name)}</span>
                <span class="badge ${connected ? "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300" : "bg-amber-500/15 text-amber-800 dark:text-amber-300"}">${connected ? "connected" : "not connected"}</span>
              </div>
              <div class="mt-1 text-xs opacity-60">${escapeHtml(candidate.email)}</div>
            </button>
            <div class="mt-3 grid gap-2 sm:grid-cols-[1fr_auto]">
              <input class="field" data-outlook-input="${escapeHtml(candidate.id)}" value="${escapeHtml(candidate.outlook_email || candidate.email)}" placeholder="candidate@outlook.com" />
              <button class="btn-primary" type="button" data-auth-candidate="${escapeHtml(candidate.id)}">Connect</button>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function messagesTable() {
    if (!messages.length) {
      return `<div class="rounded-2xl bg-black/5 p-4 text-sm font-bold dark:bg-white/5">No inbox messages loaded.</div>`;
    }
    return `
      <div class="overflow-x-auto">
        <table class="w-full min-w-[760px] text-left text-sm">
          <thead class="text-xs uppercase tracking-wide opacity-60">
            <tr><th class="pb-3">From</th><th class="pb-3">Subject</th><th class="pb-3">Received</th><th class="pb-3">Status</th></tr>
          </thead>
          <tbody class="divide-y divide-black/10 dark:divide-white/10">
            ${messages.map((message) => `
              <tr>
                <td class="py-3">${escapeHtml(message.from_name || message.from_address || "-")}</td>
                <td class="py-3">
                  <button class="font-bold text-orange-700 dark:text-orange-300" type="button" data-open-message="${escapeHtml(message.id)}">
                    ${escapeHtml(message.subject || "(no subject)")}
                  </button>
                  <div class="mt-1 max-w-xl truncate text-xs opacity-60">${escapeHtml(message.bodyPreview || "")}</div>
                </td>
                <td class="py-3">${escapeHtml(message.receivedDateTime || "-")}</td>
                <td class="py-3"><span class="badge bg-black/10 dark:bg-white/10">${message.isRead ? "read" : "unread"}</span></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  function messageDetail() {
    if (!selectedMessage) return "";
    const body = selectedMessage.body_content_type === "html"
      ? selectedMessage.body
      : `<pre class="whitespace-pre-wrap text-sm leading-6">${escapeHtml(selectedMessage.body || "")}</pre>`;
    return `
      <section class="panel">
        <div class="mb-3">
          <p class="text-sm font-extrabold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">Message</p>
          <h3 class="font-display text-2xl">${escapeHtml(selectedMessage.subject || "(no subject)")}</h3>
          <div class="mt-1 text-xs opacity-60">${escapeHtml(selectedMessage.from_address || "")}</div>
        </div>
        <div class="max-h-[30rem] overflow-auto rounded-2xl border border-black/10 bg-white/50 p-4 dark:border-white/10 dark:bg-white/5">${body || ""}</div>
      </section>
    `;
  }

  function pageHtml() {
    const selected = candidates.find((candidate) => candidate.id === selectedId);
    return `
      <div class="space-y-5">
        <section class="panel">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p class="text-sm font-extrabold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">Outlook Email</p>
              <h2 class="font-display text-4xl">Candidate mailbox access</h2>
            </div>
            <button class="btn-secondary" type="button" data-refresh-outlook>Refresh</button>
          </div>
          ${statusText ? `<div class="mt-4 rounded-2xl bg-white/70 px-4 py-3 text-sm font-bold dark:bg-white/10">${escapeHtml(statusText)}</div>` : ""}
        </section>

        <section class="grid gap-5 xl:grid-cols-[24rem_1fr]">
          <div class="panel">
            <div class="mb-4">
              <h3 class="font-display text-2xl">Candidates</h3>
              <p class="text-sm opacity-70">Select a candidate, connect Outlook, then open the inbox.</p>
            </div>
            <div class="space-y-3">${candidateCards()}</div>
          </div>

          <div class="space-y-5">
            <section class="panel">
              <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
                <div>
                  <h3 class="font-display text-2xl">${escapeHtml(selected ? selected.name : "No candidate selected")}</h3>
                  <div class="text-sm opacity-70">${escapeHtml(selected ? selected.outlook_email || selected.email : "")}</div>
                </div>
                <div class="flex flex-wrap gap-2">
                  <button class="btn-secondary" type="button" data-load-messages="unread">Unread</button>
                  <button class="btn-primary" type="button" data-load-messages="all">Load inbox</button>
                </div>
              </div>
              ${messagesTable()}
            </section>

            ${messageDetail()}

            <section class="panel">
              <div class="mb-4">
                <p class="text-sm font-extrabold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">Send Email</p>
                <h3 class="font-display text-2xl">New message</h3>
              </div>
              <form class="space-y-3" data-send-form>
                <input class="field" name="to" placeholder="To: name@example.com, second@example.com" />
                <input class="field" name="cc" placeholder="Cc" />
                <input class="field" name="subject" placeholder="Subject" />
                <textarea class="field min-h-40 resize-y" name="body" placeholder="Write email body..."></textarea>
                <button class="btn-primary w-full" type="submit" ${sending ? "disabled" : ""}>${sending ? "Sending..." : "Send email"}</button>
              </form>
            </section>
          </div>
        </section>
      </div>
    `;
  }

  function bindPage(container) {
    container.querySelector("[data-refresh-outlook]")?.addEventListener("click", function () {
      loadCandidates(false);
    });
    container.querySelectorAll("[data-select-candidate]").forEach((button) => {
      button.addEventListener("click", function () {
        selectedId = button.getAttribute("data-select-candidate") || "";
        messages = [];
        selectedMessage = null;
        render();
      });
    });
    container.querySelectorAll("[data-auth-candidate]").forEach((button) => {
      button.addEventListener("click", function () {
        startAuth(button.getAttribute("data-auth-candidate") || "");
      });
    });
    container.querySelectorAll("[data-load-messages]").forEach((button) => {
      button.addEventListener("click", function () {
        loadMessages(button.getAttribute("data-load-messages") === "unread");
      });
    });
    container.querySelectorAll("[data-open-message]").forEach((button) => {
      button.addEventListener("click", function () {
        openMessage(button.getAttribute("data-open-message") || "");
      });
    });
    container.querySelector("[data-send-form]")?.addEventListener("submit", sendMail);
  }

  function render() {
    installMenu();
    updateMenuState();
    const main = document.querySelector("main.min-w-0");
    if (!main) return;
    if (!isOutlookPage()) {
      restoreMain(main);
      return;
    }
    Array.from(main.children).forEach((child) => {
      if (child.id === "outlook-email-page") return;
      if (child.dataset.outlookHiddenDisplay == null) {
        child.dataset.outlookHiddenDisplay = child.style.display || "";
      }
      child.style.display = "none";
    });
    let container = document.getElementById("outlook-email-page");
    if (!container) {
      container = document.createElement("div");
      container.id = "outlook-email-page";
      main.appendChild(container);
    }
    container.innerHTML = pageHtml();
    bindPage(container);
    if (!loadedCandidates && !loadingCandidates) loadCandidates(true);
  }

  window.addEventListener("hashchange", render);
  window.addEventListener("load", render);
  new MutationObserver(function () {
    installMenu();
    updateMenuState();
  }).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(function () {
    installMenu();
    updateMenuState();
  }, 1000);
})();
