# Project: Expense Tracker

A personal finance app that imports real bank transactions and shows
where my money goes.

## What I Want

I download CSV exports from my bank. I want to drag them into this app
and have it figure out the column format automatically (different banks
use different formats), import the transactions, and auto-categorize them
(Starbucks → Dining, Walmart → Groceries, Netflix → Entertainment).

When I re-categorize something manually, the app should learn from it —
future transactions from the same merchant should auto-categorize the same
way.

I want a dashboard with charts: spending by category pie chart, daily
spending trend, budget vs actual progress bars. I want to set monthly
budgets per category and see when I'm over.

Needs basic auth (email + password with sessions) because this is
financial data.

Seed it with 6 months of realistic fake transactions (300+) so the
dashboard looks meaningful.

## Stack
- FastAPI + Jinja2 + SQLite
- Chart.js for visualizations
- Cookie-based session auth
- CSV parsing with auto-detection

## Directory
`/Users/sam/dev/expense-tracker`

## Challenge
Real-world CSV format detection (different delimiters, date formats, column
names). Auto-categorization engine that learns from corrections. Auth system
with password hashing and sessions. Dense dashboard with multiple chart types.
