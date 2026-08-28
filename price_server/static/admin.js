(() => {
    "use strict";

    const actionHeader = {"X-Requested-With": "TexnikachPriceAdmin"};

    const style = document.createElement("style");
    style.textContent = `
        .price-server-actions {
            display: flex;
            flex-wrap: wrap;
            gap: .5rem;
            margin-left: auto;
        }
        .price-server-action {
            border: 1px solid rgba(14, 82, 54, .24);
            border-radius: 999px;
            background: #fff;
            color: #164e3b;
            cursor: pointer;
            font: inherit;
            font-size: .84rem;
            font-weight: 650;
            padding: .58rem .85rem;
        }
        .price-server-action:hover { background: #eef8f3; }
        .price-server-action:disabled { cursor: wait; opacity: .55; }
        .price-server-action.primary { background: #176b4b; color: #fff; }
        .price-server-toast {
            position: fixed;
            z-index: 10000;
            right: 1rem;
            bottom: 1rem;
            max-width: min(28rem, calc(100vw - 2rem));
            border-radius: .8rem;
            box-shadow: 0 12px 35px rgba(0, 0, 0, .22);
            background: #173f32;
            color: #fff;
            padding: .9rem 1rem;
        }
        .price-server-toast.error { background: #8f2d2d; }
        .price-server-job-panel {
            margin: 1rem auto 1.5rem;
            max-width: 72rem;
            border: 1px solid rgba(14, 82, 54, .18);
            border-radius: 1rem;
            background: #f8fcfa;
            padding: 1rem;
        }
        .price-server-job-heading {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
        }
        .price-server-job-heading h2 { margin: 0; font-size: 1.15rem; }
        .price-server-job-list { display: grid; gap: .65rem; margin-top: .8rem; }
        .price-server-job-row {
            display: grid;
            grid-template-columns: minmax(9rem, 1fr) minmax(12rem, 2fr) auto auto;
            align-items: center;
            gap: .65rem;
            border-radius: .7rem;
            background: #fff;
            padding: .7rem .8rem;
        }
        .price-server-job-status { color: #47645a; font-size: .86rem; }
        .price-server-empty { color: #60766e; margin: .7rem 0 0; }
        @media (max-width: 680px) {
            .price-server-actions { width: 100%; margin-left: 0; }
            .price-server-action { flex: 1 1 auto; }
            .price-server-job-row { grid-template-columns: 1fr; }
        }
    `;
    document.head.appendChild(style);

    const toast = (message, error = false) => {
        const previous = document.querySelector(".price-server-toast");
        if (previous) previous.remove();
        const element = document.createElement("div");
        element.className = `price-server-toast${error ? " error" : ""}`;
        element.setAttribute("role", error ? "alert" : "status");
        element.textContent = message;
        document.body.appendChild(element);
        window.setTimeout(() => element.remove(), error ? 9000 : 5000);
    };

    const request = async (path, payload = {}, extraHeaders = {}) => {
        const response = await fetch(path, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                ...actionHeader,
                ...extraHeaders,
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        let body = {};
        try { body = await response.json(); } catch (_) { /* no-op */ }
        if (!response.ok) {
            const error = new Error(body.detail || `Ошибка сервера: ${response.status}`);
            error.status = response.status;
            error.detail = body.detail || "";
            throw error;
        }
        return body;
    };

    const run = async (button, path, payload, successText) => {
        const buttons = button.closest(".price-server-actions").querySelectorAll("button");
        buttons.forEach((item) => { item.disabled = true; });
        try {
            const result = await request(path, payload);
            toast(`${successText} Задача #${result.job_id}.`);
            await refreshJobs();
        } catch (error) {
            toast(error instanceof Error ? error.message : "Неизвестная ошибка", true);
        } finally {
            buttons.forEach((item) => { item.disabled = false; });
        }
    };

    const makeButton = (label, title, className = "") => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `price-server-action ${className}`.trim();
        button.textContent = label;
        button.title = title;
        return button;
    };

    const sectionTitles = new Map(
        [...document.querySelectorAll(".major-price-section[id]")].map((section) => [
            section.id,
            section.getAttribute("aria-label") || section.id,
        ])
    );
    const jobPanel = document.createElement("section");
    jobPanel.className = "price-server-job-panel";
    const jobHeading = document.createElement("div");
    jobHeading.className = "price-server-job-heading";
    const jobTitle = document.createElement("h2");
    jobTitle.textContent = "Расписание Telegram";
    const refreshAll = makeButton(
        "Обновить все посты",
        "Поставить в очередь обновление всех текущих прайс-постов",
        "primary"
    );
    const refresh = makeButton("Обновить список", "Обновить список заданий");
    const headingActions = document.createElement("div");
    headingActions.className = "price-server-actions";
    headingActions.append(refreshAll, refresh);
    const jobList = document.createElement("div");
    jobList.className = "price-server-job-list";
    jobHeading.append(jobTitle, headingActions);
    jobPanel.append(jobHeading, jobList);

    const pageMain = document.querySelector("main") || document.body;
    pageMain.insertBefore(jobPanel, pageMain.firstChild);

    const statusNames = {
        pending: "запланировано",
        running: "отправляется",
        done: "выполнено",
        failed: "ошибка",
        needs_review: "нужна проверка",
        cancelled: "отменено",
        skipped: "пропущено",
    };
    const actionNames = {send: "Новый пост", edit: "Обновление поста"};
    const tashkentDate = new Intl.DateTimeFormat("ru-RU", {
        timeZone: "Asia/Tashkent",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });

    async function refreshJobs() {
        refresh.disabled = true;
        try {
            const response = await fetch("/price/api/v1/jobs", {
                credentials: "same-origin",
                cache: "no-store",
            });
            if (!response.ok) throw new Error(`Ошибка расписания: ${response.status}`);
            const body = await response.json();
            const jobs = Array.isArray(body.jobs) ? body.jobs : [];
            const editBatches = Array.isArray(body.edit_batches)
                ? body.edit_batches
                : [];
            const rotations = Array.isArray(body.quick_link_rotations)
                ? body.quick_link_rotations
                : [];
            const active = new Set(["pending", "running"]);
            jobs.sort((left, right) => {
                const leftActive = active.has(left.status);
                const rightActive = active.has(right.status);
                if (leftActive !== rightActive) return leftActive ? -1 : 1;
                const comparison = String(left.execute_at).localeCompare(String(right.execute_at));
                return leftActive ? comparison : -comparison;
            });
            const bulkJobsByBatch = new Map();
            const regularJobs = [];
            jobs.forEach((job) => {
                const payload = job && typeof job.payload === "object"
                    ? job.payload
                    : {};
                if (payload.source !== "price_admin_edit_all" || !payload.batch_id) {
                    regularJobs.push(job);
                    return;
                }
                const key = String(payload.batch_id);
                if (!bulkJobsByBatch.has(key)) bulkJobsByBatch.set(key, []);
                bulkJobsByBatch.get(key).push(job);
            });
            const batchRows = editBatches.length
                ? editBatches
                : [...bulkJobsByBatch.entries()].map(([batchId, batchJobs]) => ({
                    batch_id: batchId,
                    created_at: batchJobs[0] ? batchJobs[0].execute_at : "",
                    job_count: batchJobs.length,
                    section_count: batchJobs.reduce((total, job) => {
                        const keys = job.payload && Array.isArray(job.payload.section_keys)
                            ? job.payload.section_keys : [];
                        return total + keys.length;
                    }, 0),
                    skipped: {},
                }));
            jobList.replaceChildren();

            if (!regularJobs.length && !batchRows.length && !rotations.length) {
                const empty = document.createElement("p");
                empty.className = "price-server-empty";
                empty.textContent = "Запланированных и выполненных заданий пока нет.";
                jobList.appendChild(empty);
                return;
            }

            batchRows.slice(0, 20).forEach((batch) => {
                const batchId = String(batch.batch_id || "");
                const batchJobs = bulkJobsByBatch.get(batchId) || [];
                const row = document.createElement("div");
                row.className = "price-server-job-row";
                const date = document.createElement("time");
                const timestamp = String(
                    batch.created_at
                    || (batchJobs[0] ? batchJobs[0].execute_at : "")
                    || ""
                );
                date.dateTime = timestamp;
                const parsed = new Date(timestamp);
                date.textContent = Number.isNaN(parsed.getTime())
                    ? (timestamp || "—")
                    : tashkentDate.format(parsed);
                const sectionCount = Number(batch.section_count || 0);
                const jobCount = Number(batch.job_count || batchJobs.length || 0);
                const name = document.createElement("span");
                name.textContent = `Все посты · ${jobCount} сообщений / ${sectionCount} разделов`;
                const statusCounts = batch && typeof batch.job_status_counts === "object"
                    ? batch.job_status_counts : {};
                const done = Number(
                    statusCounts.done
                    ?? batchJobs.filter((job) => job.status === "done").length
                );
                const skippedJobs = Number(
                    batch.skipped_job_count
                    ?? batchJobs.filter(
                        (job) => job.result && job.result.status === "skipped"
                    ).length
                );
                const preflightSkipped = Object.values(batch.skipped || {}).reduce(
                    (total, items) => total + (Array.isArray(items) ? items.length : 0),
                    0
                );
                const errors = Number(statusCounts.failed || 0)
                    + Number(statusCounts.needs_review || 0)
                    || batchJobs.filter(
                        (job) => ["failed", "needs_review"].includes(job.status)
                    ).length;
                const state = document.createElement("span");
                state.className = "price-server-job-status";
                state.textContent = `Пакет ${batchId.slice(0, 8)}: готово ${done}/${jobCount}`
                    + (skippedJobs ? ` · пропущено заданий ${skippedJobs}` : "")
                    + (preflightSkipped ? ` · пропущено разделов ${preflightSkipped}` : "")
                    + (errors ? ` · ошибок ${errors}` : "");
                row.append(date, name, state);
                jobList.appendChild(row);
            });

            regularJobs.slice(0, 100).forEach((job) => {
                const row = document.createElement("div");
                row.className = "price-server-job-row";
                const date = document.createElement("time");
                date.dateTime = String(job.execute_at || "");
                const parsed = new Date(job.execute_at);
                date.textContent = Number.isNaN(parsed.getTime())
                    ? String(job.execute_at || "—")
                    : tashkentDate.format(parsed);
                const name = document.createElement("span");
                name.textContent = sectionTitles.get(job.section_key) || job.section_key || "Раздел";
                const state = document.createElement("span");
                state.className = "price-server-job-status";
                state.textContent = `${actionNames[job.action] || job.action}: ${statusNames[job.status] || job.status}`;
                row.append(date, name, state);

                if (job.status === "pending") {
                    const cancel = makeButton("Отменить", "Отменить запланированное задание");
                    cancel.addEventListener("click", async () => {
                        if (!window.confirm("Отменить эту публикацию?")) return;
                        cancel.disabled = true;
                        try {
                            await request(`/price/api/v1/jobs/${encodeURIComponent(job.job_id)}/cancel`);
                            toast(`Задача #${job.job_id} отменена.`);
                            await refreshJobs();
                        } catch (error) {
                            toast(error instanceof Error ? error.message : "Ошибка отмены", true);
                        } finally {
                            cancel.disabled = false;
                        }
                    });
                    row.appendChild(cancel);
                }
                jobList.appendChild(row);
            });

            rotations.slice(0, 30).forEach((rotation) => {
                const row = document.createElement("div");
                row.className = "price-server-job-row";
                const scheduled = document.createElement("time");
                scheduled.dateTime = String(rotation.scheduled_for || "");
                const parsed = new Date(rotation.scheduled_for);
                scheduled.textContent = Number.isNaN(parsed.getTime())
                    ? String(rotation.scheduled_for || "—")
                    : tashkentDate.format(parsed);
                const name = document.createElement("span");
                name.textContent = `Главный каталог → ${rotation.secondary_title || rotation.secondary_quick_post_key || "раздел"}`;
                const state = document.createElement("span");
                state.className = "price-server-job-status";
                state.textContent = `Ротация: ${statusNames[rotation.status] || rotation.status} · ${rotation.phase || "—"}`;
                row.append(scheduled, name, state);
                if (rotation.status === "needs_review") {
                    const reconcile = makeButton(
                        "Проверить",
                        "Указать результат неоднозначной отправки"
                    );
                    reconcile.addEventListener("click", async () => {
                        const value = window.prompt(
                            "Проверьте основной канал. Введите ID нового главного поста. Если пост точно не появился, введите НЕТ."
                        );
                        if (!value) return;
                        const normalized = value.trim();
                        let payload;
                        if (/^\d+$/.test(normalized)) {
                            payload = {
                                outcome: "sent",
                                new_main_message_id: Number(normalized),
                            };
                        } else if (normalized.toLocaleUpperCase("ru-RU") === "НЕТ") {
                            if (!window.confirm(
                                "Подтверждаете, что новый главный пост не был опубликован? Только после этого отправка будет повторена."
                            )) return;
                            payload = {
                                outcome: "not_sent",
                                confirm_no_message_was_published: true,
                            };
                        } else {
                            toast("Введите числовой ID поста или слово НЕТ.", true);
                            return;
                        }
                        reconcile.disabled = true;
                        try {
                            await request(
                                `/price/api/v1/quick-link-rotations/${encodeURIComponent(rotation.rotation_id)}/reconcile`,
                                payload
                            );
                            toast(`Ротация #${rotation.rotation_id} продолжена.`);
                            await refreshJobs();
                        } catch (error) {
                            toast(error instanceof Error ? error.message : "Ошибка проверки", true);
                        } finally {
                            reconcile.disabled = false;
                        }
                    });
                    row.appendChild(reconcile);
                } else if (rotation.status === "failed") {
                    const retry = makeButton(
                        "Повторить",
                        "Продолжить с последнего сохранённого этапа"
                    );
                    retry.addEventListener("click", async () => {
                        if (!window.confirm(
                            "Повторить ротацию с последнего безопасно сохранённого этапа?"
                        )) return;
                        retry.disabled = true;
                        try {
                            await request(
                                `/price/api/v1/quick-link-rotations/${encodeURIComponent(rotation.rotation_id)}/retry`
                            );
                            toast(`Ротация #${rotation.rotation_id} снова поставлена в очередь.`);
                            await refreshJobs();
                        } catch (error) {
                            toast(error instanceof Error ? error.message : "Ошибка повтора", true);
                        } finally {
                            retry.disabled = false;
                        }
                    });
                    row.appendChild(retry);
                }
                jobList.appendChild(row);
            });
        } catch (error) {
            jobList.replaceChildren();
            const failed = document.createElement("p");
            failed.className = "price-server-empty";
            failed.textContent = error instanceof Error ? error.message : "Не удалось загрузить расписание.";
            jobList.appendChild(failed);
        } finally {
            refresh.disabled = false;
        }
    }

    const bulkStorageKey = "price-server-update-all-idempotency";
    const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    const createIdempotencyKey = () => {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
            const value = Math.floor(Math.random() * 16);
            const normalized = char === "x" ? value : ((value & 0x3) | 0x8);
            return normalized.toString(16);
        });
    };

    refreshAll.addEventListener("click", async () => {
        if (!window.confirm(
            "Обновить все текущие прайс-посты из последнего snapshot? Новые сообщения создаваться не будут."
        )) return;
        let idempotencyKey = window.sessionStorage.getItem(bulkStorageKey);
        if (idempotencyKey && !uuidPattern.test(idempotencyKey)) {
            window.sessionStorage.removeItem(bulkStorageKey);
            idempotencyKey = null;
        }
        if (!idempotencyKey) {
            idempotencyKey = createIdempotencyKey();
            window.sessionStorage.setItem(bulkStorageKey, idempotencyKey);
        }
        refreshAll.disabled = true;
        try {
            const result = await request(
                "/price/api/v1/posts/update-all",
                {confirm: true},
                {"Idempotency-Key": idempotencyKey}
            );
            window.sessionStorage.removeItem(bulkStorageKey);
            const skipped = Object.values(result.skipped || {}).reduce(
                (total, items) => total + (Array.isArray(items) ? items.length : 0),
                0
            );
            toast(
                `В очередь поставлено ${result.job_count} постов`
                + ` (${result.section_count} разделов)`
                + (skipped ? `; пропущено разделов: ${skipped}.` : ".")
            );
            await refreshJobs();
        } catch (error) {
            if (
                error instanceof Error
                && (
                    (
                        error.status === 422
                        && error.detail === "valid_idempotency_key_required"
                    )
                    || (
                        error.status === 409
                        && error.detail === "idempotency_key_conflict"
                    )
                )
            ) {
                window.sessionStorage.removeItem(bulkStorageKey);
            }
            toast(
                error instanceof Error ? error.message : "Ошибка массового обновления",
                true
            );
        } finally {
            refreshAll.disabled = false;
        }
    });

    refresh.addEventListener("click", refreshJobs);

    const loadEditableSections = async () => {
        try {
            const response = await fetch("/price/api/v1/sections", {
                credentials: "same-origin",
                cache: "no-store",
            });
            if (!response.ok) return new Set();
            const body = await response.json();
            return new Set(
                (Array.isArray(body.sections) ? body.sections : [])
                    .filter((section) => section && section.can_edit)
                    .map((section) => String(section.section_key || ""))
                    .filter(Boolean)
            );
        } catch (_) {
            return new Set();
        }
    };

    const initializeActions = async () => {
        const editableSections = await loadEditableSections();
        document.querySelectorAll(".major-price-section[id]").forEach((section) => {
            const toolbar = section.querySelector(".major-section-toolbar");
            if (!toolbar || toolbar.querySelector(".price-server-actions")) return;

            const key = section.id;
            const actions = document.createElement("div");
            actions.className = "price-server-actions";

            const send = makeButton("Отправить сейчас", "Создать новый пост в канале", "primary");
            send.addEventListener("click", () => {
                if (!window.confirm("Отправить этот прайс новым постом прямо сейчас?")) return;
                run(send, `/price/api/v1/sections/${encodeURIComponent(key)}/send-now`, {}, "Отправка поставлена в очередь.");
            });

            const tomorrow = makeButton("Завтра 09:30", "Отправить новый пост завтра в 09:30 по Ташкенту");
            tomorrow.addEventListener("click", () => {
                run(tomorrow, `/price/api/v1/sections/${encodeURIComponent(key)}/schedule`, {when: "tomorrow_0930", mode: "send"}, "Публикация запланирована.");
            });

            const schedule = makeButton("Выбрать время", "Назначить дату и время публикации");
            schedule.addEventListener("click", () => {
                const value = window.prompt("Дата и время по Ташкенту (ГГГГ-ММ-ДД ЧЧ:ММ):");
                if (!value) return;
                run(schedule, `/price/api/v1/sections/${encodeURIComponent(key)}/schedule`, {when: value, mode: "send"}, "Публикация запланирована.");
            });

            actions.append(send);
            if (editableSections.has(key)) {
                const edit = makeButton("Обновить пост", "Изменить последний актуальный пост этого раздела");
                edit.addEventListener("click", () => {
                    if (!window.confirm("Обновить существующий пост этого раздела?")) return;
                    run(edit, `/price/api/v1/sections/${encodeURIComponent(key)}/edit-current`, {}, "Обновление поставлено в очередь.");
                });
                actions.append(edit);
            }
            actions.append(tomorrow, schedule);
            toolbar.appendChild(actions);
        });
    };

    refreshJobs();
    initializeActions();
})();
