# Nexus design and module standards

Status: active design contract  
Adopted: 2026-07-27  
Applies to: the dashboard, new dashboard modules, and shared Nexus interface features

This document is the durable reference for extending Nexus without
reintroducing visual drift. Existing modules establish the baseline:

The implementation source of truth remains the repository. Shared visual tokens
and dashboard module rules live in `static/dashboard.css`; reusable navigation
and interface primitives live in `static/nexus.css`; dashboard structure lives
in `templates/dashboard.html`. When an intentional design change lands, update
this document in the same commit.

### Global application-shell invariant

The dashboard chrome is the only top-level navigation shell. Every adopted
route renders `templates/_app_shell.html::dashboard_chrome` inside
`.nexus.nexus-app-frame`, followed by `content_header` and then its page body.
The shell owns the device-frame width, product wordmark, health eye, scan and
clock cluster, settings, notifications, warning ticker, palette, responsive
breakpoints, and primary navigation. A page may choose the active destination;
it may not fork the shell's geometry or colors. Route-specific titles and
freshness labels always sit below the navigation.

The shell receives cached local state through `app/shell_context.py`. Rendering
a page must never turn the shared header into a new fleet probe path.

## 1. Naming and language

- Visible module headings are lowercase, including product and proper names:
- Use short, concrete nouns or noun phrases. Avoid implementation names,
  hostnames, acronyms, and internal service names in headings unless that is
  what the user recognizes.
- A module subtitle explains the signal in two or three words. Render it with
  `.sub.rune`; examples are `pull-model heartbeat`, `relay runs`,
  `rebuild health`, and `showtime watch`.
- Keep route names, Python identifiers, CSS compatibility classes, and database
  names stable unless a functional rename is separately required. Visible
  language does not force an internal migration.
- Buttons and compact actions use lowercase labels, such as `more →`.

## 2. Module anatomy

Every primary dashboard module uses this order:

1. `.section-title`
   - one semantic `h2`;
   - one `.sub.rune` translation;
   - optionally one `.navlink` action.
2. `.section-rule`
3. one bounded body policy:
   - `.jh-scroll` for evidence that must remain locally inspectable; or
   - `.jh-module-fit` for a glance summary that adapts to the fixed body.

The shared implementation primitive is
`templates/_module_shell.html::module_shell`. Primary dashboard modules must
call that macro rather than duplicate the shell markup. The caller supplies
only the title, subtitle, optional class/detail link, body class/ID/ARIA
attributes, and bounded body content.

Reference call:

```jinja
{% call module_shell("module name", "signal description",
                     class_name="jh-example", href="/example") %}
  <!-- bounded module content -->
{% endcall %}
```

Omit `more →` when no meaningful detail view exists. Do not add a disabled or
decorative action merely to fill the header.

## 3. Desktop geometry

At widths of 901px and above, every `.jh-col` uses the same three-row grid:

| Region | Height | Purpose |
|---|---:|---|
| Header | 64px | heading, rune subtitle, optional action |
| Divider | 31px | 10px top margin, rule, 20px bottom margin |
| Body | 284px | exactly three 88px Jobs cards plus two 10px gaps |
| Total | 379px | identical top and bottom bounds for every module |

The shared body token is `--module-h: 284px`. Do not introduce a
module-specific desktop height. Evidence may scroll inside `.jh-scroll`.
Fitted summaries never scroll: reduce spacing, clamp secondary prose, or cap
newest-first repeated rows while retaining a `more →` route to the full view.
Both policies retain the same frame and bottom rule.

The wide four-column ratio is:

```css
grid-template-columns: 1.3fr 1fr .8fr .8fr;
gap: 22px;
```

This preserves more room for Jobs Activity and Worker Activity while giving compact status
modules equal trailing shares. Adding a fifth peer module requires an explicit
layout review; do not silently compress all existing columns.

## 4. Typography

Nexus typography has four intentional roles, all sourced from
`static/nexus.css`:

