# Calendar Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared custom calendar popover to the reimbursement date inputs, with a solid black circular selected day, while preserving the existing native date fields and ISO submission contract.

**Architecture:** `static/calendar.js` progressively enhances the 12 existing date inputs with one body-level, fixed-position popover. The native inputs remain the source of truth and remain usable as a fallback if the enhancement script is unavailable. `static/calendar.css` owns only the popover and date-input presentation, using the existing `--ui-*` tokens.

**Tech Stack:** Existing server-rendered HTML, vanilla JavaScript, CSS, Python `unittest` frontend contracts, and the current local browser smoke workflow.

---

### Task 1: Lock the resource and markup contract

**Files:**
- Modify: `templates/index.html:7-10,84` to load the calendar stylesheet and script after the existing component assets and `app.js`.
- Test: `tests/test_frontend.py` add assertions for the calendar assets and the unchanged 12 date inputs.

- [ ] **Step 1: Write the failing frontend contract test**

Add a test method that reads `index.html` and asserts `/static/calendar.css` and `/static/calendar.js` are present, then reads `static/calendar.js` and asserts the source contains `input[type="date"]`, `aria-haspopup`, `aria-selected`, `Date.UTC` is absent, and `new Date` is absent. Keep the existing exact `type="date"` and 11-row assertions unchanged.

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_index_loads_calendar_assets_and_preserves_date_contract -v
```

Expected: FAIL because the new asset links and `static/calendar.js` do not exist yet.

- [ ] **Step 3: Add the asset references**

Insert `<link rel="stylesheet" href="/static/calendar.css">` after `ui-components.css`, and append `<script src="/static/calendar.js" defer></script>` after the existing deferred scripts in `templates/index.html`. Do not alter any date input attributes.

- [ ] **Step 4: Run the focused test again**

Run the same command. Expected: the markup assertions pass and the source-file assertions remain failing until Task 2 creates the script; record the remaining failure as the expected handoff.

- [ ] **Step 5: Commit the contract-only change**

```bash
git add templates/index.html tests/test_frontend.py
git commit -m "test: define calendar control frontend contract"
```

### Task 2: Implement the shared calendar behavior

**Files:**
- Create: `static/calendar.js` containing the date parser, integer calendar math, popover renderer, keyboard handling, positioning, and input synchronization.
- Test: `tests/test_frontend.py` extend the contract test with behavior tokens and the exact ISO write-back contract.

- [ ] **Step 1: Write the failing behavior assertions**

Assert the script contains `Date.now()`, `formatToParts`, `role="grid"`, `role="gridcell"`, `aria-expanded`, `aria-controls`, `dispatchEvent`, `keydown`, `Escape`, `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown`, `Enter`, `Space`, `form.reset`, and `YYYY-MM-DD` construction. Also assert it does not contain `new Date` or locale-formatted values assigned to the input.

- [ ] **Step 2: Run the focused test to verify it fails**

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_index_loads_calendar_assets_and_preserves_date_contract -v
```

Expected: FAIL because `static/calendar.js` is not present.

- [ ] **Step 3: Write the minimal calendar implementation**

Implement one IIFE that:

1. Selects `#reimbursement-form input[type="date"]` and exits without changing anything when none exist.
2. Creates a single `div.date-picker-popover` with a header (previous button, month label, next button), weekday row, `role="grid"` body, and footer “今天” button. Use inline SVG `<use href="/static/icons.svg#chevron-left">`/`#chevron-right` only if those symbols exist; otherwise use accessible text labels on the buttons.
3. Parses only valid ISO values with `/^(\d{4})-(\d{2})-(\d{2})$/`, calculates leap years, month lengths, and Monday-first weekday indexes using integer arithmetic (no `new Date`). Derive today with `Intl.DateTimeFormat(...).formatToParts(Date.now())`.
4. Renders 42 stable cells with `data-date`, `aria-selected`, `tabindex`, `.is-selected`, `.is-today`, and `.is-outside-month` states. The selected date must use the input's exact ISO value.
5. On enhancement, sets `readOnly` and `aria-haspopup="dialog"`, creates a unique `aria-controls` id, and listens for `click`, `focus`, `keydown`, `input`, and `change`. Pointer activation prevents the native picker and opens the custom popover; keyboard Enter/Space opens it.
6. On selection, assigns the ISO string to the active input, dispatches bubbling `input` and `change` events, closes the popover, and restores focus to the active input. The form's existing `FormData` path therefore receives the same value.
7. Handles month navigation, grid arrow movement, Home/End, Enter/Space selection, Escape close, outside pointer close, and `reset` close/refresh. Position the popover with `getBoundingClientRect()` and clamp it inside the viewport, flipping above the input when below-space is insufficient.

