# Nexus design index

Status: active implementation index
Established: 2026-08-01
Canonical design contract: [`PANEL-STANDARDIZATION.md`](PANEL-STANDARDIZATION.md)

This file maps Nexus's shared design language to its implementation and records
which pages have adopted it. It is an application-source index, not a sixth
fleet-governance index.

## Product language

Nexus is a calm fleet instrument: technical, legible, and unmistakably
Fleet without turning every control into ornament. The eye, device frame,
runic eyebrow, cyan signal color, Fraunces display hierarchy, readable system
UI copy, and mono evidence text form the signature. Navigation and actions use
plain language first.

### Nexus-wide type and color contract

`static/nexus.css` is the only source of truth for the adopted product's
type families and semantic colors. Route styles may define aliases for local
component names, but may not introduce a parallel page palette.

| Role | Canonical token | Use |
|---|---|---|
| Display | `--font-display` | Product name, page and lens titles, major module headings |
| Interface | `--font-ui` | Navigation labels, summaries, controls, notification copy, readable prose |
| Evidence | `--font-mono` | Timestamps, IDs, metrics, terminal output, technical evidence |
| Ornament | `nexus` / `.rune` | Decorative eyebrows and runic subtitles only |

The semantic surface, text, border, accent, status, and provider aliases also
live in `static/nexus.css`. Small metadata uses `--text-metadata`; decorative
`--rune-dim` is not a substitute for readable text. Provider colors remain
distinct only where provider identity is the information being encoded.

Design priorities, in order:

1. The user can name where they are and where each navigation item goes.
2. State is readable in text and does not depend on color or motion.
3. Repeated controls share one component, icon family, geometry, and focus state.
4. Dense evidence remains compact without becoming cryptic.
5. Decorative Fleet language supports hierarchy rather than competing with it.

## Source map

| Concern | Implementation source |
|---|---|
| Typography, semantic palette, motion, shared page shell, global navigation | `static/nexus.css` |
| Dashboard chrome, global navigation, content headers, line icons | `templates/_app_shell.html` |
| Live chrome context and utility behavior | `app/shell_context.py`, `static/app_shell.js` |
| Notification inbox, delivery preferences, and bell state | `templates/notifications.html`, `static/notifications.css`, `static/notifications.js` |
| Dashboard composition and fleet modules | `templates/dashboard.html`, `static/dashboard.css` |
| Dashboard module primitive | `templates/_module_shell.html` |
| Analytics controls and cards | `static/activity.css`, `static/model_usage.css` |
| Provider-neutral CLI workspace | `templates/gemini.html`, `static/gemini.css`, `static/gemini.js` |
| Exact reusable-component and responsive rules | `PANEL-STANDARDIZATION.md` |

## Navigation taxonomy

The primary navigation is ordered by task, not implementation history:

| Destination | User question | Icon concept |
|---|---|---|
| dashboard | What needs attention now? | four-panel overview |
| activity | What changed across commits, models, workers, and jobs—and how much model capacity remains? | vertical history bars |
| operations | What is the fleet's health, conformance, protective coverage, and canonical source state? | command wheel |
| CLI control | Where do I open a Claude, Codex, or Gemini strategy or worker session? | terminal prompt |

Notifications is a utility workspace reached from the persistent bell rather
than a peer primary-navigation destination. `/notifications` contains two
shared lens tabs: Inbox and Preferences. The former settings control and page
are retired; push-delivery setup now lives at
`/notifications?tab=preferences`. `/feed` and `/settings` are compatibility
redirects only.

The Inbox is grouped by user intent: Needs Attention, Worker Activity, Model
Usage, Fleet Operations, Jobs and Scans, and Other Updates. Empty groups are
omitted. An unread warning or failure is promoted into Needs Attention instead
of being duplicated in its source group; read warnings remain with their
operational history. The visible group cards are a single-view picker: one
group panel is rendered beneath them at a time, selection updates the `group`
query parameter in place, and selecting a group never scrolls or jumps the
page. Every row leads with a plain-language outcome, subject, source, and
relative Central time. Transport channel, numeric priority, exact timestamp,
original evidence, and durable event ID remain available under the row's Event
Details disclosure rather than competing with the summary.

