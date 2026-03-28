(() => {
    const bulkForm = document.getElementById("explorer-bulk-form");
    if (!bulkForm) {
        return;
    }

    const selectAll = document.getElementById("select-all-entries");
    const selectionCount = document.getElementById("selection-count");
    const entryCheckboxes = Array.from(bulkForm.querySelectorAll(".entry-select"));
    const bulkButtons = Array.from(bulkForm.querySelectorAll("[data-bulk-action]"));

    const updateSelectionUi = () => {
        const selectedCount = entryCheckboxes.filter((checkbox) => checkbox.checked).length;
        const totalCount = entryCheckboxes.length;

        if (selectionCount) {
            selectionCount.textContent = `${selectedCount} selected`;
        }

        for (const button of bulkButtons) {
            button.disabled = selectedCount === 0;
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

    updateSelectionUi();
})();
