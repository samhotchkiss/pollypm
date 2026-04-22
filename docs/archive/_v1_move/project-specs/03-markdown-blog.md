# Project: Markdown Blog Engine

A static blog engine that turns markdown files into a deployed website.

## What I Want

I write blog posts in markdown with YAML frontmatter (title, date, tags).
I want a tool that reads all my posts, renders them to beautiful HTML with
syntax-highlighted code blocks and auto-generated table of contents, builds
a complete static site with an index, tag pages, archive, and RSS feed,
then deploys it to ItsAlive.

The blog should look like a professional developer blog — good typography,
dark/light mode toggle, responsive. I want 8 real blog posts included as
sample content (actual developer topics, not lorem ipsum).

The workflow: write a .md file in posts/, run `python -m blog build`, get a
complete site in dist/. Then `python -m blog deploy` pushes it live.

Also want a dev server with auto-rebuild when I edit a post.

## Stack
- Python (custom build pipeline, no framework)
- Jinja2 templates
- markdown + Pygments for rendering + syntax highlighting
- Deploy to ItsAlive

## Directory
`/Users/sam/dev/markdown-blog`

## Challenge
Build pipeline that produces real deployable HTML. Frontmatter parsing,
template inheritance, RSS generation, tag system, syntax highlighting,
SEO meta tags, and actual deployment. Plus writing 8 genuine blog posts
as content.