| Role | Typeface | Size | Weight / line height |
|---|---|---:|---|
| Module heading | Fraunces | 25px desktop | 900 / 1.05 |
| Rune subtitle | Fleet, JetBrains Mono fallback | 11px | 700 / 1.2 |
| Primary body content | System UI | 13–16px | 500–700 / 1.35–1.55 |
| Supporting body content | System UI | 11–13px | normal / 1.4–1.55 |
| Metrics, IDs, timestamps, evidence | JetBrains Mono | 9–13px | 500–700 / 1.2–1.45 |
| Header action | JetBrains Mono | 10px | inherited / 1.2 |

Use `--font-display`, `--font-ui`, and `--font-mono`; do not repeat font stacks
in route styles. Fleet is decorative and never carries readable prose or
data. Plain-language labels and summaries use the interface face; only exact
machine evidence uses mono. All peer modules use these roles regardless of
whether they contain a list, metrics, artwork, or a third-party product name.
Status chips retain the shared `.chip` treatment.

Module headings may scale through the existing mobile rule, but all headings at
the same breakpoint must resolve to the same computed size.

## 5. Header alignment and actions

- The heading occupies grid row 1 and may span the header width.
- The rune subtitle occupies row 2, column 1.
- `more →` occupies row 2, column 2 so it is inline with the translation, not
  vertically centered across the entire header.
- Apply the shared small optical offset (`translateY(2px)`) to place the action
  slightly below the subtitle baseline.
- Header actions use `padding: 3px 8px`; do not use a full-size primary button
  inside a module heading.
- Long headings truncate with an ellipsis on desktop rather than wrapping and
  changing the module's vertical bounds.

## 6. Body hierarchy

- Primary content identifies the thing being reported: job name, relay token,
  metric value, or watched title.
- Supporting content carries host, seat, age, timestamps, counts, and
  explanatory details.
- Use dotted separators for repeated rows and the shared solid bottom rule for
  the module boundary.
- Keep content bounded. A dashboard summary should lead to a detail surface
  rather than reproduce it.
- Fitted summary modules must not hide information without a full-view route.
  Health compact their dashboard projections without a nested scrollbar.
- Artwork is supplementary. It must not determine typography or module height.
- Empty states use the shared `.empty` treatment and must explain what signal is
  absent.

## 7. State, color, and motion

- Reuse Nexus palette variables and established semantic classes. Do not invent
  a new color for a new module.
- `static/nexus.css` owns all canonical surface, text, border, accent, status,
  and provider tokens. Route styles may alias them but may not replace their
  literal values.
- Small readable metadata uses `--text-metadata` / `--ink-dim`. Reserve
  `--rune-dim` for decorative unknown-state marks, not body copy.
- `developed` / cyan: healthy, complete, or available.
- `safelight` / amber: warning, delayed, or needs attention.
- `overexposed` / red: failed or critical.
- `unexposed` / dim: unknown, ended, unavailable, or never run.
- State must be expressed in text as well as color.
- Reuse `.chip` for compact state labels.
- Motion communicates liveness or transition; it is never required to read
  status. Honor `prefers-reduced-motion`.

## 8. Responsive behavior

- Above 1200px: use the established four-column ratio.
- From 901px through 1200px: use two columns; compact trailing modules may span
  the full row according to the current layout rules.
- At 900px and below: stack modules in one column.
- At the stacked breakpoint, remove fixed body height and internal scrolling:
  content returns to natural height with visible overflow.
- The page must have no horizontal document overflow at 390px viewport width.
- Mobile changes must not alter desktop module geometry, and desktop
  standardization must not truncate mobile content.

## 9. Data and interaction boundaries

- The dashboard renders prepared state; it does not perform fleet probes while
  rendering HTML.
- New collectors write or cache state through the existing application seams.
- Local consumers of Tower's semantic index use the normalized
  `~/.local/share/tower/index/vault.db` path (or the
  `TOWER_INDEX_PATH` override), never the retired repository-local
  `~/tower/index/vault.db` path.
