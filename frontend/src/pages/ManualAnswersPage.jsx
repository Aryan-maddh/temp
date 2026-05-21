import { useEffect, useRef, useState } from "react";
import { api } from "../api";

function optionList(options) {
  return Array.isArray(options)
    ? options.filter((option) => {
        const text = String(option || "").trim();
        return text && !/^error:/i.test(text) && !/^['"].+['"] is required$/i.test(text);
      })
    : [];
}

function isValidationOnlyOption(fieldType, options) {
  return String(fieldType || "").toLowerCase()
    && Array.isArray(options)
    && options.length === 1
    && (/^error:/i.test(String(options[0] || "").trim()) || /^['"].+['"] is required$/i.test(String(options[0] || "").trim()));
}

function fallbackChoiceOptions(question) {
  const fieldType = String(question.field_type || "").toLowerCase();
  if (!fieldType.includes("select") && fieldType !== "radio") return [];
  const label = String(question.field_label || "").toLowerCase().replace(/[_-]/g, " ");
  if (/^(are|is|do|does|did|have|has|will|would|can|could)\b/.test(label)) return ["Yes", "No"];
  if (/(?:[.:\n]\s*|\b)(are|is|do|does|did|have|has|will|would|can|could)\b/.test(label)) return ["Yes", "No"];
  return [];
}

function splitOptionPath(option) {
  return String(option || "").split(" > ").map((part) => part.trim()).filter(Boolean);
}

function buildOptionTree(options) {
  const grouped = new Map();
  let hasNested = false;
  options.forEach((option) => {
    const parts = splitOptionPath(option);
    const parent = parts[0] || String(option || "").trim();
    const child = parts.slice(1).join(" > ");
    if (!parent) return;
    if (!grouped.has(parent)) grouped.set(parent, []);
    if (child) {
      hasNested = true;
      if (!grouped.get(parent).includes(child)) grouped.get(parent).push(child);
    }
  });
  return { hasNested, parents: Array.from(grouped.keys()), childrenByParent: grouped };
}

function groupByCompany(questions) {
  const groups = new Map();
  questions.forEach((question) => {
    const domain = question.domain || "Unknown domain";
    const applicationId = question.application_id || "unknown-application";
    const key = `${domain}::${applicationId}`;
    if (!groups.has(key)) groups.set(key, { domain, applicationId, questions: [] });
    groups.get(key).questions.push(question);
  });
  return Array.from(groups.values());
}

function SearchableSelect({ value, options, disabled, onChange, placeholder = "Select an option" }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const wrapperRef = useRef(null);

  useEffect(() => {
    function onClickOutside(event) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target)) {
        setOpen(false);
        setQuery("");
      }
    }
    if (open) document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  const q = query.trim().toLowerCase();
  const filtered = q ? options.filter((opt) => String(opt).toLowerCase().includes(q)) : options;

  return (
    <div className="relative" ref={wrapperRef}>
      <button
        type="button"
        disabled={disabled}
        className="field flex w-full items-center justify-between text-left disabled:opacity-50"
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className={value ? "" : "opacity-60"}>{value || placeholder}</span>
        <span className="ml-2 text-xs opacity-60">{open ? "▴" : "▾"}</span>
      </button>
      {open ? (
        <div className="absolute left-0 right-0 z-20 mt-2 rounded-2xl border border-black/10 bg-white p-2 shadow-2xl dark:border-white/10 dark:bg-zinc-900">
          {options.length > 6 ? (
            <input
              autoFocus
              className="field mb-2"
              placeholder={`Search ${options.length} options...`}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          ) : null}
          <div className="max-h-64 overflow-auto">
            {filtered.length === 0 ? (
              <div className="px-3 py-2 text-sm font-bold opacity-60">No matches</div>
            ) : (
              filtered.map((option) => (
                <button
                  key={option}
                  type="button"
                  className={`block w-full rounded-xl px-3 py-2 text-left text-sm font-bold transition ${
                    value === option
                      ? "bg-orange-500/15 text-orange-900 dark:text-orange-200"
                      : "hover:bg-black/5 dark:hover:bg-white/5"
                  }`}
                  onClick={() => {
                    onChange(option);
                    setOpen(false);
                    setQuery("");
                  }}
                >
                  {option}
                </button>
              ))
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function ManualAnswersPage() {
  const [questions, setQuestions] = useState([]);
  const [uniqueQuestions, setUniqueQuestions] = useState([]);
  const [answers, setAnswers] = useState({});
  const [quickAnswers, setQuickAnswers] = useState({});
  const [includeAnswered, setIncludeAnswered] = useState(false);
  const [message, setMessage] = useState("");
  const [savingDomain, setSavingDomain] = useState("");
  const [savingLabel, setSavingLabel] = useState("");

  async function loadAll() {
    const [rows, unique] = await Promise.all([
      api.listUnansweredQuestions(includeAnswered),
      api.listUniqueUnansweredQuestions(),
    ]);
    setQuestions(rows);
    setUniqueQuestions(unique);
  }

  useEffect(() => {
    loadAll().catch((error) => setMessage(error.message));
  }, [includeAnswered]);

  async function applyAllByLabel(uq) {
    const ans = String(quickAnswers[uq.field_label] || "").trim();
    if (!ans) { setMessage("Fill an answer first"); return; }
    try {
      setSavingLabel(uq.field_label);
      const result = await api.answerAllByLabel({
        field_label: uq.field_label,
        answer: ans,
        field_type: uq.field_type || null,
        save_rule: true,
      });
      const restarted = Array.isArray(result.application_restarted) ? result.application_restarted.length : 0;
      setMessage(
        `Saved "${uq.field_label}" for ${result.answered_count} application${result.answered_count === 1 ? "" : "s"}.` +
        (restarted ? ` ${restarted} restarted.` : ""),
      );
      setQuickAnswers((prev) => { const next = { ...prev }; delete next[uq.field_label]; return next; });
      await loadAll();
    } catch (error) {
      setMessage(error.message);
    } finally {
      setSavingLabel("");
    }
  }

  async function saveCompanyAnswers(group) {
    const pendingQuestions = group.questions.filter((q) => !q.answered_at && !q.manual_blocker);
    const payloadAnswers = pendingQuestions
      .map((q) => ({
        question_id: q.id,
        answer: String(answers[q.id] ?? q.recruiter_answer ?? "").trim(),
      }))
      .filter((item) => item.answer);
    const skippedCount = pendingQuestions.length - payloadAnswers.length;
    if (!payloadAnswers.length) {
      setMessage("Fill at least one pending answer before saving");
      return;
    }
    try {
      setSavingDomain(group.domain);
      const result = await api.answerUnansweredQuestionsBatch({ answers: payloadAnswers, save_rule: true });
      const restartedCount = Array.isArray(result.application_restarted) ? result.application_restarted.length : 0;
      setMessage(
        restartedCount
          ? `Saved ${result.answered_count} answers. ${restartedCount} paused application restarted.`
          : `Saved ${result.answered_count} answers${skippedCount ? `; ${skippedCount} blank still pending` : ""}.`,
      );
      setAnswers((current) => {
        const next = { ...current };
        pendingQuestions.forEach((q) => delete next[q.id]);
        return next;
      });
      await loadAll();
    } catch (error) {
      setMessage(error.message);
    } finally {
      setSavingDomain("");
    }
  }

  const groups = groupByCompany(questions);

  return (
    <div className="space-y-5">
      {/* Header */}
      <section className="panel">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-sm font-extrabold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">
              Manual Answers
            </p>
            <h2 className="font-display text-4xl">Answer pending fields</h2>
          </div>
          <label className="inline-flex items-center gap-2 text-sm font-bold">
            <input
              type="checkbox"
              checked={includeAnswered}
              onChange={(event) => setIncludeAnswered(event.target.checked)}
            />
            Show answered
          </label>
        </div>
        {message ? (
          <div className="mt-4 rounded-2xl bg-white/70 px-4 py-3 text-sm font-bold dark:bg-white/10">{message}</div>
        ) : null}
      </section>

      {/* Quick Answers — deduplicated across all jobs */}
      {uniqueQuestions.length > 0 ? (
        <section className="panel">
          <div className="mb-4">
            <p className="text-sm font-extrabold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">
              Quick Answers
            </p>
            <h3 className="font-display text-2xl">Answer once, apply to all jobs</h3>
            <p className="mt-1 text-sm opacity-60">
              Each question appears in multiple applications. Answer once to fill them all.
            </p>
          </div>
          <div className="space-y-3">
            {uniqueQuestions.map((uq) => {
              const opts = Array.isArray(uq.options) ? uq.options.filter((o) => String(o || "").trim()) : [];
              const ft = String(uq.field_type || "").toLowerCase();
              const isChoice = ft.includes("select") || ft === "radio";
              const isTextarea = ft === "textarea";
              const value = String(quickAnswers[uq.field_label] || "");
              const setValue = (v) => setQuickAnswers((prev) => ({ ...prev, [uq.field_label]: v }));
              const isSaving = savingLabel === uq.field_label;

              return (
                <div
                  key={uq.field_label}
                  className="rounded-xl border border-black/10 bg-white/50 p-4 dark:border-white/10 dark:bg-white/5"
                >
                  <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <h4 className="text-sm font-extrabold">{uq.field_label || "Unlabeled field"}</h4>
                      <div className="mt-1 text-xs opacity-60">
                        {uq.field_type || "field"}
                        {uq.sample_domain ? ` · ${uq.sample_domain}` : ""}
                      </div>
                    </div>
                    <span className="badge bg-orange-500/15 text-orange-800 dark:text-orange-300">
                      {uq.count} {uq.count === 1 ? "job" : "jobs"}
                    </span>
                  </div>

                  {isChoice && opts.length > 0 ? (
                    <SearchableSelect
                      value={value}
                      options={opts}
                      disabled={false}
                      placeholder="Select an option"
                      onChange={setValue}
                    />
                  ) : isTextarea ? (
                    <textarea
                      className="field min-h-[80px] w-full resize-y"
                      value={value}
                      placeholder="Type your answer..."
                      rows={3}
                      onChange={(e) => setValue(e.target.value)}
                    />
                  ) : (
                    <input
                      className="field"
                      value={value}
                      placeholder="Type your answer..."
                      onChange={(e) => setValue(e.target.value)}
                    />
                  )}

                  <div className="mt-3 flex justify-end">
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={isSaving || !value.trim()}
                      onClick={() => applyAllByLabel(uq)}
                    >
                      {isSaving ? "Saving..." : `Apply to all ${uq.count} ${uq.count === 1 ? "job" : "jobs"}`}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      {/* Per-application questions */}
      <section className="panel">
        <div className="space-y-4">
          {questions.length === 0 ? (
            <div className="text-sm font-bold opacity-70">No pending manual answers.</div>
          ) : null}
          {groups.map((group) => {
            const pendingCount = group.questions.filter((q) => !q.answered_at && !q.manual_blocker).length;
            const blockerCount = group.questions.filter((q) => q.manual_blocker).length;
            return (
              <div
                key={`${group.domain}-${group.applicationId}`}
                className="rounded-2xl border border-black/10 bg-white/45 p-4 dark:border-white/10 dark:bg-white/5"
              >
                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="text-xs font-extrabold uppercase tracking-wide opacity-60">Company answers</div>
                    <h3 className="mt-1 font-extrabold">{group.domain}</h3>
                    <div className="mt-1 text-[11px] opacity-50">Application: {group.applicationId}</div>
                    <div className="mt-1 text-xs opacity-60">
                      {pendingCount} pending{blockerCount ? `, ${blockerCount} blockers` : ""} of {group.questions.length} items
                    </div>
                  </div>
                  {!pendingCount ? (
                    <span className="badge bg-emerald-500/15 text-emerald-800 dark:text-emerald-300">answered</span>
                  ) : null}
                </div>

                <div className="space-y-4">
                  {group.questions.map((question) => {
                    const capturedOptions = optionList(question.options);
                    const options = capturedOptions.length ? capturedOptions : fallbackChoiceOptions(question);
                    const optionTree = buildOptionTree(options);
                    const fieldType = String(question.field_type || "").toLowerCase();
                    const isChoiceField = fieldType.includes("select") || fieldType.includes("radio");
                    const isChoiceWithoutOptions = isChoiceField && !options.length;
                    const isDateField = fieldType === "date" || (fieldType === "text" && /\bdate\b/i.test(String(question.field_label || "")));
                    const isTextareaField = fieldType === "textarea";
                    const renderAsTextInput = !isChoiceField || isChoiceWithoutOptions || isValidationOnlyOption(fieldType, options);
                    const value = answers[question.id] ?? question.recruiter_answer ?? "";
                    const disabled = Boolean(question.answered_at);
                    const selectedPath = splitOptionPath(value);
                    const selectedParent = selectedPath[0] || "";
                    const selectedChild = selectedPath.slice(1).join(" > ");
                    const childOptions = selectedParent ? optionTree.childrenByParent.get(selectedParent) || [] : [];
                    const setAnswer = (next) => setAnswers((current) => ({ ...current, [question.id]: next }));

                    return (
                      <div
                        key={question.id}
                        className="rounded-xl border border-black/10 bg-white/50 p-3 dark:border-white/10 dark:bg-white/5"
                      >
                        <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
                          <div>
                            <h4 className="text-sm font-extrabold">
                              {question.field_label || "Unlabeled field"}
                            </h4>
                            <div className="mt-1 text-xs opacity-60">
                              {question.field_type || "field"}
                            </div>
                          </div>
                          <span
                            className={`badge ${
                              question.answered_at
                                ? "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300"
                                : "bg-amber-500/15 text-amber-800 dark:text-amber-300"
                            }`}
                          >
                            {question.answered_at ? "answered" : "pending"}
                          </span>
                        </div>

                        {question.manual_blocker ? (
                          <div className="rounded-xl border border-amber-500/20 bg-amber-500/10 p-3 text-sm font-bold text-amber-900 dark:text-amber-200">
                            <div>Application needs manual review before automation can continue.</div>
                            {question.last_error || question.recruiter_answer ? (
                              <div className="mt-2 text-xs opacity-80">{question.last_error || question.recruiter_answer}</div>
                            ) : null}
                          </div>
                        ) : renderAsTextInput ? (
                          isTextareaField ? (
                            <textarea
                              className="field min-h-[80px] w-full resize-y"
                              disabled={disabled}
                              value={value}
                              placeholder="Type your answer..."
                              rows={3}
                              onChange={(e) => setAnswer(e.target.value)}
                            />
                          ) : (
                            <input
                              className="field"
                              disabled={disabled}
                              type={isDateField ? "date" : undefined}
                              value={value}
                              placeholder={isDateField ? "MM/DD/YYYY" : "Type your answer..."}
                              onChange={(event) => setAnswer(event.target.value)}
                            />
                          )
                        ) : options.length && optionTree.hasNested ? (
                          <div className="grid gap-3 sm:grid-cols-2">
                            <SearchableSelect
                              value={selectedParent}
                              options={optionTree.parents}
                              disabled={disabled}
                              placeholder="Select primary"
                              onChange={(parent) => {
                                const children = optionTree.childrenByParent.get(parent) || [];
                                setAnswer(children.length ? `${parent} > ${children[0]}` : parent);
                              }}
                            />
                            <SearchableSelect
                              value={selectedChild}
                              options={childOptions}
                              disabled={disabled || !selectedParent || !childOptions.length}
                              placeholder={selectedParent ? "Select detail" : "Choose primary first"}
                              onChange={(child) => setAnswer(child ? `${selectedParent} > ${child}` : selectedParent)}
                            />
                          </div>
                        ) : options.length ? (
                          <SearchableSelect
                            value={value}
                            options={options}
                            disabled={disabled}
                            placeholder="Select an option"
                            onChange={setAnswer}
                          />
                        ) : (
                          <input
                            className="field"
                            disabled={disabled}
                            type={isDateField ? "date" : undefined}
                            value={value}
                            placeholder={isDateField ? "MM/DD/YYYY" : "Type your answer..."}
                            onChange={(event) => setAnswer(event.target.value)}
                          />
                        )}

                        {renderAsTextInput && isValidationOnlyOption(fieldType, options) ? (
                          <div className="mt-2 text-xs font-bold text-red-700 dark:text-red-300">{options[0]}</div>
                        ) : isChoiceWithoutOptions ? (
                          <div className="mt-2 text-xs font-bold text-amber-800 dark:text-amber-300">
                            Dropdown options were not captured; type the exact answer.
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>

                {pendingCount ? (
                  <div className="mt-4 flex justify-end">
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={savingDomain === group.domain}
                      onClick={() => saveCompanyAnswers(group)}
                    >
                      {savingDomain === group.domain ? "Saving..." : "Save all answers"}
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
