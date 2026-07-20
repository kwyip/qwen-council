# Qwen Councils

A blue-themed community Flask application that live-pulls arXiv preprints from January 1, 2026 through July 7, 2026 in reverse chronological order. Visitors can browse papers, open an article detail page, register, log in, and leave comments backed by SQLite.

## Features

- Browse all 2026 arXiv papers on `/`, or use arXiv archive community pages such as `/physics`, `/cs`, `/math`, `/q-bio`, `/q-fin`, `/stat`, `/eess`, and `/econ`.
- Register, log in, set a profile picture URL, comment on papers, and upvote/downvote both papers and comments.
- Archive pages follow the high-level arXiv category taxonomy.

## Run locally

```bash
uv sync
uv run flask --app app run --debug
```

Open <http://127.0.0.1:5000> and browse the live arXiv feed. The SQLite database is initialized automatically at startup in `instance/quantum_council.sqlite` for users, comments, votes, and recently viewed article metadata. To recreate it manually, delete that file and run `uv run flask --app app init-db`.


## Deployment and reviewer agents

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for Alibaba Cloud ECS deployment instructions and the daily Qwen reviewer agent workflow.

## Submission video

See [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md) for the timed three-minute product demonstration storyboard, narration, recording notes, and capture checklist.
