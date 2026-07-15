document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[data-confirm]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            if (!window.confirm(form.dataset.confirm || '确定执行该操作吗？')) {
                event.preventDefault();
            }
        });
    });

    document.querySelectorAll('button[data-confirm]').forEach((button) => {
        button.addEventListener('click', (event) => {
            if (!window.confirm(button.dataset.confirm || '确定执行该操作吗？')) {
                event.preventDefault();
            }
        });
    });

    document.querySelectorAll('[data-file-preview]').forEach((container) => {
        const input = document.getElementById(container.dataset.filePreview);
        if (!input) return;
        input.addEventListener('change', () => {
            container.innerHTML = '';
            [...input.files].forEach((file, index) => {
                const row = document.createElement('div');
                row.className = 'file-preview-item';
                const preview = document.createElement(file.type.startsWith('video/') ? 'video' : 'img');
                if (file.type.startsWith('image/') || file.type.startsWith('video/')) {
                    preview.src = URL.createObjectURL(file);
                    if (preview.tagName === 'VIDEO') preview.muted = true;
                    row.appendChild(preview);
                } else {
                    const icon = document.createElement('span');
                    icon.textContent = '▤';
                    row.appendChild(icon);
                }
                const label = document.createElement('span');
                label.textContent = `${index + 1}. ${file.name} (${Math.ceil(file.size / 1024)} KB)`;
                row.appendChild(label);
                container.appendChild(row);
            });
        });
    });

    document.querySelectorAll('[data-select-all]').forEach((selectAll) => {
        const group = selectAll.dataset.selectAll;
        const items = [...document.querySelectorAll(`[data-selection="${group}"]`)];
        const counter = document.querySelector('[data-selection-count]');
        const update = () => {
            const selected = items.filter((item) => item.checked).length;
            selectAll.checked = items.length > 0 && selected === items.length;
            selectAll.indeterminate = selected > 0 && selected < items.length;
            if (counter) counter.textContent = `已选择 ${selected} 条`;
        };
        selectAll.addEventListener('change', () => {
            items.forEach((item) => { item.checked = selectAll.checked; });
            update();
        });
        items.forEach((item) => item.addEventListener('change', update));
        update();
    });

    document.querySelectorAll('[data-content-editor]').forEach((form) => {
        const container = form.querySelector('[data-existing-files]');
        const deleteInput = form.querySelector('[name="delete_file_ids"]');
        const orderInput = form.querySelector('[name="file_order"]');
        const deleteSummary = form.querySelector('[data-delete-summary]');
        const deleteCount = form.querySelector('[data-delete-count]');
        const undoDeletes = form.querySelector('[data-undo-deletes]');
        if (!container || !deleteInput || !orderInput) return;

        const initialDeleted = new Set(deleteInput.value.split(',').filter(Boolean));
        const initialOrder = orderInput.value.split(',').filter(Boolean);
        initialOrder.forEach((fileId) => {
            const row = container.querySelector(`[data-file-item][data-file-id="${fileId}"]`);
            if (row) container.appendChild(row);
        });
        container.querySelectorAll('[data-file-item]').forEach((row) => {
            if (initialDeleted.has(row.dataset.fileId)) {
                row.classList.add('pending-delete');
                row.hidden = true;
            }
        });

        const updateState = () => {
            const rows = [...container.querySelectorAll('[data-file-item]')];
            const deletedRows = rows.filter((row) => row.classList.contains('pending-delete'));
            deleteInput.value = deletedRows.map((row) => row.dataset.fileId).join(',');
            orderInput.value = rows.map((row) => row.dataset.fileId).join(',');
            if (deleteSummary) deleteSummary.hidden = deletedRows.length === 0;
            if (deleteCount) deleteCount.textContent = `已移除 ${deletedRows.length} 个文件，保存后生效。`;
        };

        container.addEventListener('click', (event) => {
            const button = event.target.closest('button');
            const row = event.target.closest('[data-file-item]');
            if (!button || !row) return;
            if (button.matches('[data-file-delete]')) {
                row.classList.add('pending-delete');
                row.hidden = true;
                updateState();
                return;
            }
            const direction = button.dataset.fileMove;
            if (!direction) return;
            const sameGroup = [...container.querySelectorAll(`[data-file-item][data-group-no="${row.dataset.groupNo}"]:not(.pending-delete)`)];
            const index = sameGroup.indexOf(row);
            if (direction === 'up' && index > 0) {
                sameGroup[index - 1].before(row);
            } else if (direction === 'down' && index >= 0 && index < sameGroup.length - 1) {
                sameGroup[index + 1].after(row);
            }
            updateState();
        });
        if (undoDeletes) {
            undoDeletes.addEventListener('click', () => {
                container.querySelectorAll('[data-file-item].pending-delete').forEach((row) => {
                    row.classList.remove('pending-delete');
                    row.hidden = false;
                });
                updateState();
            });
        }
        updateState();
    });

    document.querySelectorAll('[data-channel-resolver]').forEach((form) => {
        const dialog = document.querySelector('[data-channel-result-dialog]');
        const output = dialog?.querySelector('[data-channel-id-output]');
        const error = dialog?.querySelector('[data-channel-resolve-error]');
        const copyButton = dialog?.querySelector('[data-copy-channel-id]');
        const submitButton = form.querySelector('[data-resolve-submit]');
        if (!dialog || !output || !error || !copyButton || !submitButton) return;

        const openDialog = () => {
            if (typeof dialog.showModal === 'function') dialog.showModal();
            else dialog.setAttribute('open', '');
        };
        const closeDialog = () => {
            if (typeof dialog.close === 'function') dialog.close();
            else dialog.removeAttribute('open');
        };

        dialog.querySelectorAll('[data-dialog-close]').forEach((button) => {
            button.addEventListener('click', closeDialog);
        });
        dialog.addEventListener('click', (event) => {
            if (event.target === dialog) closeDialog();
        });

        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const originalText = submitButton.textContent;
            submitButton.disabled = true;
            submitButton.textContent = '解析中...';
            try {
                const response = await fetch(form.action, {
                    method: 'POST',
                    body: new FormData(form),
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                    credentials: 'same-origin',
                });
                const result = await response.json();
                if (!response.ok || !result.ok) throw new Error(result.error || '解析失败');
                output.textContent = result.chat_id;
                output.hidden = false;
                error.hidden = true;
                copyButton.hidden = false;
            } catch (resolveError) {
                output.textContent = '';
                output.hidden = true;
                error.textContent = resolveError.message || '解析失败';
                error.hidden = false;
                copyButton.hidden = true;
            } finally {
                submitButton.disabled = false;
                submitButton.textContent = originalText;
                openDialog();
            }
        });

        copyButton.addEventListener('click', async () => {
            if (!output.textContent) return;
            await navigator.clipboard.writeText(output.textContent);
            const originalText = copyButton.textContent;
            copyButton.textContent = '已复制';
            window.setTimeout(() => { copyButton.textContent = originalText; }, 1200);
        });
    });
});
