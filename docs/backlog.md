# Backlog

Open work for debug-devices. One item per request. The orchestrator gives each item to an agent as a separate brief.

| # | Item | Area | Owner | Status |
|---|---|---|---|---|
| 1 | Mouse-wheel zoom on the full-screen phone snapshot: wheel up zooms in, wheel down zooms out, around the mouse. Drag pans. Page scrolling outside the full-screen snapshot does not change. Digital zoom of the photo, not a phone camera zoom. | Monitor page | dd-ui | In progress |
| 2 | Mirror the phone snapshot horizontally, to fix left-right confusion when the user moves the camera or inspects a board. A toggle in the panel and in full screen, with a "MIRRORED" badge. Display only: the MCP tools and the API keep the true orientation. | Monitor page | dd-ui | Queued after item 1 |

Done items move to the commit history. Agent guidance (evidence rules 6 and 7: `phone_zoom`, fresh `phone_snapshot`, other board side) is done in `16cc315`.
