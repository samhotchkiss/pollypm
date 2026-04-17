---
name: firebase-stack
description: Firebase Auth, Firestore, App Hosting, and Genkit basics — a full backend without servers.
when_to_trigger:
  - firebase
  - firestore
  - firebase auth
  - genkit
kind: magic_skill
attribution: https://github.com/firebase/firebase-tools
---

# Firebase Stack

## When to use

Use when the project is on Firebase or when you need a full backend (auth + data + hosting + AI) without operating servers. Firebase excels at mobile apps and prototypes; for complex relational queries or strict transactional semantics, pick Postgres-based stacks instead.

## Process

1. **Firebase Auth as identity of record.** Email/password, OAuth, phone, anonymous. Never roll your own auth on top of Firestore — the SDK gives you ID tokens that integrate with security rules automatically.
2. **Firestore for document data, Realtime Database almost never.** Firestore has stronger consistency, real-time listeners, and offline support. Only pick RTDB for high-frequency ephemeral data (live cursor positions, game state).
3. **Security rules on every collection.** `allow read, write: if request.auth.uid == resource.data.ownerId;` is the baseline. Rules are code — version them, test them with the emulator.
4. **Subcollections over arrays.** An array of 50 items in a document is okay; 500 is not. When in doubt, make it a subcollection — Firestore scales per document, not per array length.
5. **Composite indexes ahead of time.** Firestore auto-prompts you to create indexes on the first query that needs one, but in CI/CD that fails silently. Define in `firestore.indexes.json` and deploy.
6. **App Hosting for Next.js** — first-class support for SSR, per-region deploys. For static output, Firebase Hosting (classic) is still the simpler choice.
7. **Genkit for AI flows.** `defineFlow`, schema-typed inputs and outputs, supports OpenAI/Gemini/Anthropic as plug-in models. Run locally via `genkit start`, deploy to Cloud Functions.
8. **Emulator suite for local dev.** `firebase emulators:start` runs Auth + Firestore + Functions + Hosting locally. CI should use emulators, not real projects, for unit tests.

## Example invocation

```js
// firestore.rules
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /tasks/{taskId} {
      allow read: if request.auth != null
                  && request.auth.uid == resource.data.userId;
      allow create: if request.auth != null
                    && request.auth.uid == request.resource.data.userId
                    && request.resource.data.title is string
                    && request.resource.data.title.size() <= 200;
      allow update, delete: if request.auth != null
                            && request.auth.uid == resource.data.userId;
    }
  }
}
```

```ts
// flows/summarize-task.ts — Genkit flow
import { genkit, z } from 'genkit';
import { googleAI, gemini15Flash } from '@genkit-ai/googleai';

const ai = genkit({ plugins: [googleAI()], model: gemini15Flash });

export const summarizeTask = ai.defineFlow(
  {
    name: 'summarizeTask',
    inputSchema: z.object({ taskId: z.string(), title: z.string(), notes: z.string() }),
    outputSchema: z.object({ summary: z.string() }),
  },
  async (input) => {
    const { text } = await ai.generate(
      `Summarize this task in one sentence:\nTitle: ${input.title}\nNotes: ${input.notes}`
    );
    return { summary: text };
  }
);
```

## Outputs

- `firestore.rules` with per-collection rules.
- `firestore.indexes.json` with composite indexes.
- Genkit flows in `flows/` deployed to Cloud Functions.
- Emulator suite running for local dev and CI.

## Common failure modes

- No security rules; Firestore defaults to locked, but the first "allow all" you add never gets tightened.
- Arrays instead of subcollections; documents hit 1MB and hot-spotting.
- Missing composite indexes; query fails in prod, passes in dev (because dev ran each query manually and auto-created them).
- Running tests against real Firebase; bills spike and tests pollute prod data.