### Activity views

Activity is the parent information space for four peer lenses:

1. **Commits** — commits, pushes, repositories, and contribution history.
2. **Models** — current quota, quota history/events, and comparable provider activity over time.
3. **Workers** — relay runs and their recorded transcript/tool trails.
4. **Jobs** — heartbeat-backed live and recent fleet jobs.

Activity and Operations share one parent/lens visual primitive from
`templates/_app_shell.html`: icon-and-label tabs, a cyan outlined active state,
a full-width divider, and a lowercase lens title. Activity's interactive tabs
update the query string in place; Operations links render separate cached
projections. Both keep the same geometry and horizontally scroll at narrow
widths.

The worker-run list lives at `/activity?tab=workers`; individual transcripts use
`/activity/workers/<token>`. Historical `/hero-path` URLs are redirects only and
must not reappear as labels or newly generated links.

The full job list lives at `/activity?tab=jobs`; `/activity/jobs` and the
historical `/jobs` index redirect there. Job detail routes remain `/jobs/<id>`
until the detail-sheet family is migrated as one verified unit. The dashboard
module is visibly named `jobs activity` and links to the Activity lens.

The full model-usage surface lives at `/activity?tab=models`. It combines live
provider capacity and quota history with the former Models lens's stacked daily
assistant-turn graph at the bottom. `/model-usage` is a compatibility redirect;
dashboard links and quota notifications use the canonical Activity URL.

### Operations views

Operations is the parent information space for four read-only fleet lenses:

1. **Health** — current and 24-hour sampled system state.
2. **Conformance** — declared contracts, drift checks, and recent scan history.
3. **Watchdogs** — registered protective mechanisms and their sampled evidence.
4. **Indexes** — canonical fleet sources, ownership, and validator state.

The canonical workspace is `/operations`; Health is the default lens. The
other views use `?tab=conformance`, `?tab=watchdogs`, and `?tab=indexes`.
Historical `/health`, `/conformance`, `/watchdogs`, `/indexes`, and
`/control-plane` URLs are compatibility redirects only. Dashboard modules,
notifications, and newly generated links use the canonical Operations URLs.

Watchdogs uses the standard application background and system UI typography
shared by the other Operations lenses. Its accordion may use monospace for
technical targets and evidence, but it must not inherit the legacy
`wiki-body` typography or its alternate ambient gradient.

### Dashboard module topology

Dashboard summaries use the shared module shell in equal-width, equal-height
two-column grids. Activity's four peers are Worker Activity, Jobs Activity,
Commit Activity, and Model Activity. The operational grid mirrors that geometry
with Indexes, Watchdogs, Health, and Conformance. A dashboard module
is a bounded projection and always links to its Operations lens; it must not
duplicate a collector, probe, registry, or authoritative full-page renderer.

On desktop, module bodies share the `--module-h` height and declare one of two
policies. Evidence modules use `.jh-scroll`; glance summaries use
`.jh-module-fit` and must reflow, clamp secondary prose, or apply an explicit
newest-first render cap rather than create nested scrolling. Compendium,
shows five recent runs and links to the complete Activity lens. At 900px and
below, both policies return to natural height so nested vertical scrolling is
never required on touch devices.

At phone width, the four primary destinations and every four-lens Activity or
Operations switcher fit as equal-width grids. They never auto-scroll the active
item into the center because doing so hides earlier navigation and makes the
shared shell appear horizontally displaced. Page status metadata occupies its
own full-width row beneath the title rather than competing with it.

On every phone route, including Dashboard, the Scan and live-time utilities use
the compact shared-shell scale: 34px visible Scan height with a 44px effective
touch target, and a 9px mono clock. Together they stay subordinate to the
wordmark and fit on its row whenever the available width permits.

### Control hierarchy

`/control` exposes three explicit choices in this order: provider, mode, host.
Provider is Claude, Codex, or Gemini; mode is Strategy or Worker; host is
Alpha, Charlie, or Delta. Strategy opens a native interactive CLI with its
provider-specific premium profile and normal approval behavior. Worker launches
a bounded, confirmed one-shot run with the provider's worker model. Viewing or
changing a picker never launches a session; `Connect` or `Launch worker` is the
explicit execution boundary. `/gemini` is a compatibility redirect to
`/control?provider=gemini`.

