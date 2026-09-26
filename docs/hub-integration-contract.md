# Hub integration contract — `/apps/agents/`

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
| Manifest slug / label | `agents` / `Agents` |
| Frontend type | `server-rendered` |
| Deps (Hub must have installed) | `django`, `scitex-app>=0.11.0`, `scitex-ui>=0.13.0` |

## 2. Mount it under `/apps/agents/`

```python
# hub urls.py (or the Hub's app-mounting helper)
from django.urls import include, path

urlpatterns = [
    # ... other /apps/<slug>/ mounts ...
    path("apps/agents/", include("scitex_agent_container._django.urls")),
]
```

Add the app to `INSTALLED_APPS`. That is the entire Hub-side change. The app's
templates and static resolve relative to the **request path**, so it renders
correctly under `/apps/agents/` with no further wiring.

## 3. The no-duplicate-header rule

`scitex_app._standalone.run_standalone` (the standalone server) configures
Django and renders the full `scitex_ui/standalone_shell.html`. **When mounted
in the Hub, do NOT extend the standalone shell.** The Hub's `global_base`
already renders the shell (header, nav, launcher, theme boot). Extending the
standalone shell inside it would double the header and introduce a project
switcher the Hub does not want.

Instead, the mounted views render their **content partial** into the Hub's
block. The package ships the content as the `app_content` block of its two
templates; to embed cleanly the Hub renders the *content only*:

```python
# Hub-side: reuse the package views, render into global_base's content block
from scitex_agent_container._django import views as agents_views

# map /apps/agents/ -> agents_views.index, /apps/agents/<name>/ -> detail, etc.
```

The templates call `shell_context()` + `mount_context(request, view_path=...)`
in the view, which is the scitex-ui contract for a **mounted** (not standalone)
app: `mount_context` emits the `_mount_marker.html` partial that tells
client-side code the app is mounted at `/apps/agents/`, so `mountPrefix()`
resolves instead of assuming root. That is what keeps the app from re-
rendering a header.

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
sees the fleet, audited. No app-local login, no added project switcher.

## 5. Acceptance (what "integrated" means)

- `GET /apps/agents/` renders the fleet **inside the Hub's `global_base`** —
  one header, no second header, no project switcher.
- `GET /apps/agents/api/fleet` returns the scoped JSON projection.
- `GET /apps/agents/<name>/` renders the detail card; lifecycle controls show
  only for an authorized identity and delegate to the listener.
- Fleet rows show the latest published runner operation and phase. The detail
  card also reports turn elapsed, last transcript progress, queue, inference,
  tool, and wait observations. A field is explicitly `Unknown` when the
  listener has no authoritative runtime signal; the app does not infer current
  activity from log text.
- Cross-host rows are hidden from a non-cross-host identity and flagged for one.
- Static (`/static/scitex_agent_container/agents.css`) and the scitex-ui shell
  assets resolve (no 404s) — the Hub's staticfiles gather must include the
  app's `static/` dir (it does, since the app is in `INSTALLED_APPS`).

## 6. Boundaries

- The app is a **projection** of `sac listen`; it opens no DB of its own,
  writes no registry files, and never re-derives lifecycle state.
- The only writes are the **audit trail** (cross-host authorization +
  lifecycle actions) — a JSONL the operator can inspect.
- Lifecycle verbs are delegated to the authenticated listener, so the Hub adds
  no second control path and no second token.
