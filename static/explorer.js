(() => {
    const onReady = (callback) => {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", callback, { once: true });
            return;
        }
        callback();
    };

    onReady(() => {
        const app = document.getElementById("explorer-app");
        if (!app) {
            return;
        }

        const refs = {
            initialPayload: document.getElementById("initial-explorer-payload"),
            rootSelect: document.getElementById("explorer-root-select"),
            title: document.getElementById("explorer-title"),
            breadcrumbs: document.getElementById("explorer-breadcrumbs"),
            filter: document.getElementById("explorer-filter"),
            table: document.getElementById("explorer-file-table"),
            body: document.getElementById("explorer-file-body"),
            empty: document.getElementById("explorer-empty-state"),
            selectAll: document.getElementById("explorer-select-all"),
            folderTitle: document.getElementById("explorer-folder-title"),
            folderSummary: document.getElementById("explorer-folder-summary"),
            selectionCount: document.getElementById("explorer-selection-count"),
            selectionList: document.getElementById("explorer-selection-list"),
            currentPath: document.getElementById("explorer-current-path"),
            currentRoot: document.getElementById("explorer-current-root"),
            tree: document.getElementById("explorer-tree"),
            moveTarget: document.getElementById("explorer-move-target"),
            moveFavorite: document.getElementById("explorer-move-favorite"),
            remuxDestination: document.getElementById("explorer-remux-destination"),
            remuxPath: document.getElementById("explorer-remux-path"),
            autoSort: document.getElementById("explorer-auto-sort"),
            autoDelete: document.getElementById("explorer-auto-delete"),
            mediaStatus: document.getElementById("explorer-media-status"),
            archiveList: document.getElementById("explorer-archive-job-list"),
            archiveSummary: document.getElementById("explorer-archive-summary"),
            modal: document.getElementById("explorer-modal"),
            modalTitle: document.getElementById("explorer-modal-title"),
            modalBody: document.getElementById("explorer-modal-body"),
            modalConfirm: document.getElementById("explorer-modal-confirm"),
            contextMenu: document.getElementById("explorer-context-menu"),
            toastStack: document.getElementById("explorer-toast-stack"),
        };

        const state = {
            root: app.dataset.root || "",
            path: app.dataset.path || "",
            sort: app.dataset.sort || "name",
            order: app.dataset.order || "asc",
            roots: [],
            entries: [],
            filteredEntries: [],
            selected: new Set(),
            lastSelectedIndex: null,
            focusedIndex: -1,
            treeCache: new Map(),
            expandedTree: new Set([""]),
            moveFavorites: [],
            mediaBackend: {},
            archiveSettings: {},
            archiveJobs: [],
            contextTargetPath: "",
            draggedPaths: [],
        };

        const escapeHtml = (value) => String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");

        const normalizePath = (value) => String(value || "").replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");

        const pathWithinScope = (scope, relativePath) => {
            const normalizedScope = normalizePath(scope);
            const normalizedRelativePath = normalizePath(relativePath);
            if (!normalizedScope) {
                return true;
            }
            return normalizedRelativePath === normalizedScope || normalizedRelativePath.startsWith(`${normalizedScope}/`);
        };

        const formatBytes = (value) => {
            if (value === null || value === undefined) {
                return "-";
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

        const formatDate = (value) => {
            if (!value) {
                return "-";
            }
            const parsed = new Date(value);
            if (Number.isNaN(parsed.getTime())) {
                return value;
            }
            return parsed.toLocaleString();
        };

        const typeLabel = (entry) => {
            if (entry.entry_type === "bluray") {
                return "Blu-ray";
            }
            if (entry.entry_type === "folder") {
                return "Folder";
            }
            if (entry.archive_type) {
                return `${String(entry.archive_type).toUpperCase()} archive`;
            }
            return entry.extension ? `${entry.extension.toUpperCase()} file` : "File";
        };

        const entryIcon = (entry) => {
            if (entry.entry_type === "folder") {
                return "DIR";
            }
            if (entry.entry_type === "bluray") {
                return "BD";
            }
            if (entry.archive_type) {
                return "ARC";
            }
            return "FILE";
        };

        const apiGet = async (url, params = {}) => {
            const query = new URLSearchParams(params);
            const response = await fetch(`${url}?${query.toString()}`, {
                headers: { "Accept": "application/json" },
                cache: "no-store",
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || payload.ok === false) {
                const error = new Error(payload.error || "Explorer request failed.");
                error.payload = payload;
                error.status = response.status;
                throw error;
            }
            return payload;
        };

        const apiPost = async (url, body = {}) => {
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(body),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || payload.ok === false) {
                const error = new Error(payload.error || "Explorer request failed.");
                error.payload = payload;
                error.status = response.status;
                throw error;
            }
            return payload;
        };

        const showToast = (message, kind = "success") => {
            if (!refs.toastStack) {
                return;
            }
            const node = document.createElement("div");
            node.className = `explorer-toast ${kind}`;
            node.textContent = message;
            refs.toastStack.appendChild(node);
            window.setTimeout(() => node.remove(), 4500);
        };

        const selectedEntries = () => state.entries.filter((entry) => state.selected.has(entry.relative_path));

        const selectedPaths = () => Array.from(state.selected);

        const currentRequest = (extra = {}) => ({
            root: state.root,
            current_path: state.path,
            sort: state.sort,
            order: state.order,
            ...extra,
        });

        const setUrlState = () => {
            const url = new URL(window.location.href);
            url.searchParams.set("root", state.root);
            if (state.path) {
                url.searchParams.set("path", state.path);
            } else {
                url.searchParams.delete("path");
            }
            url.searchParams.set("sort", state.sort);
            url.searchParams.set("order", state.order);
            window.history.replaceState({}, "", url.toString());
        };

        const applyPayload = (payload, { preserveSelection = false } = {}) => {
            const explorer = payload.explorer || {};
            state.root = explorer.root?.key || state.root;
            state.path = explorer.current_path || "";
            state.sort = explorer.sort || state.sort;
            state.order = explorer.order || state.order;
            state.roots = payload.roots || state.roots;
            state.entries = explorer.entries || [];
            state.moveFavorites = payload.move_favorites || state.moveFavorites;
            state.mediaBackend = payload.media_backend || state.mediaBackend;
            state.archiveSettings = payload.archive_automation_settings || state.archiveSettings;
            state.archiveJobs = payload.archive_jobs || state.archiveJobs;

            if (!preserveSelection) {
                state.selected.clear();
                state.lastSelectedIndex = null;
                state.focusedIndex = -1;
            } else {
                const existing = new Set(state.entries.map((entry) => entry.relative_path));
                state.selected = new Set(Array.from(state.selected).filter((path) => existing.has(path)));
            }

            render();
            setUrlState();
            const treePath = state.path;
            ensureTreeLoaded(treePath).catch((error) => {
                if (treeKey(treePath) === treeKey(state.path)) {
                    showToast(error.message, "error");
                }
            });
        };

        const loadDirectory = async ({ root = state.root, path = state.path, sort = state.sort, order = state.order, preserveSelection = false } = {}) => {
            const payload = await apiGet("/api/explorer", { root, path, sort, order });
            applyPayload(payload, { preserveSelection });
        };

        const ensureTreeLoaded = async (path) => {
            const normalized = normalizePath(path);
            if (state.treeCache.has(normalized)) {
                renderTree();
                return;
            }
            const payload = await apiGet("/api/explorer/tree", { root: state.root, path: normalized });
            state.treeCache.set(normalized, payload.tree || { directories: [] });
            renderTree();
        };

        const render = () => {
            renderRootControls();
            renderBreadcrumbs();
            renderFiles();
            renderDetails();
            renderArchiveJobs(state.archiveJobs);
        };

        const renderRootControls = () => {
            const root = state.roots.find((item) => item.key === state.root);
            if (refs.title) {
                refs.title.textContent = root?.label || "File Explorer";
            }
            if (refs.rootSelect) {
                refs.rootSelect.innerHTML = state.roots.map((item) => (
                    `<option value="${escapeHtml(item.key)}" ${item.key === state.root ? "selected" : ""}>${escapeHtml(item.label)}</option>`
                )).join("");
            }
            if (refs.remuxDestination) {
                refs.remuxDestination.innerHTML = state.roots.map((item) => (
                    `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`
                )).join("");
            }
            if (refs.moveFavorite) {
                refs.moveFavorite.innerHTML = [
                    '<option value="">Saved move target</option>',
                    ...state.moveFavorites.map((item) => (
                        `<option value="${escapeHtml(item.path)}">${escapeHtml(item.label)} - ${escapeHtml(item.path)}</option>`
                    )),
                ].join("");
            }
            if (refs.autoSort) {
                refs.autoSort.checked = Boolean(state.archiveSettings.auto_sort_enabled);
            }
            if (refs.autoDelete) {
                refs.autoDelete.checked = Boolean(state.archiveSettings.auto_delete_enabled);
                refs.autoDelete.disabled = !Boolean(state.archiveSettings.auto_sort_enabled);
            }
            if (refs.mediaStatus) {
                refs.mediaStatus.textContent = state.mediaBackend.available
                    ? "Blu-ray remux is available."
                    : (state.mediaBackend.reason || "Blu-ray remux backend unavailable.");
            }
        };

        const renderBreadcrumbs = () => {
            if (!refs.breadcrumbs) {
                return;
            }
            const root = state.roots.find((item) => item.key === state.root);
            const crumbs = [{ label: root?.label || "Root", path: "" }];
            if (state.path) {
                const parts = state.path.split("/").filter(Boolean);
                const running = [];
                for (const part of parts) {
                    running.push(part);
                    crumbs.push({ label: part, path: running.join("/") });
                }
            }
            refs.breadcrumbs.innerHTML = crumbs.map((crumb, index) => `
                <button type="button" data-path="${escapeHtml(crumb.path)}" class="${index === crumbs.length - 1 ? "active" : ""}">
                    ${escapeHtml(crumb.label)}
                </button>
            `).join("");
        };

        const filteredEntries = () => {
            const query = String(refs.filter?.value || "").trim().toLowerCase();
            if (!query) {
                return state.entries;
            }
            return state.entries.filter((entry) => [
                entry.name,
                entry.relative_path,
                entry.entry_type,
                entry.extension,
                entry.archive_type,
            ].some((value) => String(value || "").toLowerCase().includes(query)));
        };

        const renderFiles = () => {
            state.filteredEntries = filteredEntries();
            if (refs.folderTitle) {
                refs.folderTitle.textContent = state.path || "Root";
            }
            if (refs.folderSummary) {
                refs.folderSummary.textContent = `${state.filteredEntries.length} visible of ${state.entries.length} item(s)`;
            }

            const visiblePaths = new Set(state.filteredEntries.map((entry) => entry.relative_path));
            for (const path of Array.from(state.selected)) {
                if (!visiblePaths.has(path) && !state.entries.some((entry) => entry.relative_path === path)) {
                    state.selected.delete(path);
                }
            }

            refs.body.innerHTML = state.filteredEntries.map((entry, index) => {
                const selected = state.selected.has(entry.relative_path);
                const active = index === state.focusedIndex;
                return `
                    <tr
                        tabindex="0"
                        draggable="true"
                        data-index="${index}"
                        data-path="${escapeHtml(entry.relative_path)}"
                        class="${selected ? "selected" : ""} ${active ? "focused" : ""}"
                    >
                        <td class="selection-cell">
                            <input type="checkbox" aria-label="Select ${escapeHtml(entry.name)}" ${selected ? "checked" : ""}>
                        </td>
                        <td>
                            <button type="button" class="explorer-file-name" data-open>
                                <span class="entry-icon">${escapeHtml(entryIcon(entry))}</span>
                                <span class="explorer-file-label">${escapeHtml(entry.name)}</span>
                            </button>
                        </td>
                        <td>${escapeHtml(typeLabel(entry))}</td>
                        <td>${entry.is_dir ? "-" : escapeHtml(formatBytes(entry.size))}</td>
                        <td>${escapeHtml(formatDate(entry.modified_at))}</td>
                    </tr>
                `;
            }).join("");

            if (refs.empty) {
                refs.empty.hidden = state.filteredEntries.length !== 0;
            }
            if (refs.selectAll) {
                const selectedVisible = state.filteredEntries.filter((entry) => state.selected.has(entry.relative_path)).length;
                refs.selectAll.checked = state.filteredEntries.length > 0 && selectedVisible === state.filteredEntries.length;
                refs.selectAll.indeterminate = selectedVisible > 0 && selectedVisible < state.filteredEntries.length;
            }
            updateActionState();
        };

        const renderDetails = () => {
            const selected = selectedEntries();
            if (refs.selectionCount) {
                refs.selectionCount.textContent = `${selected.length} selected`;
            }
            if (refs.currentPath) {
                refs.currentPath.textContent = state.path || "Root";
            }
            if (refs.currentRoot) {
                const root = state.roots.find((item) => item.key === state.root);
                refs.currentRoot.textContent = root?.path || "";
            }
            if (refs.selectionList) {
                refs.selectionList.innerHTML = selected.length
                    ? selected.slice(0, 12).map((entry) => `<span>${escapeHtml(entry.name)}</span>`).join("")
                    : '<p class="subtle">No items selected.</p>';
            }
        };

        const updateActionState = () => {
            const selected = selectedEntries();
            const oneSelected = selected.length === 1;
            const hasSelected = selected.length > 0;
            const hasArchive = selected.some((entry) => entry.can_extract);
            const hasBluray = selected.some((entry) => entry.can_compile_bluray);
            const mediaAvailable = Boolean(state.mediaBackend.available);

            document.querySelectorAll("[data-action='rename']").forEach((button) => {
                button.disabled = !oneSelected;
            });
            document.querySelectorAll("[data-action='move']").forEach((button) => {
                button.disabled = !hasSelected;
            });
            document.querySelectorAll("[data-action='delete']").forEach((button) => {
                button.disabled = !hasSelected;
            });
            document.querySelectorAll("[data-action='extract']").forEach((button) => {
                button.disabled = !hasArchive;
            });
            document.querySelectorAll("[data-action='remux']").forEach((button) => {
                button.disabled = !hasBluray || !mediaAvailable;
            });
        };

        const treeKey = (path) => normalizePath(path);

        const renderTree = () => {
            if (!refs.tree) {
                return;
            }
            const rootItems = state.roots.map((root) => {
                const active = root.key === state.root;
                const childTree = active ? renderTreeChildren("") : "";
                return `
                    <div class="explorer-tree-root">
                        <button type="button" class="explorer-tree-row ${active ? "active" : ""}" data-tree-root="${escapeHtml(root.key)}" data-tree-path="">
                            <span>${escapeHtml(root.label)}</span>
                        </button>
                        ${childTree}
                    </div>
                `;
            }).join("");
            refs.tree.innerHTML = rootItems || '<p class="subtle">No roots configured.</p>';
        };

        const renderTreeChildren = (path, depth = 0) => {
            const key = treeKey(path);
            const node = state.treeCache.get(key);
            if (!node || !state.expandedTree.has(key)) {
                return "";
            }
            const directories = node.directories || [];
            if (!directories.length) {
                return "";
            }
            return `
                <div class="explorer-tree-children" style="--tree-depth:${depth + 1}">
                    ${directories.map((directory) => {
                        const childKey = treeKey(directory.relative_path);
                        const expanded = state.expandedTree.has(childKey);
                        const active = normalizePath(directory.relative_path) === normalizePath(state.path);
                        return `
                            <div class="explorer-tree-node">
                                <div class="explorer-tree-line">
                                    <button type="button" class="explorer-tree-toggle" data-tree-toggle="${escapeHtml(directory.relative_path)}" ${directory.has_children ? "" : "disabled"}>
                                        ${directory.has_children ? (expanded ? "-" : "+") : ""}
                                    </button>
                                    <button
                                        type="button"
                                        class="explorer-tree-row ${active ? "active" : ""}"
                                        data-tree-root="${escapeHtml(state.root)}"
                                        data-tree-path="${escapeHtml(directory.relative_path)}"
                                    >
                                        <span>${escapeHtml(directory.name)}</span>
                                    </button>
                                </div>
                                ${renderTreeChildren(directory.relative_path, depth + 1)}
                            </div>
                        `;
                    }).join("")}
                </div>
            `;
        };

        const setSelectedFromEvent = (entry, index, event) => {
            if (!entry) {
                return;
            }
            if (event.shiftKey && state.lastSelectedIndex !== null) {
                const [start, end] = [state.lastSelectedIndex, index].sort((a, b) => a - b);
                for (let i = start; i <= end; i += 1) {
                    const visible = state.filteredEntries[i];
                    if (visible) {
                        state.selected.add(visible.relative_path);
                    }
                }
            } else if (event.metaKey || event.ctrlKey) {
                if (state.selected.has(entry.relative_path)) {
                    state.selected.delete(entry.relative_path);
                } else {
                    state.selected.add(entry.relative_path);
                }
                state.lastSelectedIndex = index;
            } else {
                state.selected.clear();
                state.selected.add(entry.relative_path);
                state.lastSelectedIndex = index;
            }
            state.focusedIndex = index;
            renderFiles();
            renderDetails();
        };

        const openEntry = (entry) => {
            if (!entry || !entry.is_dir) {
                return;
            }
            state.expandedTree.add(treeKey(entry.relative_path));
            const targetPath = entry.relative_path;
            loadDirectory({ path: targetPath }).catch((error) => {
                const targetStillVisible = state.entries.some((item) => item.relative_path === targetPath);
                if (treeKey(state.path) === treeKey(targetPath) || targetStillVisible) {
                    showToast(error.message, "error");
                }
            });
        };

        const showModal = ({ title, body, confirmLabel = "Confirm", danger = false, onConfirm }) => {
            refs.modalTitle.textContent = title;
            refs.modalBody.innerHTML = body;
            refs.modalConfirm.textContent = confirmLabel;
            refs.modalConfirm.className = danger ? "danger-button" : "";
            refs.modal.hidden = false;
            refs.modal.setAttribute("aria-hidden", "false");
            refs.modalConfirm.onclick = async () => {
                try {
                    await onConfirm();
                    closeModal();
                } catch (error) {
                    showToast(error.message, "error");
                }
            };
            const firstInput = refs.modalBody.querySelector("input, select, textarea, button");
            window.setTimeout(() => firstInput?.focus(), 0);
        };

        const closeModal = () => {
            refs.modal.hidden = true;
            refs.modal.setAttribute("aria-hidden", "true");
            refs.modalConfirm.onclick = null;
        };

        const createFolder = () => {
            showModal({
                title: "New folder",
                body: '<label class="explorer-modal-field">Folder name<input id="modal-folder-name" type="text" autocomplete="off"></label>',
                confirmLabel: "Create",
                onConfirm: async () => {
                    const name = document.getElementById("modal-folder-name").value;
                    const payload = await apiPost("/api/explorer/folders", currentRequest({ name }));
                    applyPayload(payload);
                    state.treeCache.delete(treeKey(state.path));
                    await ensureTreeLoaded(state.path);
                    showToast(`Created ${name}.`);
                },
            });
        };

        const beginInlineRename = (entry = selectedEntries()[0]) => {
            if (!entry) {
                return;
            }
            const row = refs.body.querySelector(`tr[data-path="${CSS.escape(entry.relative_path)}"]`);
            const label = row?.querySelector(".explorer-file-label");
            if (!label) {
                return;
            }
            const input = document.createElement("input");
            input.className = "explorer-inline-rename";
            input.value = entry.name;
            label.replaceWith(input);
            input.focus();
            input.select();

            const commit = async () => {
                const newName = input.value.trim();
                if (!newName || newName === entry.name) {
                    renderFiles();
                    return;
                }
                try {
                    const payload = await apiPost("/api/explorer/rename", currentRequest({
                        entry_path: entry.relative_path,
                        new_name: newName,
                    }));
                    applyPayload(payload);
                    state.treeCache.clear();
                    await ensureTreeLoaded(state.path);
                    showToast(`Renamed to ${newName}.`);
                } catch (error) {
                    showToast(error.message, "error");
                    renderFiles();
                }
            };

            input.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    commit();
                }
                if (event.key === "Escape") {
                    renderFiles();
                }
            });
            input.addEventListener("blur", commit, { once: true });
        };

        const deleteSelected = () => {
            const selected = selectedEntries();
            if (!selected.length) {
                return;
            }
            showModal({
                title: "Delete selected items",
                body: `
                    <p class="subtle">This permanently deletes ${selected.length} selected item(s).</p>
                    <div class="explorer-confirm-list">
                        ${selected.map((entry) => `<span>${escapeHtml(entry.relative_path)}</span>`).join("")}
                    </div>
                `,
                confirmLabel: "Delete",
                danger: true,
                onConfirm: async () => {
                    const payload = await apiPost("/api/explorer/delete", currentRequest({
                        selected_paths: selectedPaths(),
                        confirm_delete: true,
                    }));
                    const deleted = payload.result?.deleted?.length || 0;
                    const failures = payload.result?.failures?.length || 0;
                    applyPayload(payload);
                    state.treeCache.clear();
                    await ensureTreeLoaded(state.path);
                    showToast(`Deleted ${deleted} item(s).${failures ? ` ${failures} failed.` : ""}`);
                },
            });
        };

        const moveSelectedTo = async (target, { replaceExisting = false } = {}) => {
            const paths = state.draggedPaths.length ? state.draggedPaths : selectedPaths();
            const payload = await apiPost("/api/explorer/move", currentRequest({
                selected_paths: paths,
                move_target: target,
                replace_existing: replaceExisting,
            }));
            applyPayload(payload);
            state.treeCache.clear();
            await ensureTreeLoaded(state.path);
            const moved = payload.result?.moved?.length || 0;
            const replaced = payload.result?.replaced?.length || 0;
            showToast(`Moved ${moved + replaced} item(s).`);
        };

        const requestMove = async (target) => {
            try {
                await moveSelectedTo(target);
            } catch (error) {
                if (error.status === 409 && error.payload?.requires_confirmation) {
                    showModal({
                        title: "Replace existing items?",
                        body: `
                            <p class="subtle">The target already contains ${error.payload.conflicts.length} selected item(s).</p>
                            <div class="explorer-confirm-list">
                                ${error.payload.conflicts.map((name) => `<span>${escapeHtml(name)}</span>`).join("")}
                            </div>
                        `,
                        confirmLabel: "Replace and move",
                        danger: true,
                        onConfirm: () => moveSelectedTo(target, { replaceExisting: true }),
                    });
                    return;
                }
                throw error;
            }
        };

        const openMoveModal = () => {
            if (!state.selected.size) {
                return;
            }
            showModal({
                title: "Move selected items",
                body: `
                    <label class="explorer-modal-field">Target folder
                        <input id="modal-move-target" type="text" value="${escapeHtml(refs.moveTarget.value || "")}" placeholder="Relative folder or saved absolute path">
                    </label>
                `,
                confirmLabel: "Move",
                onConfirm: async () => {
                    const target = document.getElementById("modal-move-target").value.trim();
                    await requestMove(target);
                },
            });
        };

        const extractSelected = () => {
            const selected = selectedEntries().filter((entry) => entry.can_extract);
            if (!selected.length) {
                return;
            }
            showModal({
                title: "Extract archives",
                body: `
                    <p class="subtle">${selected.length} archive(s) will be queued for extraction.</p>
                    <label class="explorer-modal-field">Password
                        <input id="modal-archive-password" type="password" placeholder="Optional">
                    </label>
                `,
                confirmLabel: "Queue extraction",
                onConfirm: async () => {
                    const payload = await apiPost("/api/explorer/extract", currentRequest({
                        selected_paths: selected.map((entry) => entry.relative_path),
                        password: document.getElementById("modal-archive-password").value,
                        archive_auto_sort_enabled: refs.autoSort.checked,
                        archive_auto_delete_enabled: refs.autoDelete.checked,
                    }));
                    applyPayload(payload, { preserveSelection: true });
                    showToast(`Queued ${payload.result?.queued?.length || 0} archive job(s).`);
                },
            });
        };

        const remuxSelected = () => {
            const selected = selectedEntries().filter((entry) => entry.can_compile_bluray);
            if (!selected.length || !state.mediaBackend.available) {
                return;
            }
            showModal({
                title: "Compile selected Blu-rays",
                body: `
                    <p class="subtle">${selected.length} Blu-ray folder(s) will be queued for remux.</p>
                    <label class="explorer-modal-field">Destination
                        <select id="modal-remux-destination">
                            ${state.roots.map((root) => `<option value="${escapeHtml(root.key)}" ${root.key === refs.remuxDestination.value ? "selected" : ""}>${escapeHtml(root.label)}</option>`).join("")}
                        </select>
                    </label>
                    <label class="explorer-modal-field">Output subfolder
                        <input id="modal-remux-path" type="text" value="${escapeHtml(refs.remuxPath.value || "")}" placeholder="Optional">
                    </label>
                `,
                confirmLabel: "Queue remux",
                onConfirm: async () => {
                    const payload = await apiPost("/api/explorer/compile-bluray", currentRequest({
                        selected_paths: selected.map((entry) => entry.relative_path),
                        destination: document.getElementById("modal-remux-destination").value,
                        destination_path: document.getElementById("modal-remux-path").value,
                    }));
                    applyPayload(payload, { preserveSelection: true });
                    showToast(`Queued ${payload.result?.queued || 0} Blu-ray job(s).`);
                },
            });
        };

        const saveMoveTarget = async () => {
            const moveTarget = refs.moveTarget.value.trim();
            if (!moveTarget) {
                showToast("Enter a move target before saving it.", "error");
                return;
            }
            try {
                const payload = await apiPost("/api/explorer/move-favorites", currentRequest({ move_target: moveTarget }));
                state.moveFavorites = payload.move_favorites || state.moveFavorites;
                renderRootControls();
                showToast(payload.favorite?.created ? "Saved move target." : "Move target already saved.");
            } catch (error) {
                showToast(error.message, "error");
            }
        };

        const saveArchiveSettings = async () => {
            try {
                const payload = await apiPost("/api/explorer/archive-settings", {
                    archive_auto_sort_enabled: refs.autoSort.checked,
                    archive_auto_delete_enabled: refs.autoDelete.checked,
                });
                state.archiveSettings = payload.archive_automation_settings || state.archiveSettings;
                renderRootControls();
                showToast("Saved archive defaults.");
            } catch (error) {
                refs.autoSort.checked = Boolean(state.archiveSettings.auto_sort_enabled);
                refs.autoDelete.checked = Boolean(state.archiveSettings.auto_delete_enabled);
                renderRootControls();
                showToast(error.message, "error");
            }
        };

        const pollArchiveJobs = async () => {
            try {
                const response = await fetch("/api/jobs", {
                    headers: { "Accept": "application/json" },
                    cache: "no-store",
                });
                if (!response.ok) {
                    return;
                }
                const payload = await response.json();
                const jobs = payload?.archives?.jobs || [];
                state.archiveJobs = jobs.filter((job) =>
                    job.root_key === state.root && (
                        pathWithinScope(state.path, job.archive_relative_path) ||
                        pathWithinScope(state.path, job.target_relative_path)
                    )
                );
                renderArchiveJobs(state.archiveJobs);
            } catch (error) {
                console.debug("Explorer archive polling failed", error);
            }
        };

        const isStoppedStatus = (status) => status === "failed" || status === "canceled";
        const isArchiveActiveStatus = (status) => status === "probing" || status === "extracting" || status === "sorting" || status === "cleaning";

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

        const progressPercent = (transfer) => {
            if (transfer?.bytes_total) {
                return Math.max(0, Math.min(100, (transfer.bytes_done / transfer.bytes_total) * 100));
            }
            const percent = Number(transfer?.percent);
            return Number.isNaN(percent) ? null : Math.max(0, Math.min(100, percent));
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
            if (status === "completed") {
                trackClass += " completed";
                fillClass += " completed";
                return `<div class="${trackClass}"><div class="${fillClass}" style="width:100%"></div></div>`;
            }
            if (percent === null || indeterminate) {
                return `<div class="${trackClass}"><div class="${fillClass} indeterminate"></div></div>`;
            }
            return `<div class="${trackClass}"><div class="${fillClass}" style="width:${percent.toFixed(1)}%"></div></div>`;
        };

        const renderArchiveJobs = (jobs) => {
            if (!refs.archiveList || !refs.archiveSummary) {
                return;
            }
            const activeJobs = jobs.filter((job) => isArchiveActiveStatus(job.status)).length;
            if (jobs.length === 0) {
                refs.archiveSummary.textContent = "No related archive jobs for this location yet.";
                refs.archiveList.innerHTML = '<div class="stat-tile"><span>No archive jobs here</span><strong>Nothing to monitor</strong><small class="subtle">Queue an archive extraction from this folder to see live status here.</small></div>';
                return;
            }
            refs.archiveSummary.textContent = `${jobs.length} related archive job(s), ${activeJobs} active.`;
            refs.archiveList.innerHTML = jobs.map((job) => {
                const percent = progressPercent(job.transfer);
                const progressBar = buildProgressBar(job.status, percent, isArchiveActiveStatus(job.status) && percent === null);
                const visibleMessage = String(job.transfer?.last_message || "") || "Waiting for worker output.";
                const actions = job.can_cancel ? `<button type="button" class="danger-button compact-button" data-archive-cancel="${escapeHtml(job.id)}">Cancel</button>` : "";
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
                        <div class="metric-row">
                            <span>${escapeHtml(formatBytes(job.transfer?.bytes_done))}</span>
                            <span>${escapeHtml(job.transfer?.bytes_total ? formatBytes(job.transfer.bytes_total) : "Total unknown")}</span>
                            <span>${escapeHtml(isStoppedStatus(job.status) ? "Stopped" : formatSpeed(job.transfer?.speed_bps))}</span>
                            <span>${escapeHtml(isStoppedStatus(job.status) ? "Stopped" : formatEta(job.transfer?.eta_seconds))}</span>
                        </div>
                        ${progressBar}
                        <div class="metric-row"><span>${escapeHtml(visibleMessage)}</span></div>
                        ${job.error ? `<div class="flash flash-error">${escapeHtml(job.error)}</div>` : ""}
                        ${actions ? `<div class="job-actions">${actions}</div>` : ""}
                    </article>
                `;
            }).join("");
        };

        const showContextMenu = (event, entry) => {
            event.preventDefault();
            state.contextTargetPath = entry.relative_path;
            if (!state.selected.has(entry.relative_path)) {
                state.selected.clear();
                state.selected.add(entry.relative_path);
                renderFiles();
                renderDetails();
            }
            refs.contextMenu.hidden = false;
            refs.contextMenu.style.left = `${event.clientX}px`;
            refs.contextMenu.style.top = `${event.clientY}px`;
        };

        const hideContextMenu = () => {
            refs.contextMenu.hidden = true;
        };

        refs.rootSelect?.addEventListener("change", () => {
            state.treeCache.clear();
            state.expandedTree = new Set([""]);
            loadDirectory({ root: refs.rootSelect.value, path: "" }).catch((error) => showToast(error.message, "error"));
        });

        refs.breadcrumbs?.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-path]");
            if (!button) {
                return;
            }
            state.expandedTree.add(treeKey(button.dataset.path || ""));
            loadDirectory({ path: button.dataset.path || "" }).catch((error) => showToast(error.message, "error"));
        });

        refs.filter?.addEventListener("input", () => {
            state.focusedIndex = -1;
            renderFiles();
            renderDetails();
        });

        refs.selectAll?.addEventListener("change", () => {
            for (const entry of state.filteredEntries) {
                if (refs.selectAll.checked) {
                    state.selected.add(entry.relative_path);
                } else {
                    state.selected.delete(entry.relative_path);
                }
            }
            renderFiles();
            renderDetails();
        });

        refs.body?.addEventListener("click", (event) => {
            const row = event.target.closest("tr[data-index]");
            if (!row) {
                return;
            }
            const entry = state.filteredEntries[Number(row.dataset.index)];
            if (event.target.matches("input[type='checkbox']")) {
                if (event.target.checked) {
                    state.selected.add(entry.relative_path);
                } else {
                    state.selected.delete(entry.relative_path);
                }
                state.lastSelectedIndex = Number(row.dataset.index);
                state.focusedIndex = Number(row.dataset.index);
                renderFiles();
                renderDetails();
                return;
            }
            if (event.target.closest("[data-open]")) {
                if (entry?.is_dir) {
                    openEntry(entry);
                    return;
                }
            }
            setSelectedFromEvent(entry, Number(row.dataset.index), event);
        });

        refs.body?.addEventListener("dblclick", (event) => {
            const row = event.target.closest("tr[data-index]");
            const entry = state.filteredEntries[Number(row?.dataset.index)];
            openEntry(entry);
        });

        refs.body?.addEventListener("contextmenu", (event) => {
            const row = event.target.closest("tr[data-index]");
            const entry = state.filteredEntries[Number(row?.dataset.index)];
            if (entry) {
                showContextMenu(event, entry);
            }
        });

        refs.body?.addEventListener("dragstart", (event) => {
            const row = event.target.closest("tr[data-index]");
            const entry = state.filteredEntries[Number(row?.dataset.index)];
            if (!entry) {
                return;
            }
            if (!state.selected.has(entry.relative_path)) {
                state.selected.clear();
                state.selected.add(entry.relative_path);
                renderFiles();
                renderDetails();
            }
            state.draggedPaths = selectedPaths();
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", JSON.stringify(state.draggedPaths));
        });

        refs.body?.addEventListener("dragend", () => {
            state.draggedPaths = [];
        });

        document.querySelectorAll("[data-sort]").forEach((button) => {
            button.addEventListener("click", () => {
                const sort = button.dataset.sort;
                if (state.sort === sort) {
                    state.order = state.order === "asc" ? "desc" : "asc";
                } else {
                    state.sort = sort;
                    state.order = "asc";
                }
                loadDirectory({ preserveSelection: true }).catch((error) => showToast(error.message, "error"));
            });
        });

        document.addEventListener("click", (event) => {
            if (!event.target.closest("#explorer-context-menu")) {
                hideContextMenu();
            }
            const actionButton = event.target.closest("[data-action]");
            if (!actionButton) {
                return;
            }
            const action = actionButton.dataset.action;
            if (action === "refresh") {
                loadDirectory({ preserveSelection: true }).catch((error) => showToast(error.message, "error"));
            } else if (action === "tree-refresh") {
                state.treeCache.clear();
                ensureTreeLoaded("").then(() => ensureTreeLoaded(state.path)).catch((error) => showToast(error.message, "error"));
            } else if (action === "new-folder") {
                createFolder();
            } else if (action === "rename") {
                beginInlineRename();
            } else if (action === "move") {
                openMoveModal();
            } else if (action === "delete") {
                deleteSelected();
            } else if (action === "extract") {
                extractSelected();
            } else if (action === "remux") {
                remuxSelected();
            } else if (action === "save-move-target") {
                saveMoveTarget();
            } else if (action === "modal-cancel") {
                closeModal();
            } else if (action === "clear-archive-queue") {
                fetch("/archive-jobs/clear", { method: "POST" })
                    .then(() => pollArchiveJobs())
                    .then(() => showToast("Archive queue clear request sent."))
                    .catch((error) => showToast(error.message, "error"));
            }
        });

        refs.contextMenu?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-context-action]");
            if (!button) {
                return;
            }
            hideContextMenu();
            const entry = state.entries.find((item) => item.relative_path === state.contextTargetPath);
            if (entry && !state.selected.has(entry.relative_path)) {
                state.selected.clear();
                state.selected.add(entry.relative_path);
            }
            const action = button.dataset.contextAction;
            if (action === "open") {
                openEntry(entry);
            } else if (action === "rename") {
                beginInlineRename(entry);
            } else if (action === "move") {
                openMoveModal();
            } else if (action === "delete") {
                deleteSelected();
            } else if (action === "extract") {
                extractSelected();
            } else if (action === "remux") {
                remuxSelected();
            }
        });

        refs.tree?.addEventListener("click", (event) => {
            const toggle = event.target.closest("[data-tree-toggle]");
            if (toggle) {
                const path = toggle.dataset.treeToggle || "";
                const key = treeKey(path);
                if (state.expandedTree.has(key)) {
                    state.expandedTree.delete(key);
                    renderTree();
                } else {
                    state.expandedTree.add(key);
                    ensureTreeLoaded(path).catch((error) => showToast(error.message, "error"));
                }
                return;
            }
            const row = event.target.closest("[data-tree-root]");
            if (!row) {
                return;
            }
            const root = row.dataset.treeRoot;
            const path = row.dataset.treePath || "";
            if (root !== state.root) {
                state.treeCache.clear();
                state.expandedTree = new Set([""]);
                loadDirectory({ root, path }).catch((error) => showToast(error.message, "error"));
            } else {
                state.expandedTree.add(treeKey(path));
                loadDirectory({ path }).catch((error) => showToast(error.message, "error"));
            }
        });

        refs.tree?.addEventListener("dragover", (event) => {
            const row = event.target.closest("[data-tree-path]");
            if (!row) {
                return;
            }
            event.preventDefault();
            row.classList.add("drop-target");
        });

        refs.tree?.addEventListener("dragleave", (event) => {
            event.target.closest("[data-tree-path]")?.classList.remove("drop-target");
        });

        refs.tree?.addEventListener("drop", (event) => {
            const row = event.target.closest("[data-tree-path]");
            if (!row) {
                return;
            }
            event.preventDefault();
            row.classList.remove("drop-target");
            requestMove(row.dataset.treePath || "").catch((error) => showToast(error.message, "error"));
        });

        refs.moveFavorite?.addEventListener("change", () => {
            if (refs.moveFavorite.value) {
                refs.moveTarget.value = refs.moveFavorite.value;
            }
        });

        refs.autoSort?.addEventListener("change", () => {
            if (!refs.autoSort.checked) {
                refs.autoDelete.checked = false;
            }
            saveArchiveSettings();
        });

        refs.autoDelete?.addEventListener("change", saveArchiveSettings);

        refs.archiveList?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-archive-cancel]");
            if (!button) {
                return;
            }
            fetch(`/archive-jobs/${encodeURIComponent(button.dataset.archiveCancel)}/cancel`, { method: "POST" })
                .then(() => pollArchiveJobs())
                .then(() => showToast("Archive cancel request sent."))
                .catch((error) => showToast(error.message, "error"));
        });

        document.addEventListener("keydown", (event) => {
            const target = event.target;
            const editingText = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
            if (event.key === "Escape") {
                closeModal();
                hideContextMenu();
                if (!editingText) {
                    state.selected.clear();
                    renderFiles();
                    renderDetails();
                }
                return;
            }
            if (editingText || refs.modal?.hidden === false) {
                return;
            }
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
                event.preventDefault();
                for (const entry of state.filteredEntries) {
                    state.selected.add(entry.relative_path);
                }
                renderFiles();
                renderDetails();
                return;
            }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (!state.filteredEntries.length) {
                    return;
                }
                const delta = event.key === "ArrowDown" ? 1 : -1;
                const next = Math.max(0, Math.min(state.filteredEntries.length - 1, state.focusedIndex + delta));
                const entry = state.filteredEntries[next];
                setSelectedFromEvent(entry, next, { shiftKey: event.shiftKey, metaKey: event.metaKey, ctrlKey: event.ctrlKey });
                refs.body.querySelector(`tr[data-index="${next}"]`)?.focus();
                return;
            }
            if (event.key === "Enter" && state.focusedIndex >= 0) {
                openEntry(state.filteredEntries[state.focusedIndex]);
                return;
            }
            if (event.key === "Delete" || event.key === "Backspace") {
                if (state.selected.size) {
                    event.preventDefault();
                    deleteSelected();
                }
                return;
            }
            if (event.key === "F2") {
                event.preventDefault();
                beginInlineRename();
            }
        });

        try {
            const initialPayload = JSON.parse(refs.initialPayload?.textContent || "{}");
            applyPayload(initialPayload, { preserveSelection: false });
            ensureTreeLoaded("").catch((error) => showToast(error.message, "error"));
            window.setInterval(pollArchiveJobs, Number(document.body.dataset.pollMs || 1500));
        } catch (error) {
            showToast("Explorer could not load its initial payload.", "error");
            console.error(error);
        }
    });
})();
