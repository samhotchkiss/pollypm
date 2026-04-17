---
name: supabase-postgres
description: Supabase setup, RLS policies, pgvector embeddings, edge functions — the right defaults for a Supabase project.
when_to_trigger:
  - supabase
  - postgres on supabase
  - rls policy
  - pgvector
kind: magic_skill
attribution: https://github.com/supabase/supabase
---

# Supabase / Postgres

## When to use

Use when the project is on Supabase or evaluating it. Supabase gives you real Postgres + Auth + Storage + Edge Functions + Realtime in one stack — the cost is it is opinionated, and you need to play along. This skill encodes the non-obvious defaults that keep you out of trouble.

## Process

1. **One project, many schemas.** Do not multi-tenant across projects — that defeats Auth and RLS. Use Postgres schemas for logical separation within one project.
2. **RLS on every table, always.** Supabase exposes your tables through PostgREST — a table without RLS is a table open to the internet. Default stance: `ALTER TABLE ... ENABLE ROW LEVEL SECURITY;` followed by explicit policies.
3. **Policies target operations individually.** `FOR SELECT`, `FOR INSERT`, `FOR UPDATE`, `FOR DELETE` — each gets its own policy. `FOR ALL` is the hammer of last resort; it hides intent.
4. **Auth user in policies via `auth.uid()`**. Never reference `current_user` — that is the database role, not the app user. Policies read like "user can read rows they own": `USING (user_id = auth.uid())`.
5. **pgvector for embeddings**: `CREATE EXTENSION vector;` then `embedding vector(1536)`. Index with `CREATE INDEX ON ... USING hnsw (embedding vector_cosine_ops);` — HNSW beats IVFFlat for most Supabase workloads.
6. **Edge Functions for server-side work that needs secrets.** Deno runtime, TypeScript, one function per endpoint. Do not dump everything into one function — cold starts compound.
7. **Migrations via the Supabase CLI**, not the dashboard. `supabase migration new add_tasks_table`, write SQL, `supabase db push`. The dashboard is for exploration; every production change is a migration in version control.
8. **Realtime subscriptions only on tables that need it.** Each realtime-enabled table is replication load; default to polling for "stats refresh every 30s" and reserve realtime for collaborative edits.

## Example invocation

```sql
-- Migration: add tasks with RLS
CREATE TABLE public.tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  project_id uuid NOT NULL,
  title text NOT NULL CHECK (char_length(title) <= 200),
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "tasks_select_own" ON public.tasks
  FOR SELECT USING (user_id = auth.uid());

CREATE POLICY "tasks_insert_own" ON public.tasks
  FOR INSERT WITH CHECK (user_id = auth.uid());

CREATE POLICY "tasks_update_own" ON public.tasks
  FOR UPDATE USING (user_id = auth.uid())
             WITH CHECK (user_id = auth.uid());

CREATE POLICY "tasks_delete_own" ON public.tasks
  FOR DELETE USING (user_id = auth.uid());
```

```ts
// supabase/functions/summarize-task/index.ts
import { createClient } from 'npm:@supabase/supabase-js@2';

Deno.serve(async (req) => {
  const { task_id } = await req.json();
  const sb = createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
  );
  const { data, error } = await sb.from('tasks').select('*').eq('id', task_id).single();
  if (error) return new Response(JSON.stringify({ error: error.message }), { status: 400 });
  // ... call LLM, return summary
  return new Response(JSON.stringify({ summary: '...' }), { headers: { 'Content-Type': 'application/json' } });
});
```

## Outputs

- A `supabase/migrations/` directory with every schema change versioned.
- RLS enabled and policies defined per table, per operation.
- pgvector + HNSW index for embedding columns.
- Edge functions for secrets-requiring logic.

## Common failure modes

- Table without RLS; it is public whether you realize it or not.
- `FOR ALL` blanket policies that hide intent and over-permit.
- Dashboard-driven changes that do not exist in migrations; staging drifts.
- Realtime on every table; replication load crushes performance.
