# Edge Home · Design handoff

Everything a developer needs to build the Edge Home dashboard and app on the Obsidian Platinum system: the tokens, the component list and the rules the UI must follow.

| File | What it is |
| --- | --- |
| `design/tokens.css` | CSS custom properties for both themes, spacing, radius, sizes, motion, z-index, and `.type-*` classes. Generated, don't edit. |
| `design/tokens.handoff.json` | The same values as flat JSON (`color.dark`, `color.light`, `space`, `type`…), for Compose, React Native or anything that isn't CSS. |
| `design/tokens.json` | The source of truth, in the design-system format. Change values here, then regenerate the other two. |
| `design/DESIGN-SYSTEM.md` | The design principles and token usage notes. |

Screens: the Smart Home Screens canvas (login, devices, chat, confirmation, the three admin screens, edge states), each in desktop and phone, dark and light.

## 1. Tokens

### Theming

- Dark (Obsidian) is the default. Light (Ivory) follows the OS unless the user picks a theme in Settings.
- An explicit choice sets `data-theme="dark"` or `data-theme="light"` on the root element; with no attribute, `prefers-color-scheme` decides.
- Components read **only** semantic tokens (`--surface`, `--ink-2`, `--risk`…). `--platinum` and `--graphite` are brand colors for the icon and marketing, never UI.

### Color roles

| Role | Tokens | Use |
| --- | --- | --- |
| Ground | `bg` → `surface` → `surface-2` → `surface-3` | Page, cards, recessed wells, selected segment |
| Lines | `line`, `line-strong` | Decorative hairlines and card borders only (below 3:1) |
| Control edge | `control-line` | Edge of anything you operate: inputs, selects, off switch, checkbox, filter chip, secondary button (≥ 3:1) |
| Text | `ink`, `ink-2`, `ink-3` | Primary, secondary, captions. `ink-3` only on `bg` and `surface` |
| Accent | `accent`, `on-accent` | "On" state, primary button, selected tab. One primary button per view |
| Risk | `risk`, `risk-fill`, `on-risk`, `risk-soft`, `risk-line` | Locks, confirmations, destructive actions. Nothing else may use them |
| Signals | `ok`, `warn`, `info` + `-soft` | Done/settled, no result/pending, in progress/settling |
| Utility | `focus`, `scrim`, `shadow-float` | Focus ring, sheet backdrop, floating layers |

Every text pair passes WCAG AA (4.5:1) and every control edge, icon and focus ring passes 3:1 in both themes.

### Scale

| Group | Values |
| --- | --- |
| Space | 4, 8, 12, 16, 20, 24, 32, 48 px (`--space-1` … `--space-12`) |
| Radius | 8 chip · 12 button/tile · 18 card · 26 sheet · 999 pill |
| Type | display 40/44 (32/36 phone) · title 28/32 · numeral 44/44 · heading 17/24 · body 15/22 · label 14/20 · meta 13/18 · caption 12/16 · value 22/26 |
| Fonts | Instrument Serif 400 (`--font-display`); Inter 400 and 500 (`--font-sans`). Nothing smaller than 12 px, no other weights |
| Sizes | Tap target 44 px · icon tile 40 px · tablet ≥ 600 px · desktop ≥ 1024 px |
| Motion | 120 ms press · 180 ms switch · 240 ms sheet · ease-out. All 0 under reduced motion |
| Z-index | tab bar 10 · sheet 20 · toast 30 |

## 2. Component list

