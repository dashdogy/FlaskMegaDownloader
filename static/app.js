(() => {
    const initialPayload = document.getElementById("initial-dashboard");
    if (!initialPayload) {
        return;
    }

    const summaryGrid = document.getElementById("summary-grid");
    const batchList = document.getElementById("batch-list");
    const backendLabel = document.getElementById("backend-label");
    const backendNote = document.getElementById("backend-note");
    const updatedLabel = document.getElementById("dashboard-updated");
    const pollMs = Number(document.body.dataset.pollMs || 1500);

    const escapeHtml = (value) => String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");

    const formatBytes = (value) => {
        if (value === null || value === undefined) {
            return "Unknown";
        }
        const units = ["B", "KB", "MB", "GB", "TB"];
        let size = Number(value);
        let index = 0;
        while (size >= 1024 && index < units.length - 1) {
            size /= 1024;
            index += 1;
        }
        return index === 0 ? `${Math.round(size)} ${units[index]}` : `${size.toFixed(1)} ${units[index]}`;
    };

    const formatSpeed = (value) => value ? `${formatBytes(value)}/s` : "Unknown";

    const formatEta = (value) => {
        if (value === null || value === undefined) {
            return "Estimating";
        }
        if (value <= 0) {
            return "Done";
        }
        const hours = Math.floor(value / 3600);
        const minutes = Math.floor((value % 3600) / 60);
        const seconds = Math.floor(value % 60);
        if (hours > 0) {
            return `${hours}h ${minutes}m`;
        }
        if (minutes > 0) {
            return `${minutes}m ${seconds}s`;
        }
        return `${seconds}s`;
    };

    const progressPercent = (bytesDone, bytesTotal) => {
        if (!bytesTotal) {
            return null;
        }
        return Math.max(0, Math.min(100, (bytesDone / bytesTotal) * 100));
    };

    const renderSummary = (summary) => {
        const totalLabel = summary.has_unknown_total
            ? `${formatBytes(summary.bytes_done)} / partial total`
            : `${formatBytes(summary.bytes_done)} / ${formatBytes(summary.bytes_total)}`;

        summaryGrid.innerHTML = [
            ["Total Jobs", summary.total_jobs],
            ["Queued", summary.queued_jobs],
            ["Active", summary.active_jobs],
            ["Completed", summary.completed_jobs],
            ["Throughput", formatSpeed(summary.throughput_bps)],
        ].map(([label, value]) => `
            <div class="stat-tile">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value)}</strong>
                <small class="subtle">${escapeHtml(totalLabel)}</small>
            </div>
        `).join("");
    };

    const renderJob = (job) => {
        const percent = progressPercent(job.transfer.bytes_done, job.transfer.bytes_total);
        const progressBar = percent === null
            ? '<div class="progress-fill indeterminate"></div>'
            : `<div class="progress-fill" style="width:${percent.toFixed(1)}%"></div>`;

        const actions = [
            job.can_cancel ? `
                <form action="/jobs/${encodeURIComponent(job.id)}/cancel" method="post">
                    <button type="submit">Cancel</button>
                </form>
            ` : "",
            job.can_retry ? `
                <form action="/jobs/${encodeURIComponent(job.id)}/retry" method="post">
                    <button type="submit">Retry</button>
                </form>
            ` : "",
            `<a class="ghost-button" href="/explorer?root=${encodeURIComponent(job.destination_key)}&path=${encodeURIComponent(job.destination_relative_path || "")}">Open Destination</a>`,
        ].join("");

        return `
            <article class="job-card">
                <div class="job-header">
                    <div>
                        <strong>${escapeHtml(job.display_name)}</strong>
                        <p class="job-url">${escapeHtml(job.url)}</p>
                    </div>
                    <span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(job.status_label)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(job.destination_display)}</span>
                    <span>${escapeHtml(formatBytes(job.transfer.bytes_done))}</span>
                    <span>${escapeHtml(job.transfer.bytes_total ? formatBytes(job.transfer.bytes_total) : "Total unknown")}</span>
                    <span>${escapeHtml(formatSpeed(job.transfer.speed_bps))}</span>
                    <span>${escapeHtml(formatEta(job.transfer.eta_seconds))}</span>
                </div>
                <div class="progress-track">${progressBar}</div>
                <div class="metric-row">
                    <span>${escapeHtml(job.transfer.last_message || "Waiting for worker output.")}</span>
                </div>
                ${job.error ? `<div class="flash flash-error">${escapeHtml(job.error)}</div>` : ""}
                <div class="job-actions">${actions}</div>
            </article>
        `;
    };

    const renderBatch = (batch) => {
        const percent = progressPercent(batch.bytes_done, batch.bytes_total);
        const progressBar = (percent === null || batch.has_unknown_total)
            ? '<div class="progress-fill indeterminate"></div>'
            : `<div class="progress-fill" style="width:${percent.toFixed(1)}%"></div>`;
        const statusSummary = Object.entries(batch.status_counts)
            .map(([status, count]) => `${status}: ${count}`)
            .join(" | ");

        return `
            <section class="batch-card">
                <div class="batch-header">
                    <div>
                        <p class="eyebrow">Batch ${escapeHtml(batch.id)}</p>
                        <strong>${escapeHtml(`${batch.job_count} job(s)`)}</strong>
                    </div>
                    <span class="status-pill">${escapeHtml(statusSummary || "No jobs")}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(formatBytes(batch.bytes_done))}</span>
                    <span>${escapeHtml(batch.has_unknown_total ? "Partial total" : formatBytes(batch.bytes_total))}</span>
                    <span>${escapeHtml(formatSpeed(batch.speed_bps))}</span>
                    <span>${escapeHtml(formatEta(batch.eta_seconds))}</span>
                </div>
                <div class="progress-track">${progressBar}</div>
                <div class="job-list">${batch.jobs.map(renderJob).join("")}</div>
            </section>
        `;
    };

    const renderDashboard = (payload) => {
        if (backendLabel) {
            backendLabel.textContent = payload.backend.label;
        }
        if (backendNote) {
            backendNote.textContent = payload.backend.reason || "";
        }
        if (updatedLabel) {
            updatedLabel.textContent = `Updated ${payload.updated_at}`;
        }

        renderSummary(payload.summary);
        batchList.innerHTML = payload.batches.length
            ? payload.batches.map(renderBatch).join("")
            : '<div class="stat-tile"><span>No jobs yet</span><strong>Queue is empty</strong><small class="subtle">Submit one or more MEGA URLs to begin.</small></div>';
    };

    const poll = async () => {
        try {
            const response = await fetch("/api/jobs", {
                headers: { "Accept": "application/json" },
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            renderDashboard(payload);
        } catch (error) {
            console.error("Dashboard refresh failed", error);
        }
    };

    renderDashboard(JSON.parse(initialPayload.textContent));
    window.setInterval(poll, pollMs);
})();