- A heartbeat with a stable `ended_at` and an explicit stopped/cancelled state
  is terminal. Its old heartbeat age must not be reclassified as an active
  stall in Activity or Fleet Status.
- Fleet sweeps bound concurrent SSH handshakes per target and retry only
  transport/session failures once. A negative remote-command result is real
  evidence and must remain immediate; it is never converted into a retry.
- Collectors sharing the five-minute fleet cadence belong in
  `HeartbeatRunner.run()` so scheduler single-flight, timeout ownership, and
  one bounded sweep remain centralized. Do not register a second scheduler job
  for an extra liveness probe unless its cadence or safety contract genuinely
  differs and the exception is documented.
- A `more →` link navigates to a useful, stable detail route.
- Use semantic `section`, `h2`, list, link, and button elements. Preserve
  keyboard focus visibility and at least the established mobile touch target
  behavior.
- Do not expose credentials, private API payloads, raw transcripts, or
  uncontrolled logs in a module.

## 10. Implementation checklist

Before merging a new module or changing an existing one:

- [ ] Heading is lowercase and concise.
- [ ] Header contains `h2`, `.sub.rune`, and only a meaningful optional action.
- [ ] The module calls `module_shell` and therefore receives `.jh-col`,
      `.section-title`, `.section-rule`, and one declared body policy from one
      shared primitive.
- [ ] Any new five-minute liveness collector is folded into the central
      heartbeat sweep rather than creating an independent scheduler job.
- [ ] Desktop rows resolve to 64px / 31px / 284px.
- [ ] Heading, primary, supporting, and action text match the shared computed
      typography.
- [ ] `more →` is aligned with the rune subtitle and uses the optical offset.
- [ ] The desktop body deliberately uses `.jh-scroll` or `.jh-module-fit`.
- [ ] A fitted body has no nested scrollbar and any row cap matches its live
      refresh path.
- [ ] Mobile content returns to natural height at 900px and below.
- [ ] 390px viewport has no horizontal document overflow.
- [ ] Status uses existing semantic colors, text labels, and `.chip`.
- [ ] Active worker model badges fit the shared compact geometry and truncate
      long model names without covering the seat header or status copy.
- [ ] Empty, loading, unknown, warning, and failure states are represented.
- [ ] Browser verification measures all peer modules, not only the new one.
- [ ] `git diff --check` passes and the scoped source mutation is committed.
- [ ] This document is updated if the shared contract intentionally changes.

## 11. Required visual verification

For desktop, inspect all peer modules in the same rendered frame and verify:

- identical header, divider, body, and total section bounds;
- identical computed heading family and size;
- identical computed primary and supporting content scales;
- action/subtitle vertical alignment;
- no clipped title, rule, action, or state chip.

For mobile, verify at 390×844:

- document `scrollWidth` equals `clientWidth`;
- headings share one computed size;
- bodies are auto-height with visible overflow;
- links, disclosures, and actions remain usable.

Service health and a successful HTTP response are necessary but do not prove
visual conformance. Use an authenticated browser measurement and screenshot for
every shared-layout change.

## 12. Analytics surfaces

Dense comparative views such as Fleet Activity use one compact analytics body
instead of a stack of full-page report panels or a second layer of page chrome.
The reference interaction model is Claude's activity view, translated into
Nexus's palette and data semantics:

- use the shared parent/lens header above the analytics body; place Activity's
  view tabs in that shared row and keep lens-specific range controls aligned to
  the body at upper right;
- use an eight-cell, four-by-two overview grid on desktop and two columns on
  mobile;
- keep metric labels above values; use system UI for labels and controls, and
  JetBrains Mono for the metric values themselves;
- show contribution history as a seven-row calendar grid with unavailable
  collection history visually distinct from verified zero-activity days;
- show model/provider history as compact stacked daily bars with a simple axis
  and legend rows containing provider, total, sessions/days, and share;
