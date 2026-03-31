(() => {
    const bulkForm = document.getElementById("explorer-bulk-form");
    if (!bulkForm) {
        return;
    }

    const selectAll = document.getElementById("select-all-entries");
    const selectionCount = document.getElementById("selection-count");
    const entryCheckboxes = Array.from(bulkForm.querySelectorAll(".entry-select"));
    const selectionButtons = Array.from(bulkForm.querySelectorAll("[data-selection-action]"));
    const moveButton = bulkForm.querySelector("[data-move-action]");
    const saveTargetButton = bulkForm.querySelector("[data-save-target]");
    const compileButton = bulkForm.querySelector("[data-compile-action]");
    const saveDestinationButton = bulkForm.querySelector("[data-save-destination]");
    const moveTargetInput = document.getElementById("move-target-path");
    const savedMoveTargetSelect = document.getElementById("saved-move-target");
    const savedMoveTargetPreview = document.getElementById("saved-move-target-preview");
    const autoSortCheckbox = document.getElementById("auto-sort-extracted-videos");
    const autoDeleteCheckbox = document.getElementById("auto-delete-source-archives");
    const compileDestinationSelect = document.getElementById("compile-destination");
    const compileDestinationInput = document.getElementById("compile-destination-path");
    const compileDestinationPreview = document.getElementById("compile-destination-preview");
    const archiveJobList = document.getElementById("explorer-archive-job-list");
    const archiveSummary = document.getElementById("explorer-archive-summary");
    const initialArchivePayload = document.getElementById("initial-explorer-archives");
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

    const progressPercent = (transfer) => {
        if (transfer?.bytes_total) {
            return clampPercent((transfer.bytes_done / transfer.bytes_total) * 100);
        }
        return clampPercent(transfer?.percent);
    };

    const isStoppedStatus = (status) => status === "failed" || status === "canceled";
    const isArchiveActiveStatus = (status) => status === "probing" || status === "extracting" || status === "sorting" || status === "cleaning";

    const buildProgressBar = (status, percent, indeterminate = false) => {
        let trackClass = "progress-track";
        let fillClass = "progress-fill";

        if (isStoppedStatus(status)) {
            trackClass += " failed";
            fillClass += " failed";
            const width = percent === null ? "100%" : `${percent.toFixed(1)}%`;
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

    const pathWithinScope = (scope, relativePath) => {
        const normalizedScope = String(scope || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
        const normalizedRelativePath = String(relativePath || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
        if (!normalizedScope) {
            return true;
        }
        return normalizedRelativePath === normalizedScope || normalizedRelativePath.startsWith(`${normalizedScope}/`);
    };

    const currentArchiveRoot = archiveJobList?.dataset.root || "";
    const currentArchivePath = archiveJobList?.dataset.currentPath || "";
    const currentArchiveSort = archiveJobList?.dataset.sort || "name";

    const filterRelevantArchiveJobs = (jobs) => jobs.filter((job) =>
        job.root_key === currentArchiveRoot && (
            pathWithinScope(currentArchivePath, job.archive_relative_path) ||
            pathWithinScope(currentArchivePath, job.target_relative_path)
        )
    );

    const renderArchiveJob = (job) => {
        const percent = progressPercent(job.transfer);
        const progressBar = buildProgressBar(job.status, percent, isArchiveActiveStatus(job.status) && percent === null);
        const speedLabel = isStoppedStatus(job.status) ? "Stopped" : formatSpeed(job.transfer.speed_bps);
        const etaLabel = isStoppedStatus(job.status) ? "Stopped" : formatEta(job.transfer.eta_seconds);
        const visibleMessage = String(job.transfer?.last_message || "") || "Waiting for worker output.";
        const sortSummary = job.sort_summary || {};
        const autoDeleteSummary = job.auto_delete_summary || {};
        const sortSummaryParts = [
            sortSummary.moved_movies ? `${sortSummary.moved_movies} to Movies` : "",
            sortSummary.moved_tv ? `${sortSummary.moved_tv} to TvShows` : "",
            sortSummary.skipped_unclear ? `${sortSummary.skipped_unclear} unclear` : "",
            sortSummary.skipped_conflict ? `${sortSummary.skipped_conflict} conflict` : "",
            sortSummary.failed ? `${sortSummary.failed} failed` : "",
        ].filter(Boolean);
        const deletedCount = Number(autoDeleteSummary.deleted_count ?? autoDeleteSummary.deleted_paths?.length ?? 0);
        const failedDeleteCount = Number(autoDeleteSummary.failed_count ?? autoDeleteSummary.failed_paths?.length ?? 0);
        const autoDeleteParts = [
            deletedCount ? `deleted ${deletedCount}` : "",
            failedDeleteCount ? `${failedDeleteCount} failed` : "",
            autoDeleteSummary.kept_reason === "no_videos_moved" ? "kept source archives" : "",
        ].filter(Boolean);
        const actions = job.can_cancel ? `
            <form action="/archive-jobs/${encodeURIComponent(job.id)}/cancel" method="post">
                <input type="hidden" name="root" value="${escapeHtml(currentArchiveRoot)}">
                <input type="hidden" name="current_path" value="${escapeHtml(currentArchivePath)}">
                <input type="hidden" name="sort" value="${escapeHtml(currentArchiveSort)}">
                <button type="submit" class="danger-button compact-button">Cancel</button>
            </form>
        ` : "";

        return `
            <article class="job-card media-job-card">
                <div class="job-header">
                    <div>
                        <strong>${escapeHtml(job.archive_display_name)}</strong>
                        <p class="job-url">${escapeHtml(job.archive_relative_path)}</p>
                    </div>
                    <span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(job.status_label || job.status)}</span>
                </div>
                <div class="metric-row">
                    <span>${escapeHtml(String(job.archive_type || "").toUpperCase())}</span>
                    <span>${escapeHtml(job.target_display || job.target_relative_path || job.target_path)}</span>
                </div>
                ${job.auto_sort_enabled ? `
                    <div class="metric-row">
                        <span>Auto-sort</span>
                        <span>${escapeHtml(sortSummaryParts.join(" | ") || "Movies / TvShows favorites")}</span>
                    </div>
                ` : ""}
                ${job.auto_delete_enabled ? `
                    <div class="metric-row">
                        <span>Auto-delete</span>
                        <span>${escapeHtml(autoDeleteParts.join(" | ") || "Waiting for auto-sort result")}</span>
                    </div>
                ` : ""}
                <div class="metric-row">
                    <span>${escapeHtml(formatBytes(job.transfer?.bytes_done))}</span>
                    <span>${escapeHtml(job.transfer?.bytes_total ? formatBytes(job.transfer.bytes_total) : "Total unknown")}</span>
                    <span>${escapeHtml(speedLabel)}</span>
                    <span>${escapeHtml(etaLabel)}</span>
                </div>
                ${progressBar}
                <div class="metric-row">
                    <span>${escapeHtml(visibleMessage)}</span>
                </div>
                ${job.error ? `<div class="flash flash-error">${escapeHtml(job.error)}</div>` : ""}
                ${actions ? `<div class="job-actions">${actions}</div>` : ""}
            </article>
        `;
    };

    const renderArchiveJobs = (jobs) => {
        if (!archiveJobList || !archiveSummary) {
            return;
        }
        const activeJobs = jobs.filter((job) => isArchiveActiveStatus(job.status)).length;
        if (jobs.length === 0) {
            archiveSummary.textContent = "No related archive jobs for this location yet.";
            archiveJobList.innerHTML = '<div class="stat-tile"><span>No archive jobs here</span><strong>Nothing to monitor</strong><small class="subtle">Queue an archive extraction from this folder to see live status here.</small></div>';
            return;
        }
        archiveSummary.textContent = `${jobs.length} related archive job(s), ${activeJobs} active.`;
        archiveJobList.innerHTML = jobs.map(renderArchiveJob).join("");
    };

    const pollArchiveJobs = async () => {
        if (!archiveJobList) {
            return;
        }
        try {
            const response = await fetch("/api/jobs", {
                headers: { "Accept": "application/json" },
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            renderArchiveJobs(filterRelevantArchiveJobs(payload?.archives?.jobs || []));
        } catch (error) {
            console.debug("Explorer archive polling failed", error);
        }
    };

    const updateSelectionUi = () => {
        const selectedCount = entryCheckboxes.filter((checkbox) => checkbox.checked).length;
        const totalCount = entryCheckboxes.length;
        const hasMoveTarget = Boolean(moveTargetInput?.value.trim());
        const hasDestinationPath = Boolean(compileDestinationInput?.value.trim());
        const autoSortEnabled = Boolean(autoSortCheckbox?.checked);

        if (selectionCount) {
            if (selectedCount === 0) {
                selectionCount.textContent = "No items selected";
            } else if (selectedCount === 1) {
                selectionCount.textContent = "1 item selected";
            } else {
                selectionCount.textContent = `${selectedCount} items selected`;
            }
        }

        for (const button of selectionButtons) {
            button.disabled = selectedCount === 0;
        }

        if (moveButton) {
            moveButton.disabled = selectedCount === 0 || !hasMoveTarget;
        }

        if (saveTargetButton) {
            saveTargetButton.disabled = !hasMoveTarget;
        }

        if (saveDestinationButton) {
            saveDestinationButton.disabled = !hasDestinationPath;
        }

        if (compileButton) {
            const compileLocked = compileButton.dataset.locked === "true";
            compileButton.disabled = compileLocked || selectedCount === 0;
        }

        if (autoDeleteCheckbox) {
            autoDeleteCheckbox.disabled = !autoSortEnabled;
            if (!autoSortEnabled && autoDeleteCheckbox.checked) {
                autoDeleteCheckbox.checked = false;
            }
        }

        if (savedMoveTargetPreview && savedMoveTargetSelect) {
            const selectedOption = savedMoveTargetSelect.selectedOptions[0];
            const selectedPath = selectedOption?.dataset.path || selectedOption?.value || "";
            savedMoveTargetPreview.textContent = selectedPath || "Choose a saved target to copy its path here.";
        }

        if (compileDestinationPreview && compileDestinationSelect) {
            const selectedOption = compileDestinationSelect.selectedOptions[0];
            const basePath = selectedOption?.dataset.path || "";
            const customPath = compileDestinationInput?.value.trim();
            if (customPath) {
                compileDestinationPreview.textContent = `Base path: ${basePath}. Custom path: ${customPath}`;
            } else if (basePath) {
                compileDestinationPreview.textContent = `Base path: ${basePath}`;
            } else {
                compileDestinationPreview.textContent = "Choose an output destination.";
            }
        }

        if (!selectAll) {
            return;
        }

        selectAll.checked = totalCount > 0 && selectedCount === totalCount;
        selectAll.indeterminate = selectedCount > 0 && selectedCount < totalCount;
    };

    if (selectAll) {
        selectAll.addEventListener("change", () => {
            const nextState = selectAll.checked;
            for (const checkbox of entryCheckboxes) {
                checkbox.checked = nextState;
            }
            updateSelectionUi();
        });
    }

    for (const checkbox of entryCheckboxes) {
        checkbox.addEventListener("change", updateSelectionUi);
    }

    if (moveTargetInput) {
        moveTargetInput.addEventListener("input", updateSelectionUi);
    }

    if (compileDestinationInput) {
        compileDestinationInput.addEventListener("input", updateSelectionUi);
    }

    if (compileDestinationSelect) {
        compileDestinationSelect.addEventListener("change", updateSelectionUi);
    }

    if (autoSortCheckbox) {
        autoSortCheckbox.addEventListener("change", () => {
            updateSelectionUi();
        });
    }

    if (autoDeleteCheckbox) {
        autoDeleteCheckbox.addEventListener("change", () => {
            updateSelectionUi();
        });
    }

    if (savedMoveTargetSelect && moveTargetInput) {
        savedMoveTargetSelect.addEventListener("change", () => {
            const selectedValue = savedMoveTargetSelect.value;
            if (selectedValue) {
                moveTargetInput.value = selectedValue;
            }
            updateSelectionUi();
        });
    }

    if (compileButton && compileButton.disabled) {
        compileButton.dataset.locked = "true";
    }

    if (archiveJobList && initialArchivePayload) {
        try {
            const initialJobs = JSON.parse(initialArchivePayload.textContent || "[]");
            renderArchiveJobs(initialJobs);
        } catch (error) {
            console.debug("Explorer archive payload could not be parsed", error);
            renderArchiveJobs([]);
        }
        window.setInterval(pollArchiveJobs, pollMs);
    }

    updateSelectionUi();
})();