| Component | Variants and states | Key props / data |
| --- | --- | --- |
| Button | primary · secondary · ghost · danger · risk-outline; sizes 44 and 36 (`sm`, desktop only); icon-only | `label`, `icon`, `loading` |
| Switch | on · off · disabled (offline) | `checked`, `onChange`, `aria-label` = device name |
| Segmented | 2–4 options, one selected | `options`, `value` |
| Slider | brightness 1–100, position 0–100 step 5 | `value`, commit on release |
| Text field / Select | default · error (with message under) · disabled | `label` always visible |
| Checkbox row | on · off | Room scopes |
| DeviceCard | light · fan · outlet; on · off · offline · error · view-only | `name`, `room`, `state`, `capabilities`, `online` |
| MotionCard | settled · accepted · settling · no result | `position`, `target`, `phase` |
| ClimateCard | on (setpoint + mode) · off · offline · view-only | `target`, `current`, `mode`, `modes[]` |
| SensorCard | normal · stale | `readings[]`, `updatedAt` |
| LockCard | locked · unlocked · offline · view-only | `locked`, `since` |
| RoomHeader | member/admin · guest | `name`, summary counts |
| Room tabs / Side nav | selected · unselected; count text | rooms the user can see |
| Readings strip | 1–4 values | room sensors, AC target, blinds |
| ChatBubble | user · assistant · streaming (caret) | `text`, `streaming` |
| ToolChip | done · running · failed · needs confirmation; "+N more" after 4 | `device`, `result` (plain words) |
| ConfirmCard | pending · confirming · confirmed · cancelled · expired | `summary` (server text only) |
| Confirm sheet / dialog | phone sheet · desktop dialog | same as ConfirmCard |
| Composer | idle · streaming (Stop) · image/voice slots reserved | `placeholder` by role |
| StatusBadge | online · offline · settling · settled · no result · locked · unlocked · view only · pending · role | label + icon/dot |
| Banner | info (reconnecting) · warn (hub unreachable) · risk (error) | title + one sentence |
| Toast | success · failure | one line, 2.4 s |
| Skeleton | card, tab, title shapes | mirrors the real layout |
| Empty state | room · pending · filtered log · guest with no rooms | headline, one line, one verb button (admin only) |
| PendingNodeRow | default · field error | `label`, `capabilities[]`, `joinedAt`, name, room |
| UserRow / RoomRow | admin · member · guest (outlined initials) | `name`, `role`, `rooms[]`, `until` |
| Role editor | role segmented, room checklist, access end, live summary | Guest = rooms only, view-only |
| LogRow | kind · status · source · time · endpoint; risky kinds in `risk` | see rule 6 |
| Log filters | node · endpoint · user · status · time range; clear all | chips on desktop, sheet on phone |
| Load more | button + "Showing N of M" | cursor paging |
| Tab bar / Side nav | 2–4 items by role | Admin only for admins |

## 3. Rules developers must follow

1. **Risky devices look different.** Locks render only as `LockCard`: `risk-line` border, "Secured device" label, full-width action button. Never a switch. Unlock always goes through the server's confirmation. An unlocked lock washes its whole card `risk-soft`.
2. **Offline means hands off.** An offline device keeps its card but goes dashed and dimmed. Controls render `disabled`, `tabindex="-1"` and `pointer-events: none`. When the hub itself is unreachable, show one banner and treat every device as offline with its last known state and time.
3. **Guests never see a control that won't work.** Don't render it; don't render it disabled. Guests get state text and a "View only" badge. They get no All off, no Admin tab, and only the rooms in their scope; other rooms don't exist for them. The server still enforces this; the UI is not the guard.
4. **Never show secrets or internals.** No tokens, hashes, signatures, device keys, node IDs, MAC addresses, request IDs or raw tool calls. Not in cards, chips, logs, tooltips, error messages, toasts or the console in production. Name things by room and device. Unknown nodes are "New light" until an admin names them.
5. **Confirmation shows server text only.** The confirm card body is the server's `summary` string rendered as plain text. The confirmation handle stays in memory and is sent back on Confirm; it is never displayed. Buttons are always "Cancel" and "Confirm". After a decision the card collapses to one line and can't be pressed again.
6. **Action log rows** show kind, status, source and time, plus node and endpoint in plain names (Device command, Chat, Confirmation, Node approval, Node status, Users, Sign-in). Risky kinds (unlock) color their verb `risk`. Paging uses a server cursor; the button says "Load N more" with "Showing N of M". Filters persist in the URL query on web.
7. **Motion follows the server.** Show Accepted → Settling → Settled from server events. Never show the target as reached before "settled". On timeout show "No result" with the last known position and a Try again.
8. **Failures keep the old state.** A failed command reverts the control, says why in the card in one sentence, and offers Try again. No optimistic state that isn't rolled back.
9. **Status never relies on color alone.** Every badge pairs color with an icon or dot and a word.
10. **Tokens only.** No hex values in component code. Use `control-line` for control edges, `line`/`line-strong` only for decoration.
11. **Accessibility.** 44 px touch targets; visible 2 px `focus` ring with 2 px offset; real buttons, inputs and labels; icon-only buttons have `aria-label`; live regions for toasts and streaming replies; respect `prefers-reduced-motion`.
12. **Layout.** Phone < 600 px: room tabs and a bottom tab bar. Tablet 600–1023 px: two-column grid. Desktop ≥ 1024 px: side nav, grid, right rail. Keep a 16 px minimum gutter and respect safe-area insets.
13. **Copy.** Sentence case. Buttons start with a verb. Actions that lead to a confirmation end with "…". Errors say what happened and what to do. No "please", no "successfully".
14. **Reserved slots.** The composer keeps space for image and voice. Don't render those buttons until the features exist.

## 4. Roles at a glance

| Can… | Admin | Member | Guest |
| --- | --- | --- | --- |
| See rooms | All | All, or the rooms given | Only the rooms given |
| Change devices | Yes | Yes, in their rooms | No |
| Unlock (with confirmation) | Yes | Yes, in their rooms | No |
| Chat | Changes and questions | Changes and questions | Questions only |
| Admin area | Yes | No | No |