- keep caveats in a small footer note rather than a large explanatory block;
- place verbose event history in a collapsed secondary drawer so the primary
  view retains the compact analytics-card silhouette;
- retain Nexus semantic colors, focus states, truthful `N/A` handling, and the
  no-horizontal-overflow mobile requirement.

An analytics redesign must preserve the underlying metric definitions. Visual
similarity to an external reference does not authorize relabeling assistant
turns as tokens, treating unavailable provider history as zero, or fabricating
activity outside the collected date range.

### Persistent quota telemetry

Model Usage is the Models lens within Activity, reached from the dashboard
strip's `more →` action. Its compact dashboard strip shows current quota;
`/activity?tab=models` owns quota history, comparison, anomaly inspection, and
the stacked daily assistant-turn graph. `/model-usage` is a compatibility
redirect and does not appear in primary navigation.

The dashboard projection is one full-width outer card immediately above the
worker row. On desktop it uses four equal columns aligned exactly with the four
worker cards below:

1. worker routing tiers above Charlie;
2. Claude quota above Delta;
3. Codex quota above Alpha;
4. Gemini quota above Localworker.

Each provider column shows `5 hour` and `weekly` percent-used bars, an honest
`unavailable` state when a window is absent, freshness, and the sanitized source
label. The routing column shows worker candidates only, ordered GREEN, YELLOW,
then RED and by descending router score within each tier. The selected worker
recommendation is highlighted. Column alignment is visual; it does not imply
that a card is bound to the provider directly above it.
Provider identities retain Claude orange, Codex cyan, and Gemini blue.

Localworker is a full peer worker card, never nested beneath a cloud node or substituted
by the Model Usage surface. The worker row order is always Charlie, Delta, Alpha,
Localworker. The first three titles are physical node names; retired worker identities
remain read-only source aliases and never appear as card labels or subtitles. All
four cards show the current task. Charlie, Delta, and Alpha show the active run's
actual provider/model badge only while working. Localworker always shows its fixed open
model badge (currently `GPT-OSS 20B`), including while idle. At the tablet breakpoint
both the usage strip and worker row become two-by-two grids. At the phone breakpoint
both stack in the same logical order with no horizontal overflow.

- Sample every provider on the existing five-minute collector cadence.
- Store history in one Alpha-local WAL-mode SQLite database, not Syncthing or
  the vault Git repository.
- Normalize provider output into provider samples and named quota windows;
  preserve the source and fallback class, never credentials, raw endpoint
  responses, prompts, conversation state, or terminal output.
- Track reset reanchors, rollovers, availability transitions, source changes,
  and fallback use in a separate append-only event ledger.
- Missing telemetry is `unavailable`, never zero usage.
- Retain normalized history indefinitely while it remains small; downsample API
  responses by range so the browser never receives an unbounded five-minute
  series.
- Use Claude orange, Codex cyan, and Gemini blue consistently. Current-state
  cards and historical plots share those provider identities.
- Keep verbose quota events in a collapsed drawer. The primary page retains the
  standard compact eight-metric grid, current provider row, and bounded charts.
- All range/provider controls are keyboard accessible and the page must have no
  horizontal overflow at 390×844.
- Quota notifications reuse Nexus's single notification router. Baseline
  existing history on first activation, persist an exact event-id watermark,
  and give every reset a stable `model-usage-event:<id>` dedup key.
- Notify only for confirmed `five_hour`, `weekly`, or `fable_weekly` window
  rollovers and for an aggregate tracker transition from usable to completely
  unavailable. A tracker loss means the newest capture has zero usable
  providers or the collector is more than 15 minutes stale.
- Recovery is intentionally silent. Reset reanchors, usage drops,
  provider-specific availability edges, source changes, and fallback changes
  remain queryable in Model Usage history but never create notification-feed
  rows or PWA pushes.

The main dashboard may project a multi-view analytics surface as separate peer
modules when both views are independently useful at a glance. Each projection:

