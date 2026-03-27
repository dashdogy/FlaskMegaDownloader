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

    const clampPercent = (value) => {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return null;
        }
        return Math.max(0, Math.min(100, Number(value)));
    };

    const isStoppedStatus = (status) => status === "failed" || status === "canceled";
    const isPausedStatus = (status) => status === "paused";
    const isActiveStatus = (status) => status === "starting" || status === "probing" || status === "downloading" || status === "active";

    const progressPercent = (transfer) => {
        if (transfer?.bytes_total) {
            return clampPercent((transfer.bytes_done / transfer.bytes_total) * 100);
        }
        return clampPercent(transfer?.percent);
    };

    const batchProgressPercent = (batch) => {
        if (batch.bytes_total && !batch.has_unknown_total) {
            return clampPercent((batch.bytes_done / batch.bytes_total) * 100);
        }
        if (!Array.isArray(batch.jobs) || !batch.jobs.length) {
            return null;
        }
        const percents = batch.jobs.map((job) => {
            const percent = progressPercent(job.transfer);
            if (percent !== null) {
                return percent;
            }
            return job.status === "completed" ? 100 : 0;
        });
        return clampPercent(percents.reduce((sum, percent) => sum + percent, 0) / percents.length);
    };

    const buildProgressBar = (status, percent, indeterminate = false) => {
        let trackClass = "progress-track";
        let fillClass = "progress-fill";

        if (isStoppedStatus(status)) {
            trackClass += " failed";
            fillClass += " failed";
            const width = percent === null ? "100%" : `${percent.toFixed(1)}%`;
            return `<div class="${trackClass}"><div class="${fillClass}" style="width:${width}"></div></div>`;
        }

        if (isPausedStatus(status)) {
            trackClass += " paused";
            fillClass += " paused";
            const width = percent === null ? "35%" : `${percent.toFixed(1)}%`;
            return `<div class="${trackClass}"><div class="${fillClass}" style="width:${width}"></div></div>`;
        }

        if (status === "completed") {
            trackClass += " completed";
            fillClass += " completed";
            return `<div class="${trackClass}"><div class="${fillClass}" style="width:100%"></div></div>`;
        }

        if (status === "queued") {
            return `<div class="${trackClass}"><div class="${fillClass}" style="width:0%"></div></div>`;
        }

        if (percent === null || indeterminate) {
            return `<div class="${trackClass}"><div class="${fillClass} indeterminate"></div></div>`;
        }

        return `<div class="${trackClass}"><div class="${fillClass}" style="width:${percent.toFixed(1)}%"></div></div>`;
    };

    const batchVisualStatus = (batch) => {
        const counts = batch.status_counts || {};
        if (counts.starting || counts.probing || counts.downloading || counts.queued) {
            return "active";
        }
        if (counts.paused) {
            return "paused";
        }
        if (counts.failed || counts.canceled) {
            return "failed";
        }
        if (counts.completed) {
            return "completed";
        }
        return "queued";
    };

    const renderSummary = (summary) => {
        const totalLabel = summary.has_unknown_total
            ? `${formatBytes(summary.bytes_done)} / partial total`
            : `${formatBytes(summary.bytes_done)} / ${formatBytes(summary.bytes_total)}`;

        summaryGrid.innerHTML = [
            ["Total Jobs", summary.total_jobs],
            ["Queued", summary.queued_jobs],
            ["Paused", summary.paused_jobs],
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
        const percent = progressPercent(job.transfer);
        const progressBar = buildProgressBar(job.status, percent, isActiveStatus(job.status) && percent === null);
        const speedLabel = isPausedStatus(job.status) ? "Paused" : (isStoppedStatus(job.status) ? "Stopped" : formatSpeed(job.transfer.speed_bps));
        const etaLabel = isPausedStatus(job.status) ? "Paused" : (isStoppedStatus(job.status) ? "Stopped" : formatEta(job.transfer.eta_seconds));

        const openDestination = job.explorer_root
            ? `<a class="ghost-button" href="/explorer?root=${encodeURIComponent(job.explorer_root)}&path=${encodeURIComponent(job.explorer_path || "")}">Open Destination</a>`
            : `<span class="subtle">Explorer unavailable for custom path</span>`;

        const actions = [
            job.can_pause ? `
                <form action="/jobs/${encodeURIComponent(job.id)}/pause" method="post">
                    <button type="submit" class="secondary-button">Pause</button>
                </form>
            ` : "",
            job.can_resume ? `
                <form action="/jobs/${encodeURIComponent(job.id)}/resume" method="post">
                    <button type="submit" class="secondary-button">Resume</button>
                </form>
            ` : "",
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
            openDestination,
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
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
                <div class="metric-row">
                    <span>${escapeHtml(job.transfer.last_message || "Waiting for worker output.")}</span>
                </div>
                ${job.error ? `<div class="flash flash-error">${escapeHtml(job.error)}</div>` : ""}
                <div class="job-actions">${actions}</div>
            </article>
        `;
    };

    const renderBatch = (batch) => {
        const visualStatus = batchVisualStatus(batch);
        const percent = batchProgressPercent(batch);
        const progressBar = buildProgressBar(visualStatus, percent, isActiveStatus(visualStatus) && percent === null);
        const speedLabel = isPausedStatus(visualStatus) ? "Paused" : formatSpeed(batch.speed_bps);
        const etaLabel = isPausedStatus(visualStatus) ? "Paused" : formatEta(batch.eta_seconds);
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
                    <span class="status-pill ${escapeHtml(visualStatus)}">${escapeHtml(statusSummary || "No jobs")}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(formatBytes(batch.bytes_done))}</span>
                    <span>${escapeHtml(batch.has_unknown_total ? "Partial total" : formatBytes(batch.bytes_total))}</span>
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
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
