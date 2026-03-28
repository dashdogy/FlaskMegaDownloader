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
    const moveTargetInput = document.getElementById("move-target-path");
    const savedMoveTargetSelect = document.getElementById("saved-move-target");

    const updateSelectionUi = () => {
        const selectedCount = entryCheckboxes.filter((checkbox) => checkbox.checked).length;
        const totalCount = entryCheckboxes.length;
        const hasMoveTarget = Boolean(moveTargetInput?.value.trim());

        if (selectionCount) {
            selectionCount.textContent = `${selectedCount} selected`;
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

    if (savedMoveTargetSelect && moveTargetInput) {
        savedMoveTargetSelect.addEventListener("change", () => {
            const selectedValue = savedMoveTargetSelect.value;
            if (selectedValue) {
                moveTargetInput.value = selectedValue;
            }
            updateSelectionUi();
        });
    }

    updateSelectionUi();
})();
