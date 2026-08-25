Copy this file to `context.md` and replace it with your own. This is the single
most important setting in the project: the examiner is only as sharp as the
context it is given. "A Django shop with Celery workers on Hetzner" produces far
better questions than "web development", because it lets the model build
scenarios out of parts you actually touch.

Write a paragraph or two. Name the concrete pieces: languages, frameworks, where
things run, what the moving parts are called, what you are responsible for.

---

Example:

I run a private control plane and a couple of small products. The backend is
FastAPI with PostgreSQL, Redis and Celery behind Nginx, all in Docker on a single
VPS. Frontends are Next.js on Vercel, never the backend. One product stores its
accounts and analytics in Supabase. Releases are built in CI and shipped to the
App Store through the App Store Connect API. I work in Claude Code daily: hooks,
subagents, MCP servers, slash commands.

I am the only engineer. Nothing is handed off to a team, so I have to understand
the failure modes myself.