- receives its own lowercase module heading and rune subtitle;
- follows the standard 64px / 31px / 284px desktop module geometry;
- renders a bounded summary rather than embedding tabs or the entire full-page
  analytics card;
- shares one aggregate fetch and the dashboard's central visibility-aware
  polling cadence;
- links its `more →` action directly to the corresponding full view;
- stacks with natural height at the established mobile breakpoint.

## 13. Worker model badges

Runtime model identity is a compact status annotation, not a second card
heading. Apply the shared `.seat-model-badge` treatment to active Charlie, Delta,
and Alpha runs. Localworker is the deliberate exception: because its model is fixed,
its card keeps the same badge visible while idle as well as while active.

- Desktop badge geometry is 22px high with a 100px maximum width.
- At 760px and below, use 20px high with a 90px maximum width.
- Keep the provider mark at 10px desktop and 9px mobile.
- Keep the model label at 8px desktop and 7px mobile.
- Long labels remain one line and truncate with an ellipsis.
- Reserve enough right padding in `.seat-head` that the badge cannot cover the
  seat name or host subtitle.
- Prevent mobile text autosizing inside the badge so iOS cannot inflate it
  independently of the worker-card typography.
- Do not reduce the badge by clipping the provider mark, removing the visible
  model family, or allowing it to overlap run status.

## 14. Worker Activity transcript parity

- Worker Activity accepts structured `.json` transcripts from Claude, Codex, and
  Gemini workers.
- Gemini workers launch with `--output-format stream-json`; plain-text
  output is a legacy compatibility format, not the normal path.
- Gemini session, assistant-response, tool-use, tool-result, and final-result
  records map onto the same Worker Activity components used by other providers.
- Codex thread, agent-message, command-execution, reasoning, and file-change
  records map onto those same components; a saved Codex JSONL file must never
  silently render as a zero-event transcript.
- Legacy Gemini `.txt` transcripts remain readable as a bounded final-result view
  so historical links never display a false “no transcript” message.
- Transcript resolution remains token-validated and restricted to canonical
  per-seat transcript directories; adding a format never widens path access.
- Every cloud-worker provider must retain the immutable request, routing decision,
  structured transcript, per-run response, latest-response projection, append-only
  response history, completion sentinel, and supplied session-note execution log.

## 15. Fleet conformance surfaces

- Conformance probes run only in the offline collector timer; request handlers
  read a local cache and never initiate SSH, systemd, or filesystem probes.
- The check inventory is a tracked declarative manifest. Adding a check requires
  an explicit bounded collector implementation and a test; manifest text is
  never evaluated by a shell.
- Evidence is sanitized and bounded. Never store private keys, tokens, cookies,
  environment dumps, credential files, or conversation content.
- States are `ok`, `warning`, `error`, or `unknown`. Probe failure is not
  silently converted to compliant state, and drift never triggers auto-repair.
- The dashboard projection remains compact; exact evidence and timestamps live
  on the dedicated lowercase `conformance` page.
- The cache carries the manifest revision and check-set fingerprint. A policy
  migration restarts stability wording without masquerading as a fleet-state
  transition.
- Removed checks are retired in the persistent watcher watermark without a
  fabricated recovery notification; reintroduced checks seed silently.
- Categories and human metadata come from the tracked manifest. Unknown types
  never silently fall into the Services category.

## 16. Indexes

- `/indexes` is the authenticated, read-only projection of the five
  central indexes. It is not a generic Vault browser or Markdown renderer.
- `/control-plane` is a compatibility redirect and must not appear in newly
  generated user-facing links or labels.
- The scheduled conformance collector writes `state/control-plane.json` before
  collecting governance checks. Request handlers read only that local cache.
- Only the allowlisted `fleet-index.md`, `roadmap-index.md`,
  `conventions-index.md`, `instructions-index.md`, and `automation-index.md`
  files and their deterministic validators may enter the cache.
- Detailed instruction and automation results remain drill-down evidence;
  Conformance receives one aggregate row per central index so its dashboard
  stays operationally legible.
