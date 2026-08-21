/** @odoo-module **/

// Copyright (C) 2026 Mr Abdulkarim M. Mousa
// @ Valutoria L.T.D. <abdulkarim@valutoria.com>
// License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import { loadJS } from "@web/core/assets";
import { rpc } from "@web/core/network/rpc";

let mapsPromise;

async function loadGoogleMaps() {
    if (!mapsPromise) {
        mapsPromise = rpc("/web/dataset/call_kw", {
            model: "ir.config_parameter",
            method: "get_param",
            args: ["google.api_key_geocode", ""],
            kwargs: {},
        }).then(async (key) => {
            if (!key) {
                throw new Error("Google Maps API key is not configured.");
            }
            await loadJS(
                `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}`
            );
            return window.google.maps;
        });
    }
    return mapsPromise;
}

function readPoints() {
    return [...document.querySelectorAll(".o_field_one2many[name='polygon_ids'] .o_data_row")]
        .map((row) => [...row.querySelectorAll(".o_data_cell")])
        .map((cells) => ({
            lat: Number.parseFloat(cells[1]?.textContent),
            lng: Number.parseFloat(cells[2]?.textContent),
        }))
        .filter(({ lat, lng }) => Number.isFinite(lat) && Number.isFinite(lng));
}

async function initialize() {
    const container = document.getElementById("district_polygon_map");
    if (!container || container.dataset.initialized) {
        return;
    }
    container.dataset.initialized = "1";
    const status = document.getElementById("polygon_status");
    const maps = await loadGoogleMaps();
    const points = readPoints();
    const center = points[0] || { lat: 24.7136, lng: 46.6753 };
    const map = new maps.Map(container, {
        center,
        zoom: points.length ? 14 : 11,
        mapTypeControl: false,
        streetViewControl: false,
    });
    const polygon = new maps.Polygon({
        paths: points,
        editable: true,
        strokeColor: "#2563eb",
        fillColor: "#93c5fd",
        fillOpacity: 0.3,
        map,
    });
    const refreshStatus = () => {
        const count = polygon.getPath().getLength();
        if (status) {
            status.textContent = `${count} point${count === 1 ? "" : "s"}`;
        }
    };
    map.addListener("click", (event) => {
        polygon.getPath().push(event.latLng);
        refreshStatus();
    });
    document.getElementById("btn_polygon_undo")?.addEventListener("click", () => {
        const path = polygon.getPath();
        if (path.getLength()) {
            path.pop();
            refreshStatus();
        }
    });
    document.getElementById("btn_polygon_clear")?.addEventListener("click", () => {
        polygon.setPath([]);
        refreshStatus();
    });
    document.getElementById("btn_polygon_save")?.addEventListener("click", async () => {
        const match = window.location.pathname.match(/\/(\d+)(?:\/|$)/);
        const districtId = match ? Number.parseInt(match[1], 10) : false;
        if (!districtId) {
            return;
        }
        const points = polygon.getPath().getArray().map((point, index) => ({
            sequence: (index + 1) * 10,
            lat: point.lat(),
            lng: point.lng(),
        }));
        await rpc("/web/dataset/call_kw", {
            model: "res.district",
            method: "write",
            args: [[districtId], {
                polygon_ids: [[5, 0, 0], ...points.map((point) => [0, 0, point])],
            }],
            kwargs: {},
        });
        window.location.reload();
    });
    refreshStatus();
}

const observer = new MutationObserver(initialize);
observer.observe(document.body, { childList: true, subtree: true });
initialize().catch((error) => {
    const status = document.getElementById("polygon_status");
    if (status) {
        status.textContent = error.message;
    }
});
