(() => {
    const initialPayload = document.getElementById("initial-dashboard");
    if (!initialPayload) {
        return;
    }

    const summaryGrid = document.getElementById("summary-grid");
    const batchList = document.getElementById("batch-list");
    const backendLabel = document.getElementById("backend-label");
    const backendNote = document.getElementById("backend-note");
    const archiveSummaryGrid = document.getElementById("archive-summary-grid");
    const archiveJobList = document.getElementById("archive-job-list");
    const mediaSummaryGrid = document.getElementById("media-summary-grid");
    const mediaJobList = document.getElementById("media-job-list");
    const mediaBackendLabel = document.getElementById("media-backend-label");
    const mediaBackendNote = document.getElementById("media-backend-note");
    const updatedLabel = document.getElementById("dashboard-updated");
    const bulkPauseToggleButton = document.getElementById("bulk-pause-toggle");
    const pollMs = Number(document.body.dataset.pollMs || 1500);
    const scrollStorageKey = `dashboard-scroll:${window.location.pathname}`;
    const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
    });

    const rememberScrollPosition = () => {
        try {
            sessionStorage.setItem(scrollStorageKey, JSON.stringify({ top: window.scrollY }));
        } catch (error) {
            console.debug("Scroll position could not be saved", error);
        }
    };

    const restoreScrollPosition = () => {
        try {
            const savedValue = sessionStorage.getItem(scrollStorageKey);
            if (!savedValue) {
                return;
            }
            sessionStorage.removeItem(scrollStorageKey);
            const savedState = JSON.parse(savedValue);
            const top = Number(savedState?.top);
            if (!Number.isFinite(top) || top < 0) {
                return;
            }
            window.requestAnimationFrame(() => {
                window.requestAnimationFrame(() => {
                    window.scrollTo(0, top);
                });
            });
        } catch (error) {
            console.debug("Scroll position could not be restored", error);
        }
    };

    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        const method = (form.getAttribute("method") || "get").toLowerCase();
        if (method !== "post") {
            return;
        }
        rememberScrollPosition();
    }, true);

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

    const formatPartialTotal = (value) => {
        if (value === null || value === undefined || Number(value) <= 0) {
            return "Unknown";
        }
        return formatBytes(value);
    };

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

    const clampPercent = (value) => {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return null;
        }
        return Math.max(0, Math.min(100, Number(value)));
    };

    const parsePercentFromMessage = (message) => {
        const match = String(message || "").match(/(\d{1,3}(?:\.\d+)?)\s*%/);
        return match ? clampPercent(match[1]) : null;
    };

    const formatPercentLabel = (value) => {
        const percent = clampPercent(value);
        if (percent === null) {
            return null;
        }
        if (percent >= 100 || Number.isInteger(percent)) {
            return `${percent.toFixed(0)}%`;
        }
        if (percent >= 10) {
            return `${percent.toFixed(1)}%`;
        }
        return `${percent.toFixed(2)}%`;
    };

    const isTransferDebugMessage = (message) => /^TRANSFERRING\b/i.test(String(message || "").trim());

    const isStoppedStatus = (status) => status === "failed" || status === "canceled";
    const isPausedStatus = (status) => status === "paused";
    const isActiveStatus = (status) => status === "starting" || status === "probing" || status === "downloading" || status === "active";
    const isMediaActiveStatus = (status) => status === "scanning" || status === "compiling" || status === "verifying";

    const progressPercent = (transfer) => {
        if (transfer?.bytes_total) {
            return clampPercent((transfer.bytes_done / transfer.bytes_total) * 100);
        }
        return clampPercent(transfer?.percent) ?? parsePercentFromMessage(transfer?.last_message);
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
            const width = percent === null ? "0%" : `${percent.toFixed(1)}%`;
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
            ? `${formatBytes(summary.bytes_done)} / ${formatPartialTotal(summary.bytes_total)}`
            : `${formatBytes(summary.bytes_done)} / ${formatBytes(summary.bytes_total)}`;

        summaryGrid.innerHTML = [
            ["Total Jobs", summary.total_jobs],
            ["Queued", summary.queued_jobs],
            ["Paused", summary.paused_jobs],
            ["Active", summary.active_jobs],
            ["Completed", summary.completed_jobs],
            ["Download Speed", formatSpeed(summary.throughput_bps)],
        ].map(([label, value]) => `
            <div class="stat-tile">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value)}</strong>
                <small class="subtle">${escapeHtml(totalLabel)}</small>
            </div>
        `).join("");
    };

    const renderMediaSummary = (summary) => {
        if (!mediaSummaryGrid) {
            return;
        }
        const totalLabel = summary.has_unknown_total
            ? `${formatBytes(summary.bytes_done)} / ${formatPartialTotal(summary.bytes_total)}`
            : `${formatBytes(summary.bytes_done)} / ${formatBytes(summary.bytes_total)}`;

        mediaSummaryGrid.innerHTML = [
            ["Total Jobs", summary.total_jobs],
            ["Queued", summary.queued_jobs],
            ["Active", summary.active_jobs],
            ["Completed", summary.completed_jobs],
            ["Failed", summary.failed_jobs],
            ["Remux Speed", formatSpeed(summary.throughput_bps)],
        ].map(([label, value]) => `
            <div class="stat-tile">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value)}</strong>
                <small class="subtle">${escapeHtml(totalLabel)}</small>
            </div>
        `).join("");
    };

    const renderArchiveSummary = (summary) => {
        if (!archiveSummaryGrid) {
            return;
        }
        const totalLabel = summary.has_unknown_total
            ? `${formatBytes(summary.bytes_done)} / ${formatPartialTotal(summary.bytes_total)}`
            : `${formatBytes(summary.bytes_done)} / ${formatBytes(summary.bytes_total)}`;

        archiveSummaryGrid.innerHTML = [
            ["Total Jobs", summary.total_jobs],
            ["Queued", summary.queued_jobs],
            ["Active", summary.active_jobs],
            ["Completed", summary.completed_jobs],
            ["Failed", summary.failed_jobs],
            ["Extract Speed", formatSpeed(summary.throughput_bps)],
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
        const progressLabel = formatPercentLabel(percent);
        const statusLabel = isActiveStatus(job.status) && progressLabel
            ? `Transferring ${progressLabel}`
            : job.status_label;
        const lastMessage = String(job.transfer.last_message || "");
        const visibleMessage = isTransferDebugMessage(lastMessage)
            ? ""
            : (lastMessage || "Waiting for worker output.");

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
                    <span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(statusLabel)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(job.destination_display)}</span>
                    <span>${escapeHtml(formatBytes(job.transfer.bytes_done))}</span>
                    <span>${escapeHtml(job.transfer.bytes_total ? formatBytes(job.transfer.bytes_total) : "Total unknown")}</span>
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
                ${visibleMessage ? `
                    <div class="metric-row">
                        <span>${escapeHtml(visibleMessage)}</span>
                    </div>
                ` : ""}
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
                    <span>${escapeHtml(batch.has_unknown_total ? formatPartialTotal(batch.bytes_total) : formatBytes(batch.bytes_total))}</span>
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
                <div class="job-list">${batch.jobs.map(renderJob).join("")}</div>
            </section>
        `;
    };

    const renderVerificationBadges = (verification) => {
        const badges = [];
        if (verification?.dolby_vision) {
            badges.push('<span class="status-pill completed">Dolby Vision</span>');
        }
        if (verification?.dolby_atmos) {
            badges.push('<span class="status-pill completed">Dolby Atmos</span>');
        }
        if (verification?.video_codec) {
            badges.push(`<span class="status-pill">${escapeHtml(verification.video_codec)}</span>`);
        }
        if (verification?.audio_codec) {
            badges.push(`<span class="status-pill">${escapeHtml(verification.audio_codec)}</span>`);
        }
        return badges.join("");
    };

    const renderMediaJob = (job) => {
        const percent = progressPercent(job.transfer);
        const progressBar = buildProgressBar(job.status, percent, isMediaActiveStatus(job.status) && percent === null);
        const speedLabel = isStoppedStatus(job.status) ? "Stopped" : formatSpeed(job.transfer.speed_bps);
        const etaLabel = isStoppedStatus(job.status) ? "Stopped" : formatEta(job.transfer.eta_seconds);
        const titleLabel = job.title_id === null || job.title_id === undefined
            ? "Auto-select pending"
            : `Title ${job.title_id}${job.title_name ? ` - ${job.title_name}` : ""}`;
        const outputLabel = job.output_file_path || job.output_destination_display;
        const visibleMessage = String(job.transfer.last_message || "") || "Waiting for worker output.";

        const actions = [
            job.can_cancel ? `
                <form action="/media-jobs/${encodeURIComponent(job.id)}/cancel" method="post">
                    <button type="submit">Cancel</button>
                </form>
            ` : "",
            job.can_retry ? `
                <form action="/media-jobs/${encodeURIComponent(job.id)}/retry" method="post">
                    <button type="submit">Retry</button>
                </form>
            ` : "",
        ].join("");

        return `
            <article class="job-card media-job-card">
                <div class="job-header">
                    <div>
                        <strong>${escapeHtml(job.source_display_name)}</strong>
                        <p class="job-url">${escapeHtml(job.source_display)}</p>
                    </div>
                    <span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(job.status_label)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(titleLabel)}</span>
                    <span>${escapeHtml(outputLabel)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(formatBytes(job.transfer.bytes_done))}</span>
                    <span>${escapeHtml(job.transfer.bytes_total ? formatBytes(job.transfer.bytes_total) : "Total unknown")}</span>
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
                <div class="metric-row media-verification-row">
                    ${renderVerificationBadges(job.verification) || '<span class="subtle">Verification pending</span>'}
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(visibleMessage)}</span>
                </div>
                ${job.error ? `<div class="flash flash-error">${escapeHtml(job.error)}</div>` : ""}
                <div class="job-actions">${actions}</div>
            </article>
        `;
    };

    const renderArchiveJob = (job) => {
        const percent = progressPercent(job.transfer);
        const progressBar = buildProgressBar(job.status, percent, isActiveStatus(job.status) && percent === null);
        const speedLabel = isStoppedStatus(job.status) ? "Stopped" : formatSpeed(job.transfer.speed_bps);
        const etaLabel = isStoppedStatus(job.status) ? "Stopped" : formatEta(job.transfer.eta_seconds);
        const visibleMessage = String(job.transfer.last_message || "") || "Waiting for worker output.";

        return `
            <article class="job-card media-job-card">
                <div class="job-header">
                    <div>
                        <strong>${escapeHtml(job.archive_display_name)}</strong>
                        <p class="job-url">${escapeHtml(job.archive_relative_path)}</p>
                    </div>
                    <span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(job.status_label)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(job.archive_type.toUpperCase())}</span>
                    <span>${escapeHtml(job.target_display || job.target_relative_path || job.target_path)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(formatBytes(job.transfer.bytes_done))}</span>
                    <span>${escapeHtml(job.transfer.bytes_total ? formatBytes(job.transfer.bytes_total) : "Total unknown")}</span>
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
                <div class="metric-row">
                    <span>${escapeHtml(visibleMessage)}</span>
                </div>
                ${job.error ? `<div class="flash flash-error">${escapeHtml(job.error)}</div>` : ""}
            </article>
        `;
    };

    const renderDashboard = (payload) => {
        if (backendLabel) {
            backendLabel.textContent = payload.backend.label;
        }
        if (backendNote) {
            backendNote.textContent = payload.backend.reason || "";
        }
        if (mediaBackendLabel && payload.media?.backend) {
            mediaBackendLabel.textContent = payload.media.backend.label;
        }
        if (mediaBackendNote && payload.media?.backend) {
            mediaBackendNote.textContent = payload.media.backend.reason || "";
        }
        if (updatedLabel) {
            updatedLabel.textContent = `Updated ${formatTimestamp(payload.updated_at)}`;
        }
        if (bulkPauseToggleButton && payload.bulk_pause_toggle) {
            bulkPauseToggleButton.textContent = payload.bulk_pause_toggle.label;
            bulkPauseToggleButton.disabled = !payload.bulk_pause_toggle.available;
        }

        renderSummary(payload.summary);
        renderArchiveSummary(payload.archives.summary);
        renderMediaSummary(payload.media.summary);
        if (archiveJobList) {
            archiveJobList.innerHTML = payload.archives.jobs.length
                ? payload.archives.jobs.map(renderArchiveJob).join("")
                : '<div class="stat-tile"><span>No archive jobs yet</span><strong>Queue is empty</strong><small class="subtle">Use the explorer to queue archive extraction jobs.</small></div>';
        }
        if (mediaJobList) {
            mediaJobList.innerHTML = payload.media.jobs.length
                ? payload.media.jobs.map(renderMediaJob).join("")
                : '<div class="stat-tile"><span>No Blu-ray jobs yet</span><strong>Queue is empty</strong><small class="subtle">Use the explorer to queue Blu-ray remux jobs.</small></div>';
        }
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
    restoreScrollPosition();
    window.setInterval(poll, pollMs);
})();