Strategy mode presents the connected PTY as a chat-shaped workspace by default:
a native multiline composer, provider-aware slash suggestions, a server-
allowlisted live model selector, Claude's native Auto-mode control, and a
Terminal view that preserves the complete interactive CLI. The chat transcript
is a readable projection of terminal state, not a second conversation process.
On phones, its composer follows `visualViewport` and safe-area insets, keeps a
16px input to prevent Safari focus zoom, and exposes 44px-or-larger touch
targets. Live model and permission changes never replace the durable premium
launcher pin or enable a worker-only bypass. CLI Control's shared header scrolls
away at phone width; its three selection rows stay compact, the session toolbar
uses intentional two-column rows, and the transcript scrolls inside a bounded
dynamic-viewport workspace so the composer cannot obscure the conversation.

## Icon language

- Use the shared 24×24 outline icons in `_app_shell.html`.
- Use a 1.8px rounded stroke and no filled tile-specific artwork.
- Pair every primary-navigation icon with a visible text label on desktop and
  mobile; tooltips and `aria-label` are supporting metadata, not the interface.
- Reserve emoji for event content authored as text. Do not use emoji as app
  navigation or utility icons.
- Active destinations use cyan plus `aria-current="page"`; inactive destinations
  remain legible at the shared muted-ink contrast.

## Application-shell anatomy

Every adopted top-level route uses one shared order:

1. The dashboard's full-width device frame.
2. The Fleet eye, `Nexus` wordmark, scan timestamp, Central clock,
   and notification bell.
3. The global primary navigation immediately below.
4. A route-specific content header and optional freshness/live status.
5. The route body.

The product chrome and global navigation form one persistent glass pane on
desktop and mobile. It remains in normal document flow, sticks to the top edge
while content scrolls beneath it, and uses translucent blur rather than an
opaque floating toolbar. The page remains the only vertical scroller; mobile
navigation may scroll horizontally inside the pane.

`templates/_app_shell.html::dashboard_chrome` is the sole markup source. A
route must not clone it, replace the wordmark with its page title, change its
width, or redefine its palette. Page identity belongs in
`content_header` below the navigation. `app/shell_context.py` supplies the same
cached scan state and unread count to every route without launching probes.

Do not add a second back button when the eye and `dashboard` navigation item
already lead home. A local breadcrumb is reserved for parent/child content such
as one worker run within the Worker Activity list.

## Adoption matrix

| Surface | Dashboard chrome | Shared content header | Shared icons | Status |
|---|---:|---:|---:|---|
| dashboard | canonical source | n/a | yes | adopted |
| fleet activity | yes | yes | yes | adopted |
| model usage | yes | yes | yes | adopted under Activity |
| fleet operations | yes | yes | yes | adopted parent workspace |
| conformance | yes | yes | yes | adopted under Operations |
| indexes | yes | yes | yes | adopted under Operations |
| watchdogs | yes | yes | yes | adopted under Operations |
| health | yes | yes | yes | adopted under Operations |
| CLI control | yes | yes | yes | adopted |
| notifications inbox/preferences | yes | yes | yes | adopted utility workspace |
| jobs activity list | yes | yes | yes | adopted under Activity |
| job detail | no | legacy dashboard sheet | partial | queued |
| worker activity list/session | yes | yes | yes | adopted under Activity |
| alerts, queues, approval | no | legacy wiki header | partial | queued |

The queued pages remain functional and retain the existing shared `wiki-head`
component. Migrate them by route family so parent/child breadcrumbs and sheet
behavior are verified together.

## Verification ledger

For each adoption pass, record:

- templates and shared sources changed;
- template compilation and test result;
- authenticated desktop and 390px mobile screenshots;
- document overflow result;
- persistent-shell position and readable glass contrast after scrolling;
- keyboard focus and `aria-current` checks;
- any intentional exception added to the matrix above.

The dated session or audit note owns screenshots and findings. This index owns
the current component map and adoption state.