- The dashboard summary and dedicated page use shared Nexus tokens, lowercase
  headings, bounded scroll regions, and responsive one/two/five-column layouts.
  No Indexes page action edits the Vault or repairs drift.

## 17. Global navigation, page headers, and icons

- `templates/_app_shell.html` is the shared primitive for the primary
  navigation, subpage header, and interface icons. Do not duplicate its route
  list or draw page-local alternatives for an adopted surface.
- Primary navigation uses visible labels in this order: dashboard, activity,
  operations, CLI control.
  The order follows user tasks and remains identical on every adopted page.
- The current destination uses `aria-current="page"` and a cyan active state.
  Color is supplementary; the visible page title and active label carry the
  location in text.
- Subpages use the shared eye home link, product eyebrow, visible page title,
  optional freshness/status label, and primary navigation. Do not add a second
  generic back button to an adopted top-level page.
- Interface icons use the shared 24×24 outline set: 1.8px rounded strokes,
  current-color rendering, and no emoji. Primary navigation always pairs an
  icon with a visible label; tooltip-only navigation is not permitted.
- Notifications is a utility workspace reached from the persistent bell and
  does not appear as a peer destination in primary navigation. The bell always
  links to `/notifications` and carries its unread badge; it uses the cyan
  active state on that page.
- `/notifications` owns Inbox and Preferences lenses using the same shared tab
  primitive as Activity and Operations. Push enablement and the iPhone, iPad,
  and MacBook device picker live under Preferences. There is no separate
  settings control or settings page; `/feed` and `/settings` are compatibility
  redirects only.
- The notification Inbox groups events by user intent, omits empty groups, and
  promotes unread warnings or failures into one Needs Attention group without
  duplicating them. Group cards behave as an accessible single-view tablist:
  selection swaps the panel directly below, updates the `group` query parameter
  in place, supports arrow/Home/End keys, and never performs anchor scrolling.
  Rows lead with a human outcome, subject, source, and relative Central time.
  Raw channel, priority, exact timestamp, event ID, and unmodified evidence
  belong in an expandable Event Details region.
- At narrow widths, the primary navigation scrolls horizontally as one stable
  ordered row. Labels remain visible and the document itself must not overflow.
- The identity/status row and primary navigation are one persistent glass pane
  on desktop and mobile. The shared pane uses `position: sticky`, translucent
  blur, and remains in normal flow while content scrolls behind it. Do not make
  the page shell a nested vertical scroller; the document owns vertical scroll.
- Device-frame clipping uses `overflow: clip`, not `overflow: hidden`, so the
  frame preserves rounded clipping without becoming the sticky pane's scroll
  container.
- `design-index.md` owns the implementation source map and page-adoption
  matrix. Update it in the same commit when a page adopts or leaves a shared
  primitive.

### Operations information architecture

- `/operations` is the only primary-navigation destination for operational
  evidence. It defaults to Health and exposes Health, Conformance, Watchdogs,
  and Indexes as one shared, ordered local tab row.
- Operations lenses use canonical URLs of the form `/operations?tab=<lens>`.
  `/health`, `/conformance`, `/watchdogs`, `/indexes`, and `/control-plane`
  remain redirects for existing bookmarks and must not be generated by new UI.
- The shared Operations header, local tabs, and lens title come from
  `templates/_app_shell.html`; individual templates own only their evidence
  body and lens-specific assets.
- The Operations tab order is Health, Conformance, Watchdogs, Indexes on every
  viewport. It may scroll horizontally on narrow screens but may not wrap or
  reorder.
- Watchdogs is an Operations lens, not a wiki surface. Its page body uses the
  shared system UI family and application ambient palette; reserve monospace
  for technical targets and evidence fields rather than the whole page.

## 18. Activity information architecture

