# Hub integration contract — `/apps/agent-container/`

How the `scitex-agent-container` **Agents** app embeds inside the SciTeX Hub
shell. This is a **contract** the Hub implements; the package ships the app and
nothing else. Do not edit Hub to adopt this — adopt by following it.

The package is a mountable Django app. The Hub already mounts other
`scitex_*` apps the same way (e.g. `/apps/storage/`, `/apps/cards/`), so this
adds a sibling, not a new mechanism.

## 1. What the package provides

| Piece | Value |
| --- | --- |
| App config (add to `INSTALLED_APPS`) | `scitex_agent_container._django.apps.AgentContainerDashboardConfig` |
| URL module | `scitex_agent_container._django.urls` |
| URL namespace / `app_name` | `scitex_agent_container` |
| Manifest (Hub app registry) | `scitex_agent_container/_django/manifest.json` |
| Manifest slug / label | `agent-container` / `SciTeX Agent Container` |
| Frontend type | `server-rendered` |
| Deps (Hub must have installed) | `django`, matching `scitex-sdk[gui]` source/release (shared header, filter, static tags) |

## 2. Mount it under `/apps/agent-container/`

```python
# hub urls.py (or the Hub's app-mounting helper)
from django.urls import include, path

urlpatterns = [
    # ... other /apps/<slug>/ mounts ...
    path("apps/agent-container/", include("scitex_agent_container._django.urls")),
]
```

Add the app to `INSTALLED_APPS`. That is the entire Hub-side change. The app's
templates and static resolve relative to the **request path**, so it renders
correctly under `/apps/agent-container/` with no further wiring.

## 3. One host shell, one SDK app header

All leaf pages extend `scitex_sdk/app/app_shell.html` and fill
`scitex_app_content`. A mounted host shadows that adapter through its template
DIRS to render its own workspace shell. Standalone uses the SDK adapter.
Do not copy the content into a second shell or maintain a separate header.

`views._shell_context()` uses `scitex_sdk.ui.branding.shell_context` and
`scitex_sdk.ui.mount.mount_context`. The header label/version and host-provided
scope capabilities are data; the SDK owns typography and project picker chrome.
SAC supports identity scope and shows unsupported project choices disabled.
Leaf static URLs use `scitex_static.app_static` so preview changes invalidate
cached CSS/JavaScript even when package metadata has not changed.

## 4. What the Hub must supply (config, not code)

The app reads its data + authorization entirely from the environment of the
**web process**:

| Env var | Meaning |
| --- | --- |
| `SCITEX_AGENT_CONTAINER_API_URL` | The SAC host control plane, e.g. `http://127.0.0.1:7878`. Required for any live data. |
| `SCITEX_AGENT_CONTAINER_API_TOKEN_FILE` | Path to the host-wide Bearer token (`sac listen` auto-writes `~/.scitex/agent-container/tokens/listen-<bind-host>.token`). The app auto-discovers this; set it explicitly only if the token lives elsewhere. |
| `SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS` | Comma list of identities allowed to start/stop/restart **own-node** agents. |
| `SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS` | Comma list allowed to see **and** control **other-node** agents. A separate, stricter list; every cross-host grant/denial and lifecycle action is written to the audit trail. |
| `SCITEX_AGENT_CONTAINER_GUI_AUDIT_LOG` | Optional absolute path for the audit log (defaults to `~/.scitex/agent-container/runtime/gui-audit.log`). |
| `SCITEX_AGENT_CONTAINER_GUI_IDENTITY` | Standalone-only: the declared acting identity when there is no Django login. **In the Hub, ignore this — identity comes from `request.user`.** |

**Identity resolution order:** a Django-authenticated `request.user.username`
always wins. The app is therefore Hub-auth-aware: an ordinary Hub user sees
only their own node's agents; a Hub superuser/operator in the cross-host list
sees the fleet, audited. No app-local login; the SDK selector reflects the host capability policy.

## 5. Acceptance (what "integrated" means)

- `GET /apps/agent-container/` renders the fleet **inside the Hub's `global_base`** —
  one workspace shell and one SDK app header with capability-aware selection.
- `GET /apps/agent-container/api/fleet` returns the scoped JSON projection.
- `GET /apps/agent-container/<name>/` renders the detail card; lifecycle controls show
  only for an authorized identity and delegate to the listener.
- Fleet rows show the latest published runner operation and phase. The detail
  card also reports turn elapsed, last transcript progress, queue, inference,
  tool, and wait observations. A field is explicitly `Unknown` when the
  listener has no authoritative runtime signal; the app does not infer current
  activity from log text.
- Cross-host rows are hidden from a non-cross-host identity and flagged for one.
- Static (`/static/scitex_agent_container/agents.css`) and the scitex-sdk shell
  assets resolve (no 404s) — the Hub's staticfiles gather must include the
  app's `static/` dir (it does, since the app is in `INSTALLED_APPS`).

## 6. Boundaries

- The app is a **projection** of `sac listen`; it opens no DB of its own,
  writes no registry files, and never re-derives lifecycle state.
- The only writes are the **audit trail** (cross-host authorization +
  lifecycle actions) — a JSONL the operator can inspect.
- Lifecycle verbs are delegated to the authenticated listener, so the Hub adds
  no second control path and no second token.

## 7. Chat and filter behavior

Dashboard message forms request JSON from their own authorized Django endpoint.
The endpoint uses the authenticated listener's `POST /agents/<name>/send`
transport, which can reach a host-loopback bridge from a containerized dashboard.
An accepted exchange receipt means pending completion, not proof of completion.
An uncertain dispatch remains visible and is not retried automatically. Dispatch
IDs are correlation; the in-process guard is not durable cross-worker deduplication.

Both the chat and injected Details composers stay on the dashboard. Shift+Enter
inserts a newline. Real browser regressions use disposable intercepted transports:

```bash
python -m pytest tests/integration/test_gui_composer_browser.py -m integration
```

This optional test requires Playwright and Chromium. `SCITEX_TEST_CHROMIUM` can
point to an existing executable. It sends no messages to running agents.

The filter is an empty `data-stx-filter-field` SDK mount; the leaf owns only matching
and its outer layout width. The SDK supplies the leading icon, keyword
placeholder, and focus-aware Ctrl+K/Esc badge. The pane row fills remaining
workspace height with bottom breathing room. SDK phone tabs control which pane
is displayed; leaf CSS must not force hidden panes visible.
