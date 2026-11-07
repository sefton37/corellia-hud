// Corellia HUD pin: keeps the HUD window behind everything, on every workspace, at the right edge,
// and out of the dock / Alt-Tab list. It only ever touches a window whose WM class or title matches.
// Precedent: the shipped ding@rastersoft.com extension (lower on 'raised', unmake_above on 'notify::above').
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {classifyWindow} from './match.js';

const WM_CLASS = 'corellia-hud';
const TITLE = 'Corellia HUD';
const DETAIL_WM_CLASS = 'corellia-hud-detail';   // the floating detail window (same process, own app id)
const DETAIL_TITLE = 'Corellia detail';
const WIDTH = 380;   // logical pixels; the launcher asks for the same width
const MARGIN = 12;   // gap to the work-area edges
const DETAIL_WIDTH = 560;        // detail window: beside the column, not on top of it
const DETAIL_GAP = 12;           // between the detail window and the column
const DETAIL_MAX_HEIGHT = 900;
const TAG = 'corellia-hud-pin:';

class PinnedWindow {
    constructor(win, onChange) {
        this._win = win;
        this._ids = [];
        this._timeout = 0;
        const on = (signal, fn) => this._ids.push(win.connect(signal, fn));
        // After every raise (click, focus) push it straight back down.
        this._ids.push(win.connect_after('raised', () => this._win.lower()));
        on('notify::above', () => { if (this._win.above) this._win.unmake_above(); });
        on('notify::minimized', () => this._win.unminimize());
        on('position-changed', () => this.place());
        on('size-changed', () => this.place());
        this.apply();
        onChange?.();
    }

    apply() {
        const win = this._win;
        win.stick();                       // every workspace
        win.lower();                       // bottom of the stack
        this._hide();
        this.place();
    }

    _hide() {
        // Mutter 50 exposes hide_from_window_list() on the window itself; older shells had it on a
        // Meta.WaylandClient that only the launching process could hold.
        const win = this._win;
        if (typeof win.hide_from_window_list === 'function')
            win.hide_from_window_list();
        console.log(`${TAG} hide_from_window_list=${typeof win.hide_from_window_list} skip_taskbar=${win.is_skip_taskbar()}`);
    }

    place() {
        if (this._placing)
            return;
        this._placing = true;
        const win = this._win;
        const monitor = Main.layoutManager.primaryIndex;
        const area = Main.layoutManager.getWorkAreaForMonitor(monitor);
        const x = area.x + area.width - WIDTH - MARGIN;
        const y = area.y + MARGIN;
        const h = area.height - 2 * MARGIN;
        const rect = win.get_frame_rect();
        if (rect.x !== x || rect.y !== y || rect.width !== WIDTH || rect.height !== h)
            win.move_resize_frame(true, x, y, WIDTH, h);
        // Reset after the resulting position/size signals have fired.
        GLib.idle_add(GLib.PRIORITY_LOW, () => { this._placing = false; return GLib.SOURCE_REMOVE; });
    }

    report() {
        const win = this._win;
        const tab = global.display.get_tab_list(Meta.TabList.NORMAL, null).includes(win);
        const r = win.get_frame_rect();
        console.log(`${TAG} report skip_taskbar=${win.is_skip_taskbar()} on_all_workspaces=${win.is_on_all_workspaces()} ` +
            `in_alt_tab=${tab} rect=${r.x},${r.y} ${r.width}x${r.height} above=${win.above}`);
    }

    destroy() {
        this._ids.forEach(id => this._win.disconnect(id));
        this._ids = [];
        this._win.unstick();
        this._win.show_in_window_list?.();
    }
}

// The floating detail window: raised, to the left of the column, top-aligned with it, out of the window
// list. Unlike the column it is never lowered, never sticky, and may take keyboard focus (Esc closes it).
class DetailWindow {
    constructor(win) {
        this._win = win;
        this._ids = [];
        this._placing = false;
        this._ids.push(win.connect('size-changed', () => this.place()));
        this._ids.push(win.connect('position-changed', () => this.place()));
        this.apply();
    }

    apply() {
        const win = this._win;
        win.make_above();
        if (typeof win.hide_from_window_list === 'function')
            win.hide_from_window_list();
        this.place();
        win.activate(global.get_current_time());   // first map only: lets Esc reach the window
        console.log(`${TAG} detail window placed above the column`);
    }

    place() {
        if (this._placing)
            return;
        this._placing = true;
        const win = this._win;
        const area = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
        const columnX = area.x + area.width - WIDTH - MARGIN;
        const x = columnX - DETAIL_GAP - DETAIL_WIDTH;
        const y = area.y + DETAIL_GAP;
        const h = Math.min(area.height - 2 * DETAIL_GAP, DETAIL_MAX_HEIGHT);
        const rect = win.get_frame_rect();
        if (rect.x !== x || rect.y !== y || rect.width !== DETAIL_WIDTH || rect.height !== h)
            win.move_resize_frame(true, x, y, DETAIL_WIDTH, h);
        GLib.idle_add(GLib.PRIORITY_LOW, () => { this._placing = false; return GLib.SOURCE_REMOVE; });
    }

    destroy() {
        this._ids.forEach(id => this._win.disconnect(id));
        this._ids = [];
    }
}

export default class CorelliaHudPin extends Extension {
    enable() {
        this._pinned = new Map();
        this._created = global.display.connect('window-created', (_d, win) => this._consider(win));
        this._areas = global.display.connect('workareas-changed', () => this._pinned.forEach(p => p.place()));
        global.get_window_actors().forEach(a => this._consider(a.meta_window));
    }

    _consider(win) {
        if (this._pinned.has(win))
            return;
        const take = () => {
            if (this._pinned.has(win))
                return false;
            const kind = classifyWindow(win.get_wm_class(), win.get_title());
            if (kind === null)
                return false;
            this._pinned.set(win, kind === 'detail' ? new DetailWindow(win) : new PinnedWindow(win, () => null));
            win.connect('unmanaged', () => this._pinned.delete(win));
            if (kind === 'column' && GLib.getenv('CORELLIA_HUD_PIN_DEBUG'))
                GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1500, () => { this._pinned.get(win)?.report(); return GLib.SOURCE_REMOVE; });
            return true;
        };
        // wm_class / title are not always set at window-created time on Wayland: retry once mapped.
        if (!take()) {
            const id = win.connect('notify::title', () => { if (take()) win.disconnect(id); });
            win.connect_after('shown', () => take());
        }
    }

    disable() {
        global.display.disconnect(this._created);
        global.display.disconnect(this._areas);
        this._pinned?.forEach(p => p.destroy());
        this._pinned = null;
    }
}
