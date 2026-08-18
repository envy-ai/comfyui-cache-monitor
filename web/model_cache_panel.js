import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const ENDPOINT = "/comfyui-cache-monitor/model-cache";
const PIN_ENDPOINT = "/comfyui-cache-monitor/model-pin";
const REMOVE_ENDPOINT = "/comfyui-cache-monitor/model-remove";
const RELEASE_VRAM_ENDPOINT = "/comfyui-cache-monitor/release_vram";
const VRAM_WAIT_ENDPOINT = "/comfyui-cache-monitor/vram-wait";
const STYLE_ID = "comfyui-cache-monitor-style";
let destroyPanel = null;

function addStyles() {
    if (document.getElementById(STYLE_ID)) return;

    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
        .cache-monitor-panel {
            box-sizing: border-box;
            height: 100%;
            overflow: auto;
            padding: 12px;
            color: var(--input-text, #e6e8ec);
            font-size: 12px;
        }
        .cache-monitor-header {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 10px;
        }
        .cache-monitor-title {
            margin: 0;
            font-size: 14px;
            font-weight: 600;
        }
        .cache-monitor-updated {
            color: var(--descrip-text, #a4a7ad);
            font-size: 10px;
            white-space: nowrap;
        }
        .cache-monitor-header-actions {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .cache-monitor-action-button {
            padding: 5px 8px;
            border: 1px solid var(--border-color, rgba(255, 255, 255, 0.2));
            border-radius: 4px;
            background: var(--comfy-input-bg, rgba(0, 0, 0, 0.22));
            color: var(--input-text, #e6e8ec);
            font: inherit;
            cursor: pointer;
            white-space: nowrap;
        }
        .cache-monitor-action-button:hover:not(:disabled) {
            border-color: var(--p-blue-500, #3b82f6);
        }
        .cache-monitor-action-button:disabled {
            cursor: wait;
            opacity: 0.55;
        }
        .cache-monitor-wait-control {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            margin-bottom: 12px;
            padding: 8px 9px;
            border: 1px solid var(--border-color, rgba(255, 255, 255, 0.12));
            border-radius: 5px;
            background: var(--comfy-input-bg, rgba(0, 0, 0, 0.22));
        }
        .cache-monitor-wait-control input {
            margin: 2px 0 0;
        }
        .cache-monitor-wait-copy {
            display: grid;
            min-width: 0;
            gap: 2px;
        }
        .cache-monitor-wait-label {
            font-weight: 600;
        }
        .cache-monitor-wait-description,
        .cache-monitor-wait-status {
            color: var(--descrip-text, #a4a7ad);
            line-height: 1.35;
        }
        .cache-monitor-wait-status.waiting {
            color: var(--p-yellow-500, #eab308);
        }
        .cache-monitor-summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(125px, 1fr));
            gap: 8px;
            margin-bottom: 12px;
        }
        .cache-monitor-card {
            min-width: 0;
            padding: 8px 9px;
            border: 1px solid var(--border-color, rgba(255, 255, 255, 0.12));
            border-radius: 5px;
            background: var(--comfy-input-bg, rgba(0, 0, 0, 0.22));
        }
        .cache-monitor-card-title {
            overflow: hidden;
            margin-bottom: 5px;
            color: var(--descrip-text, #a4a7ad);
            font-size: 10px;
            font-weight: 600;
            text-overflow: ellipsis;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .cache-monitor-card-line {
            display: flex;
            justify-content: space-between;
            gap: 6px;
            line-height: 1.5;
        }
        .cache-monitor-card-line span:first-child {
            color: var(--descrip-text, #a4a7ad);
        }
        .cache-monitor-card-line span:last-child {
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .cache-monitor-table-wrap {
            overflow: auto;
            border: 1px solid var(--border-color, rgba(255, 255, 255, 0.12));
            border-radius: 5px;
            background: var(--comfy-input-bg, rgba(0, 0, 0, 0.14));
        }
        .cache-monitor-section-title {
            margin: 14px 0 7px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .cache-monitor-table {
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }
        .cache-monitor-table th,
        .cache-monitor-table td {
            padding: 7px 6px;
            border-bottom: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
            text-align: right;
            vertical-align: top;
        }
        .cache-monitor-table th {
            position: sticky;
            top: 0;
            z-index: 1;
            background: var(--comfy-menu-bg, #202020);
            color: var(--descrip-text, #b8bbc2);
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .cache-monitor-table th:first-child,
        .cache-monitor-table td:first-child {
            text-align: left;
        }
        .cache-monitor-model-table th:first-child,
        .cache-monitor-model-table td:first-child {
            width: 27%;
            text-align: left;
        }
        .cache-monitor-model-table th:nth-child(2),
        .cache-monitor-model-table td:nth-child(2) {
            width: 12%;
            text-align: left;
        }
        .cache-monitor-model-table th:nth-child(3),
        .cache-monitor-model-table td:nth-child(3) {
            width: 14%;
            text-align: left;
        }
        .cache-monitor-model-table th:last-child,
        .cache-monitor-model-table td:last-child {
            width: 70px;
            text-align: center;
        }
        .cache-monitor-table td:first-child {
            overflow-wrap: anywhere;
        }
        .cache-monitor-table td:not(:first-child) {
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .cache-monitor-model-state.retained {
            color: var(--p-yellow-500, #eab308);
        }
        .cache-monitor-table .cache-monitor-model-row td {
            padding-bottom: 4px;
            border-bottom: 0;
        }
        .cache-monitor-table .cache-monitor-model-bars td {
            padding: 0 0 6px;
            border-bottom: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
        }
        .cache-monitor-memory-bars {
            display: grid;
            width: 100%;
            gap: 2px;
        }
        .cache-monitor-memory-bar {
            height: 3px;
            overflow: hidden;
            background: color-mix(in srgb, var(--border-color, #666) 45%, transparent);
        }
        .cache-monitor-memory-fill {
            height: 100%;
            transition: width 160ms ease-out;
        }
        .cache-monitor-memory-fill.ram {
            background: var(--p-green-500, #22c55e);
        }
        .cache-monitor-memory-fill.vram {
            background: var(--p-blue-500, #3b82f6);
        }
        .cache-monitor-table tbody tr:last-child:not(.cache-monitor-model-bars) td {
            border-bottom: 0;
        }
        .cache-monitor-empty,
        .cache-monitor-error {
            padding: 18px 10px !important;
            color: var(--descrip-text, #a4a7ad);
            text-align: center !important;
            white-space: normal !important;
        }
        .cache-monitor-error {
            color: var(--error-text, #f38b8b);
        }
        .cache-monitor-model-actions {
            display: flex;
            justify-content: center;
            gap: 4px;
        }
        .cache-monitor-pin-button,
        .cache-monitor-remove-button {
            display: inline-flex;
            width: 28px;
            height: 28px;
            align-items: center;
            justify-content: center;
            padding: 0;
            border: 1px solid var(--border-color, rgba(255, 255, 255, 0.2));
            border-radius: 4px;
            background: transparent;
            color: var(--descrip-text, #a4a7ad);
            font: inherit;
            cursor: pointer;
        }
        .cache-monitor-pin-button[aria-pressed="true"] {
            border-color: var(--p-green-500, #22c55e);
            color: var(--p-green-500, #22c55e);
        }
        .cache-monitor-remove-button:hover:not(:disabled) {
            border-color: var(--p-red-500, #ef4444);
            color: var(--p-red-500, #ef4444);
        }
        .cache-monitor-pin-button:disabled,
        .cache-monitor-remove-button:disabled {
            cursor: wait;
            opacity: 0.55;
        }
        .cache-monitor-pin-icon,
        .cache-monitor-remove-icon {
            width: 18px;
            height: 18px;
            fill: currentColor;
        }
    `;
    document.head.append(style);
}

function element(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
}

function keepIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("cache-monitor-pin-icon");
    svg.setAttribute("viewBox", "0 -960 960 960");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m640-480 80 80v80H520v240l-40 40-40-40v-240H240v-80l80-80v-280h-40v-80h400v80h-40v280Zm-286 80h252l-46-46v-314H400v314l-46 46Zm126 0Z");
    svg.append(path);
    return svg;
}

function closeIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("cache-monitor-remove-icon");
    svg.setAttribute("viewBox", "0 -960 960 960");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M256-200 200-256l224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z");
    svg.append(path);
    return svg;
}

function formatBytes(value) {
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    const amount = value / (1024 ** unit);
    return `${amount >= 100 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

function memoryBar(kind, bytes, totalBytes) {
    const percent = totalBytes > 0 ? Math.min(100, Math.max(0, bytes / totalBytes * 100)) : 0;
    const label = `${kind.toUpperCase()}: ${percent.toFixed(1)}% (${formatBytes(bytes)} of ${formatBytes(totalBytes)})`;
    const track = element("div", "cache-monitor-memory-bar");
    track.title = label;
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", `${kind.toUpperCase()} model residency`);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", percent.toFixed(1));
    const fill = element("div", `cache-monitor-memory-fill ${kind}`);
    fill.style.width = `${percent}%`;
    track.append(fill);
    return track;
}

function addCard(container, title, lines) {
    const card = element("div", "cache-monitor-card");
    card.append(element("div", "cache-monitor-card-title", title));
    for (const [label, value, bytes] of lines) {
        const line = element("div", "cache-monitor-card-line");
        line.append(element("span", "", label));
        const amount = element("span", "", value);
        if (bytes !== undefined) amount.title = `${bytes.toLocaleString()} bytes`;
        line.append(amount);
        card.append(line);
    }
    container.append(card);
}

function renderSummary(container, data) {
    container.replaceChildren();
    const activeModels = data.models.filter((model) => model.active !== false);
    const retainedModels = data.models.filter((model) => model.active === false);
    addCard(container, "Models", [
        ["Active", String(activeModels.length)],
        ["Retained", String(retainedModels.length)],
        ["Pinned", String(data.models.filter((model) => model.pinned).length)],
        ["Pinned RAM", formatBytes(data.system_ram.pinned_model_bytes), data.system_ram.pinned_model_bytes],
    ]);

    const ram = data.system_ram;
    addCard(container, "System RAM", [
        ["Tracked model weights", formatBytes(ram.cached_model_bytes), ram.cached_model_bytes],
        ["Active model weights", formatBytes(ram.active_model_bytes ?? ram.cached_model_bytes), ram.active_model_bytes ?? ram.cached_model_bytes],
        ["Retained model weights", formatBytes(ram.retained_model_bytes ?? 0), ram.retained_model_bytes ?? 0],
        ["ComfyUI process", formatBytes(ram.process_rss_bytes ?? 0), ram.process_rss_bytes ?? 0],
        ["Available", formatBytes(ram.available_bytes), ram.available_bytes],
        ["Total", formatBytes(ram.total_bytes), ram.total_bytes],
    ]);

    for (const device of data.vram) {
        addCard(container, device.device.toUpperCase(), [
            ["Active model weights", formatBytes(device.cached_model_bytes), device.cached_model_bytes],
            ["Available for models", formatBytes(device.available_for_model_cache_bytes), device.available_for_model_cache_bytes],
            ["Available", formatBytes(device.available_bytes), device.available_bytes],
            ["Total", formatBytes(device.total_bytes), device.total_bytes],
        ]);
    }
}

function renderVramWait(input, status, data) {
    input.checked = data.enabled;
    status.classList.toggle("waiting", data.waiting);
    if (data.waiting) {
        status.textContent = `Waiting on ${data.device}: ${formatBytes(data.available_bytes)} available, ${formatBytes(data.required_bytes)} required`;
    } else if (data.enabled) {
        status.textContent = "Armed; model loading will pause when external VRAM causes a shortfall.";
    } else {
        status.textContent = "Disabled";
    }
}

function renderModels(body, models, setPinned, removeModel) {
    body.replaceChildren();
    if (!models.length) {
        const row = element("tr");
        const cell = element("td", "cache-monitor-empty", "No active or retained models.");
        cell.colSpan = 7;
        row.append(cell);
        body.append(row);
        return;
    }

    for (const model of models) {
        const active = model.active !== false;
        const row = element("tr", "cache-monitor-model-row");
        const values = [
            [model.model, undefined, ""],
            [active ? "Active" : "Retained", undefined, `cache-monitor-model-state ${active ? "active" : "retained"}`],
            [model.device, undefined, ""],
            [formatBytes(model.system_ram_bytes), model.system_ram_bytes, ""],
            [formatBytes(model.vram_bytes), model.vram_bytes, ""],
            [formatBytes(model.total_weight_bytes), model.total_weight_bytes, ""],
        ];
        for (const [value, bytes, className] of values) {
            const cell = element("td", className, value);
            if (bytes !== undefined) cell.title = `${bytes.toLocaleString()} bytes`;
            if (!active && className.includes("cache-monitor-model-state")) {
                cell.title = "Pinned by this mod after leaving ComfyUI's active model registry";
            }
            row.append(cell);
        }
        const actionCell = element("td");
        const actions = element("div", "cache-monitor-model-actions");
        const pinButton = element("button", "cache-monitor-pin-button");
        pinButton.type = "button";
        pinButton.title = model.pinned
            ? active
                ? "Allow this model to be unloaded from system RAM"
                : "Release this retained model from system RAM"
            : "Keep this model in system RAM until it is unpinned or ComfyUI exits";
        pinButton.setAttribute("aria-label", `${model.pinned ? "Unpin" : "Pin"} ${model.model} in system RAM`);
        pinButton.setAttribute("aria-pressed", String(model.pinned));
        pinButton.append(keepIcon());
        pinButton.addEventListener("click", async () => {
            pinButton.disabled = true;
            removeButton.disabled = true;
            try {
                await setPinned(model, !model.pinned);
            } finally {
                pinButton.disabled = false;
                removeButton.disabled = false;
            }
        });
        const removeButton = element("button", "cache-monitor-remove-button");
        removeButton.type = "button";
        removeButton.title = `Remove ${model.model} from ComfyUI's RAM cache and release its VRAM`;
        removeButton.setAttribute("aria-label", `Remove ${model.model} from system RAM`);
        removeButton.append(closeIcon());
        removeButton.addEventListener("click", async () => {
            pinButton.disabled = true;
            removeButton.disabled = true;
            try {
                await removeModel(model);
            } finally {
                pinButton.disabled = false;
                removeButton.disabled = false;
            }
        });
        actions.append(pinButton, removeButton);
        actionCell.append(actions);
        row.append(actionCell);

        const barsRow = element("tr", "cache-monitor-model-bars");
        const barsCell = element("td");
        barsCell.colSpan = 7;
        const bars = element("div", "cache-monitor-memory-bars");
        bars.append(
            memoryBar("ram", model.system_ram_bytes, model.total_weight_bytes),
            memoryBar("vram", model.vram_bytes, model.total_weight_bytes),
        );
        barsCell.append(bars);
        barsRow.append(barsCell);
        body.append(row, barsRow);
    }
}

function formatRemovedAt(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
    });
}

function renderRemovedModels(body, models) {
    body.replaceChildren();
    if (!models.length) {
        const row = element("tr");
        const cell = element("td", "cache-monitor-empty", "No active-registry removals recorded this session.");
        cell.colSpan = 3;
        row.append(cell);
        body.append(row);
        return;
    }

    for (const model of models) {
        const row = element("tr");
        const removedAt = formatRemovedAt(model.removed_at);
        row.append(
            element("td", "", model.model),
            element("td", "", model.device),
            element("td", "", removedAt),
        );
        row.lastElementChild.title = model.removed_at;
        body.append(row);
    }
}

function renderPanel(container) {
    destroyPanel?.();
    addStyles();
    container.replaceChildren();
    container.classList.add("cache-monitor-panel");

    const header = element("div", "cache-monitor-header");
    header.append(element("h3", "cache-monitor-title", "Model Memory"));
    const headerActions = element("div", "cache-monitor-header-actions");
    const freeVramButton = element("button", "cache-monitor-action-button", "Free VRAM");
    freeVramButton.type = "button";
    freeVramButton.title = "Offload active model weights from VRAM while retaining their RAM caches";
    const updated = element("span", "cache-monitor-updated", "Loading…");
    headerActions.append(freeVramButton, updated);
    header.append(headerActions);

    const waitControl = element("div", "cache-monitor-wait-control");
    const waitCheckbox = element("input");
    waitCheckbox.type = "checkbox";
    waitCheckbox.id = "cache-monitor-wait-for-vram";
    const waitCopy = element("div", "cache-monitor-wait-copy");
    const waitLabel = element("label", "cache-monitor-wait-label", "Wait for external VRAM");
    waitLabel.htmlFor = waitCheckbox.id;
    const waitDescription = element(
        "span",
        "cache-monitor-wait-description",
        "Hold model loading when another process is using VRAM required by the active prompt.",
    );
    const waitStatus = element("span", "cache-monitor-wait-status", "Loading…");
    waitCopy.append(waitLabel, waitDescription, waitStatus);
    waitControl.append(waitCheckbox, waitCopy);

    const summary = element("div", "cache-monitor-summary");
    const tableWrap = element("div", "cache-monitor-table-wrap");
    const table = element("table", "cache-monitor-table cache-monitor-model-table");
    const head = element("thead");
    const headerRow = element("tr");
    for (const title of ["Model", "State", "For device", "RAM", "VRAM", "Total", "Actions"]) {
        headerRow.append(element("th", "", title));
    }
    head.append(headerRow);
    const body = element("tbody");
    table.append(head, body);
    tableWrap.append(table);

    const removedTitle = element("h4", "cache-monitor-section-title", "Recently Removed from Active Registry");
    const removedWrap = element("div", "cache-monitor-table-wrap");
    const removedTable = element("table", "cache-monitor-table");
    const removedHead = element("thead");
    const removedHeaderRow = element("tr");
    for (const title of ["Model", "For device", "Removed"]) {
        removedHeaderRow.append(element("th", "", title));
    }
    removedHead.append(removedHeaderRow);
    const removedBody = element("tbody");
    removedTable.append(removedHead, removedBody);
    removedWrap.append(removedTable);

    container.append(header, waitControl, summary, tableWrap, removedTitle, removedWrap);

    let active = true;
    let refreshing = false;
    let request = null;

    const setPinned = async (model, pinned) => {
        try {
            const response = await api.fetchApi(PIN_ENDPOINT, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ cache_id: model.cache_id, pinned }),
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || `${response.status} ${response.statusText}`);
            }
            if (data.released_ram_bytes > 0 || data.released_vram_bytes > 0) {
                app.extensionManager.toast.add({
                    severity: "success",
                    summary: "Retained model released",
                    detail: `${formatBytes(data.released_ram_bytes)} RAM and ${formatBytes(data.released_vram_bytes)} VRAM released.`,
                    life: 4000,
                });
            }
            await refresh();
        } catch (error) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Could not change RAM pin",
                detail: error.message,
                life: 5000,
            });
        }
    };

    const removeModel = async (model) => {
        try {
            const response = await api.fetchApi(REMOVE_ENDPOINT, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ cache_id: model.cache_id }),
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || `${response.status} ${response.statusText}`);
            }
            app.extensionManager.toast.add({
                severity: "success",
                summary: "Model removed from RAM",
                detail: `${formatBytes(data.removed_ram_bytes)} RAM and ${formatBytes(data.released_vram_bytes)} VRAM removed.`,
                life: 4000,
            });
            await refresh();
        } catch (error) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Could not remove model",
                detail: error.message,
                life: 5000,
            });
        }
    };

    const setVramWait = async (enabled) => {
        const previous = !enabled;
        waitCheckbox.disabled = true;
        try {
            const response = await api.fetchApi(VRAM_WAIT_ENDPOINT, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
            renderVramWait(waitCheckbox, waitStatus, data);
        } catch (error) {
            waitCheckbox.checked = previous;
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Could not change VRAM wait",
                detail: error.message,
                life: 5000,
            });
        } finally {
            waitCheckbox.disabled = false;
        }
    };

    waitCheckbox.addEventListener("change", () => setVramWait(waitCheckbox.checked));

    const releaseVram = async () => {
        freeVramButton.disabled = true;
        freeVramButton.textContent = "Freeing…";
        try {
            const response = await api.fetchApi(RELEASE_VRAM_ENDPOINT, { method: "POST" });
            const data = await response.json();
            if (!response.ok || !data.released) {
                throw new Error(data.error || `${response.status} ${response.statusText}`);
            }
            app.extensionManager.toast.add({
                severity: "success",
                summary: "VRAM released",
                detail: `${formatBytes(data.released_bytes)} released from ${data.models.length} active models.`,
                life: 4000,
            });
            await refresh();
        } catch (error) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Could not free VRAM",
                detail: error.message,
                life: 5000,
            });
        } finally {
            freeVramButton.disabled = false;
            freeVramButton.textContent = "Free VRAM";
        }
    };

    freeVramButton.addEventListener("click", releaseVram);

    const refresh = async () => {
        if (!active || refreshing || !container.isConnected || container.getClientRects().length === 0) return;
        refreshing = true;
        request = new AbortController();
        try {
            const response = await api.fetchApi(ENDPOINT, { cache: "no-store", signal: request.signal });
            if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
            const data = await response.json();
            if (!active) return;
            renderVramWait(waitCheckbox, waitStatus, data.vram_wait);
            renderSummary(summary, data);
            renderModels(body, data.models, setPinned, removeModel);
            renderRemovedModels(removedBody, data.removed_models);
            updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
        } catch (error) {
            if (!active || error.name === "AbortError") return;
            summary.replaceChildren();
            body.replaceChildren();
            removedBody.replaceChildren();
            const row = element("tr");
            const cell = element("td", "cache-monitor-error", `Unable to read model state: ${error.message}`);
            cell.colSpan = 7;
            row.append(cell);
            body.append(row);
            updated.textContent = "Unavailable";
        } finally {
            refreshing = false;
            request = null;
        }
    };

    refresh();
    const timer = window.setInterval(refresh, 1000);
    destroyPanel = () => {
        active = false;
        window.clearInterval(timer);
        request?.abort();
        destroyPanel = null;
    };
}

app.registerExtension({
    name: "comfyui-cache-monitor.model-cache-panel",
    async setup() {
        app.extensionManager.registerSidebarTab({
            id: "comfyui-cache-monitor",
            title: "Model Memory",
            tooltip: "RAM and VRAM used by ComfyUI's active and retained models",
            icon: "pi pi-database",
            type: "custom",
            render: renderPanel,
            destroy: () => destroyPanel?.(),
        });
    },
});