- [ ] **Step 4: Run the focused contract test**

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_index_loads_calendar_assets_and_preserves_date_contract -v
```

Expected: PASS.

- [ ] **Step 5: Commit the behavior**

```bash
git add static/calendar.js tests/test_frontend.py
git commit -m "feat: add shared custom reimbursement calendar"
```

### Task 3: Match the current visual system

**Files:**
- Create: `static/calendar.css` for date-input affordances and the fixed popover.
- Test: `tests/test_frontend.py` add visual contract assertions.

- [ ] **Step 1: Write the failing visual assertions**

Assert the CSS contains `.date-picker-popover`, `position: fixed`, `grid-template-columns: repeat(7`, `border-radius: 50%`, `background: var(--ui-button-primary)`, `color: #fff`, `--ui-shadow-lg`, `prefers-reduced-motion`, and `prefers-reduced-transparency`.

- [ ] **Step 2: Run the focused test to verify it fails**

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_calendar_visual_contract -v
```

Expected: FAIL because `static/calendar.css` is not present.

- [ ] **Step 3: Add the CSS**

Style the popover as a 320px `position: fixed` surface with the existing `--ui-surface-strong`, `--ui-line`, `--ui-radius-md`, and `--ui-shadow-lg`; use a 7-column grid with 40px cells, 34px circular day buttons, black selected state, subtle blue hover/today states, visible keyboard focus, and a 36px mobile cell variant. Hide only the native calendar indicator after enhancement (`[data-calendar-enhanced]::-webkit-calendar-picker-indicator`) and leave unenhanced inputs untouched. Add reduced-motion and reduced-transparency overrides.

- [ ] **Step 4: Run the focused visual test**

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_calendar_visual_contract -v
```

Expected: PASS.

- [ ] **Step 5: Commit the visual layer**

```bash
git add static/calendar.css tests/test_frontend.py
git commit -m "style: match calendar popover to reimbursement UI"
```

### Task 4: Verify integration and produce the reference preview

**Files:**
- Modify only if needed: `static/calendar.js`, `static/calendar.css`, `templates/index.html`.
- Test: existing `tests/test_frontend.py`, `tests/test_server.py` frontend contract class, and browser smoke checks.

- [ ] **Step 1: Run the focused regression set**

```bash
python3 -m unittest tests.test_frontend tests.test_server.FrontendContractTest -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run static syntax and diff checks**

```bash
node --check static/calendar.js
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 3: Start the local app and inspect the reference states**

Use the repository's supported launcher with an unused `APP_PORT` if 8800 is occupied. Open the root form after setup/login and inspect: an empty top date, a selected top date, an empty first detail date, a selected detail date near the table edge, previous/next month, Escape/outside close, reset, and a 390px-wide viewport. Confirm the selected day is a black circle, the input still contains ISO `YYYY-MM-DD`, and no table overflow clips the popover.

- [ ] **Step 4: Run the complete regression suite**

```bash
python3 -m unittest discover -s tests -v
```

Expected: the existing suite passes with no new failures.

- [ ] **Step 5: Commit the verified integration**

```bash
git add static/calendar.js static/calendar.css templates/index.html tests/test_frontend.py
git commit -m "feat: ship redesigned reimbursement calendar control"
```
