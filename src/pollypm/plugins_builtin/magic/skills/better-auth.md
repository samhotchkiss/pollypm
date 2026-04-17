---
name: better-auth
description: Authentication with the better-auth library — sessions, OAuth, MFA, organization and role primitives.
when_to_trigger:
  - auth
  - login
  - session management
  - oauth
  - mfa
kind: magic_skill
attribution: https://github.com/better-auth/better-auth
---

# better-auth

## When to use

Use when adding authentication to a new Node/Next project, or replacing a hand-rolled auth stack. better-auth is the current best default for TypeScript apps: framework-agnostic, typed, plugin architecture (OAuth, MFA, orgs, passkeys). Reach for Supabase Auth or Auth0 if you are already in those ecosystems.

## Process

1. **Install and init with the CLI.** `pnpm add better-auth`, then `pnpm dlx @better-auth/cli generate` to scaffold the schema against your DB (Prisma, Drizzle, or raw Kysely). Schema is code — commit the migrations.
2. **One `auth.ts` as the single source of truth.** All config — providers, plugins, hooks, session options — lives here. No per-route auth config.
3. **Cookie sessions by default.** Opaque session IDs stored in httpOnly, secure, sameSite=lax cookies. JWT is available but avoid it unless you have a cross-domain reason — JWTs cannot be revoked without extra plumbing.
4. **Providers enabled explicitly.** Email/password with email verification, OAuth (Google / GitHub / etc) via plugins. Do not enable what you do not use — less surface, fewer misconfigurations.
5. **MFA via the `twoFactor` plugin** for TOTP. Passkeys via `passkey`. Magic links via `magicLink`. Each is a plugin — add by import, no forking.
6. **Organizations + roles via the `organization` plugin.** Use when you have multi-tenant with per-org roles. Do not build this yourself; every team does it and gets it subtly wrong.
7. **Server-side session checks in middleware.** Every protected route goes through `auth.api.getSession({ headers })` server-side. Do not trust client-only guards.
8. **Rate-limit sign-in and password reset.** Built-in via the `ratelimit` plugin. Tune per endpoint — strict on sign-up, generous on session refresh.

## Example invocation

```ts
// auth.ts
import { betterAuth } from 'better-auth';
import { prismaAdapter } from 'better-auth/adapters/prisma';
import { twoFactor, organization, passkey } from 'better-auth/plugins';
import { prisma } from './db.js';

export const auth = betterAuth({
  database: prismaAdapter(prisma, { provider: 'postgresql' }),
  emailAndPassword: {
    enabled: true,
    requireEmailVerification: true,
  },
  socialProviders: {
    google: {
      clientId: process.env.GOOGLE_CLIENT_ID!,
      clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
    },
  },
  session: {
    expiresIn: 60 * 60 * 24 * 30, // 30d
    cookieCache: { enabled: true, maxAge: 60 * 5 }, // 5m edge cache
  },
  plugins: [twoFactor(), organization(), passkey()],
});

// middleware.ts (Next.js)
import { auth } from './auth';

export async function middleware(req: Request) {
  const session = await auth.api.getSession({ headers: req.headers });
  if (!session && req.nextUrl.pathname.startsWith('/app')) {
    return Response.redirect(new URL('/login', req.url));
  }
}

// client — sign in
import { createAuthClient } from 'better-auth/react';
export const authClient = createAuthClient();
await authClient.signIn.email({ email, password });
```

## Outputs

- An `auth.ts` exporting the configured auth instance.
- Schema migrations generated from the CLI.
- Middleware that protects routes server-side.
- Rate limits on sign-in and password-reset endpoints.
- Client SDK wired for the frontend.

## Common failure modes

- Client-only auth guards; real attackers hit routes directly.
- Long-lived JWTs with no revocation path.
- Enabling every provider, plugin, and option; huge surface area with dev accounts forgotten.
- Skipping email verification; account takeover via typo'd emails.
