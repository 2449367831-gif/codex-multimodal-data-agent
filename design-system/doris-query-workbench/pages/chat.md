# Chat Page Overrides

> **PROJECT:** Doris Query Workbench
> **Generated:** 2026-08-29 10:06:48
> **Page Type:** Dashboard / Data View

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file (`design-system/MASTER.md`).
> Only deviations from the Master are documented here. For all other rules, refer to the Master.

---

## Page-Specific Rules

### Layout Overrides

- **Max Width:** 1400px or full-width
- **Grid:** three functional columns on desktop: task navigation, conversation, execution trace. Collapse to one column below 900px; drawers are used for navigation and trace.
- **Sections:** no marketing hero. The first viewport is the working conversation surface with the supplied Royal Little Tiger logo in the page header, a low-weight desktop mascot cue, and three example requests.

### Spacing Overrides

- **Content Density:** High — optimize for information display

### Typography Overrides

- No overrides — use Master typography

### Color Overrides

- **Strategy:** use the supplied brand tokens: `#F3F5F7` page background, `#FFFFFF` surfaces, `#17202A` text, `#DDE2E7` separators, `#FABE00` brand yellow, `#632C16` brand brown, and `#16745B` status green. Yellow is reserved for selected state, focus, primary action, and the small brand cue.

### Component Overrides

- Avoid: Leave UI frozen with no feedback
- Avoid: Single row actions only
- Avoid: Auto-play high-res video loops

---

## Page-Specific Components

- **Task sidebar:** opaque task IDs and installation-scoped, screened display history. Preserve user questions, approved public tables and result states across refreshes.
- **Execution trace:** process stages only. Distinguish success, no data, rejected, failed and truncated results. Never display SQL, raw tool payloads, credentials or audit contents.
- **Composer:** single natural-language input with a clear read-only boundary.
- **Brand assets:** `frontend/assets/royal-tiger/royal-tiger-logo-banner.png` appears once in the sidebar header; `tiger-wave.png` is a low-opacity wide-screen cue and is hidden below 900px; `tiger-avatar.jpg` is used for the welcome brand avatar and assistant message avatars. No custom logo or mascot mark is drawn in CSS/SVG.

---

## Recommendations

- Effects: Hover tooltips, chart zoom on click, row highlighting on hover, smooth filter animations, data loading spinners
- Animation: Use skeleton screens or spinners
- Data Entry: Allow multi-select and bulk edit
- Sustainability: Click-to-play or pause when off-screen
- CTA Placement: Primary CTA in nav + After metrics
