# Project: GitHub Dashboard

A personal dashboard that shows everything happening across all my GitHub repos
in one place.

## What I Want

I want to see my GitHub activity at a glance — something GitHub itself doesn't
provide. Show me:
- All my repos with stars, forks, open issues, open PRs
- Recent commits across all repos (last 7 days)
- PRs where I'm a reviewer
- Language breakdown chart
- Commit activity heatmap (like the GitHub profile green squares)

Use the `gh` CLI for API access — it's already authenticated on this machine.
Cache everything in SQLite so I'm not hammering the API on every page load.
Handle rate limits gracefully.

I want real charts (Chart.js is fine) and a sortable repo table. It should
work with however many repos I have.

## Stack
- FastAPI + Jinja2 + SQLite (cache layer)
- `gh` CLI for GitHub API (already authenticated)
- Chart.js via CDN for visualizations

## Directory
`/Users/sam/dev/github-dash`

## Challenge
Complex external API integration — many paginated `gh api` calls, rate limit
handling, caching strategy, presenting dense data clearly. The fetcher needs
to be robust because real GitHub data has edge cases.
