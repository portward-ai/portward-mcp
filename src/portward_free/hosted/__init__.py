"""The hosted route (#104): Portward runs one small engine per Personal user, the free tool in server mode, so
claude.ai and ChatGPT reach a cloud database with the computer off. Labelled "Portward runs this" everywhere.

- seal: sealed boxes (X25519, HKDF, AES-256-GCM) to the engine's key and to the account's inbox key.
- bundle: what the computer hands the engine (connections, logins, governance) and how the engine lands it.
- route, tools: the computer's side (which connections are on the route; the web_routes and web_route tools).
- engine: the container's side (`portward-free serve-hosted`).
- inbox: the engine's AI log summary, sealed back to the user's computers and into the synced history.
- service: the control plane's API for both sides; it only ever carries ciphertext."""