- Activity owns four top-level views in this order: Commits, Models, Workers,
  Jobs. Models contains current quota, quota history and events, followed by
  comparable provider activity over time; it replaces the former standalone
  historical-only Models view.
  Workers is the user-facing name for relay-run transcript
  history; do not restore the former Hero's Path label.
- Activity uses the same `nexus-lens-tabs`, `nexus-lens-tab`, and
  `nexus-lens-head` primitives as Operations. Every Activity view has an icon,
  one cyan outlined active tab, and a visible lowercase lens title above its
  evidence body. Do not restore the former rounded tab strip inside the
  analytics card.
- The Activity tab order is Commits, Models, Workers, Jobs on every viewport.
  The row scrolls horizontally rather than wrapping or shrinking labels on
  narrow screens.
- The worker list is the `workers` tab on `/activity`. Transcript detail lives
  under `/activity/workers/<token>` and keeps Activity selected in the global
  navigation.
- `/hero-path`, `/hero-path/<token>`, and `/hero-path/ws` are compatibility
  routes only. New UI, notifications, and generated links use Activity paths.
- Activity-level range controls and the Recent Activity drawer apply to Commits
  only. Models owns separate quota-history and assistant-turn ranges. Hide the
  Activity-level controls and drawer while Models, Workers, or Jobs is selected.

## 19. CLI control information architecture

- `/control` is the provider-neutral authenticated CLI workspace. The former
  `/gemini` destination redirects to its Gemini selection.
- Selection order is provider (Claude, Codex, Gemini), mode (Strategy,
  Worker), then host (Alpha, Charlie, Delta). Do not collapse provider and mode
  into ambiguous host cards.
- Strategy sessions use the installed premium provider profile and native
  interactive approval behavior. Worker runs use the installed worker profile,
  require a bounded prompt plus explicit confirmation, and retain a timeout.
- Merely loading the page or changing provider, mode, or host never starts a
  CLI. `Connect` and `Launch worker` are the explicit execution boundaries.
- URL state uses `provider`, `mode`, and `host` query parameters so a selected
  workspace can be bookmarked without auto-launching it.
- Strategy mode defaults to a chat-shaped control surface while retaining a
  one-tap Terminal view as the complete native compatibility boundary. Chat is
  a presentation layer over the same authenticated PTY and WebSocket; it must
  not launch a second provider session or claim cleaner semantics than the CLI
  exposes.
- The chat composer is a native multiline `textarea`, not xterm's hidden input
  and not a `contenteditable` substitute. Return inserts a newline, Cmd/Ctrl +
  Return sends, and a visible 44px-or-larger send control remains available to
  touch and switch-control users. iOS dictation, selection, paste, composition
  events, `visualViewport`, dynamic viewport height, and safe-area insets are
  first-class requirements.
- Typing `/` or using the slash control opens provider-aware command
  suggestions. Suggestions are conveniences only: arbitrary slash commands
  still pass verbatim into the connected CLI, and Terminal view remains
  available for native menus, approvals, and commands that need cursor-key
  interaction.
- Model choices are allowlisted by the server. Selecting one sends the
  provider's native live-session model command; it never rewrites the durable
  premium launcher pin. Providers with no approved alternate expose the pinned
  model as read-only rather than inventing a downgrade path.
- Claude's Auto control cycles the native interactive permission mode and
  reflects only a mode observed in terminal state. It never maps to
  `--dangerously-skip-permissions`, `bypassPermissions`, a worker permission
  profile, or Tower's provider auto-routing. If Nexus cannot confirm the mode,
  it opens Terminal view instead of claiming Auto is active.
- On phone widths, provider, mode, and host choices compress into three compact
  equal-width touch rows; decorative page chrome and explanatory desktop prose
  yield to the session workspace. The global header scrolls away on this route,
  the model/Auto controls and Chat/Terminal switcher occupy deliberate rows
  without clipping, and connecting or changing views focuses the session.
- The conversation owns a bounded dynamic-viewport region with internal message
  scrolling. The native composer stays above the software keyboard without
  covering transcript content, and the document has no horizontal overflow.
