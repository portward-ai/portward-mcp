"""The names of the routes an AI app reaches Portward by, in one place for the free tool.

- desktop: an AI app on this computer (stdio).
- direct (#103): an AI app on the web reaches this computer, through the relay or serve-http.
- hosted (#104): an AI app on the web reaches the hosted engine Portward runs ("Portward runs this").

The AI log (proof.py), the usage counts, the weekly facts and the web-route tools all use these. Portward's account
service keeps its own copy: the free tool never imports it."""

from __future__ import annotations

DESKTOP, DIRECT, HOSTED = "desktop", "direct", "hosted"
ROUTES = (DESKTOP, DIRECT, HOSTED)
WEB_ROUTES = (HOSTED, DIRECT)  # the routes from the web, hosted first (the default where it's offered)
