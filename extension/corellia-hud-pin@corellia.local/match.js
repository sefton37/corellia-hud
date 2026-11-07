// Which Corellia HUD window is this? Pure ES module (no gi:// imports) so node can test it.
// Matching is by exact equality: lookalike class names or titles match nothing.
export const WM_CLASS = 'corellia-hud';
export const TITLE = 'Corellia HUD';
export const DETAIL_WM_CLASS = 'corellia-hud-detail';
export const DETAIL_TITLE = 'Corellia detail';

// 'detail' | 'column' | null. The detail identity is tested first, so the detail window can never be
// taken for the pinned column even while one of its two attributes is not set yet (Wayland sets the
// class and the title at different moments).
export function classifyWindow(wmClass, title) {
    if (wmClass === DETAIL_WM_CLASS || title === DETAIL_TITLE)
        return 'detail';
    if (wmClass === WM_CLASS || title === TITLE)
        return 'column';
    return null;
}
