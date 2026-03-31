(() => {
    const initialPayload = document.getElementById("initial-logs");
    if (!initialPayload) {
        return;
    }

    const logStream = document.getElementById("log-stream");
    const updatedLabel = document.getElementById("logs-updated");
    const followStateLabel = document.getElementById("log-follow-state");
    const pollMs = Number(document.body.dataset.pollMs || 1500);
    const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "medium",
    });
    let payload = JSON.parse(initialPayload.textContent);
    let lastId = Number(payload.last_id || 0);

    const escapeHtml = (value) => String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");

    const formatTimestamp = (value) => {
        if (!value) {
            return "Unknown";
        }
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) {
            return String(value);
        }
        return dateTimeFormatter.format(parsed);
    };

    const renderContext = (context) => {
        if (!context || typeof context !== "object" || !Object.keys(context).length) {
            return "";
        }
        return `<pre class="log-context">${escapeHtml(JSON.stringify(context, null, 2))}</pre>`;
    };

    const renderEntry = (entry) => {
        const refs = [];
        if (entry.feature) {
            refs.push(`<span class="log-chip">${escapeHtml(entry.feature)}</span>`);
        }
        if (entry.job_id) {
            refs.push(`<span class="log-chip">job ${escapeHtml(entry.job_id)}</span>`);
        }
        if (entry.batch_id) {
            refs.push(`<span class="log-chip">batch ${escapeHtml(entry.batch_id)}</span>`);
        }
        return `
            <article class="log-entry" data-log-id="${escapeHtml(entry.id)}">
                <div class="log-entry-main">
                    <div class="log-entry-meta">
                        <span class="log-timestamp">${escapeHtml(formatTimestamp(entry.created_at))}</span>
                        <span class="status-pill ${escapeHtml(entry.level)}">${escapeHtml(entry.level)}</span>
                        <span class="status-pill">${escapeHtml(entry.subsystem)}</span>
                        ${refs.join("")}
                    </div>
                    <p class="log-message">${escapeHtml(entry.message)}</p>
                    ${renderContext(entry.context)}
                </div>
            </article>
        `;
    };

    const isFollowingBottom = () => {
        const threshold = 40;
        return (logStream.scrollHeight - logStream.scrollTop - logStream.clientHeight) <= threshold;
    };

    const updateFollowLabel = () => {
        if (!followStateLabel) {
            return;
        }
        followStateLabel.textContent = isFollowingBottom()
            ? "Auto-following newest lines"
            : "Scroll to the bottom to resume auto-follow";
    };

    const scrollToBottom = () => {
        logStream.scrollTop = logStream.scrollHeight;
        updateFollowLabel();
    };

    const replaceEntries = (entries) => {
        logStream.innerHTML = entries.length
            ? entries.map(renderEntry).join("")
            : '<div class="stat-tile"><span>No logs yet</span><strong>Waiting for runtime events</strong><small class="subtle">New lines will appear here as the app processes work.</small></div>';
    };

    const appendEntries = (entries) => {
        if (!entries.length) {
            return;
        }
        const shouldFollow = isFollowingBottom();
        logStream.insertAdjacentHTML("beforeend", entries.map(renderEntry).join(""));
        if (shouldFollow) {
            scrollToBottom();
        } else {
            updateFollowLabel();
        }
    };

    const renderInitial = () => {
        replaceEntries(payload.entries || []);
        if (updatedLabel) {
            updatedLabel.textContent = `Updated ${formatTimestamp(payload.updated_at)}`;
        }
        requestAnimationFrame(scrollToBottom);
    };

    const poll = async () => {
        try {
            const response = await fetch(`/api/logs?after_id=${encodeURIComponent(lastId)}`, {
                headers: { "Accept": "application/json" },
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const nextPayload = await response.json();
            const entries = Array.isArray(nextPayload.entries) ? nextPayload.entries : [];
            if (entries.length) {
                lastId = Number(nextPayload.last_id || entries[entries.length - 1].id || lastId);
                appendEntries(entries);
            }
            if (updatedLabel) {
                updatedLabel.textContent = `Updated ${formatTimestamp(nextPayload.updated_at)}`;
            }
        } catch (error) {
            console.error("Log refresh failed", error);
        }
    };

    logStream.addEventListener("scroll", updateFollowLabel, { passive: true });
    renderInitial();
    window.setInterval(poll, pollMs);
})();
