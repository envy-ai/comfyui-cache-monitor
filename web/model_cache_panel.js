import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const ENDPOINT = "/comfyui-cache-monitor/model-cache";
const PIN_ENDPOINT = "/comfyui-cache-monitor/model-pin";
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
            width: 31%;
            text-align: left;
        }
        .cache-monitor-table th:nth-child(2),
        .cache-monitor-table td:nth-child(2) {
            width: 14%;
            text-align: left;
        }
        .cache-monitor-model-table th:last-child,
        .cache-monitor-model-table td:last-child {
            width: 52px;
            text-align: center;
        }
        .cache-monitor-table td:first-child {
            overflow-wrap: anywhere;
        }
        .cache-monitor-table td:not(:first-child) {
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
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
        .cache-monitor-pin-button {
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
        .cache-monitor-pin-button:disabled {
            cursor: wait;
            opacity: 0.55;
        }
        .cache-monitor-pin-icon {
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
    addCard(container, "Models", [
        ["Active", String(data.models.length)],
        ["Pinned", String(data.models.filter((model) => model.pinned).length)],
        ["Pinned RAM", formatBytes(data.system_ram.pinned_model_bytes), data.system_ram.pinned_model_bytes],
    ]);

    const ram = data.system_ram;
    addCard(container, "System RAM", [
        ["Active model weights", formatBytes(ram.cached_model_bytes), ram.cached_model_bytes],
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

function renderModels(body, models, setPinned) {
    body.replaceChildren();
    if (!models.length) {
        const row = element("tr");
        const cell = element("td", "cache-monitor-empty", "No models are in ComfyUI's active model registry.");
        cell.colSpan = 6;
        row.append(cell);
        body.append(row);
        return;
    }

    for (const model of models) {
        const row = element("tr", "cache-monitor-model-row");
        const values = [
            [model.model, undefined],
            [model.device, undefined],
            [formatBytes(model.system_ram_bytes), model.system_ram_bytes],
            [formatBytes(model.vram_bytes), model.vram_bytes],
            [formatBytes(model.total_weight_bytes), model.total_weight_bytes],
        ];
        for (const [value, bytes] of values) {
            const cell = element("td", "", value);
            if (bytes !== undefined) cell.title = `${bytes.toLocaleString()} bytes`;
            row.append(cell);
        }
        const pinCell = element("td");
        const pinButton = element("button", "cache-monitor-pin-button");
        pinButton.type = "button";
        pinButton.title = model.pinned
            ? "Allow this model to be unloaded from system RAM"
            : "Keep this model in system RAM until it is unpinned or ComfyUI exits";
        pinButton.setAttribute("aria-label", `${model.pinned ? "Unpin" : "Pin"} ${model.model} in system RAM`);
        pinButton.setAttribute("aria-pressed", String(model.pinned));
        pinButton.append(keepIcon());
        pinButton.addEventListener("click", async () => {
            pinButton.disabled = true;
            await setPinned(model, !model.pinned);
        });
        pinCell.append(pinButton);
        row.append(pinCell);

        const barsRow = element("tr", "cache-monitor-model-bars");
        const barsCell = element("td");
        barsCell.colSpan = 6;
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
    header.append(element("h3", "cache-monitor-title", "Active Model Memory"));
    const updated = element("span", "cache-monitor-updated", "Loading…");
    header.append(updated);

    const summary = element("div", "cache-monitor-summary");
    const tableWrap = element("div", "cache-monitor-table-wrap");
    const table = element("table", "cache-monitor-table cache-monitor-model-table");
    const head = element("thead");
    const headerRow = element("tr");
    for (const title of ["Model", "For device", "RAM", "VRAM", "Total", "RAM pin"]) {
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

    container.append(header, summary, tableWrap, removedTitle, removedWrap);

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
            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || `${response.status} ${response.statusText}`);
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

    const refresh = async () => {
        if (!active || refreshing || !container.isConnected || container.getClientRects().length === 0) return;
        refreshing = true;
        request = new AbortController();
        try {
            const response = await api.fetchApi(ENDPOINT, { cache: "no-store", signal: request.signal });
            if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
            const data = await response.json();
            if (!active) return;
            renderSummary(summary, data);
            renderModels(body, data.models, setPinned);
            renderRemovedModels(removedBody, data.removed_models);
            updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
        } catch (error) {
            if (!active || error.name === "AbortError") return;
            summary.replaceChildren();
            body.replaceChildren();
            removedBody.replaceChildren();
            const row = element("tr");
            const cell = element("td", "cache-monitor-error", `Unable to read active model state: ${error.message}`);
            cell.colSpan = 6;
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
            tooltip: "RAM and VRAM used by ComfyUI's active model registry",
            icon: "pi pi-database",
            type: "custom",
            render: renderPanel,
            destroy: () => destroyPanel?.(),
        });
    },
});
