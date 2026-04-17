---
name: playwright-best-practices
description: Page-object pattern, waiting strategies, fixture usage, parallelism — a Playwright project that does not become a liability.
when_to_trigger:
  - playwright project setup
  - flaky e2e
  - playwright architecture
  - e2e best practices
kind: magic_skill
attribution: https://github.com/microsoft/playwright
---

# Playwright Best Practices

## When to use

Use when setting up a new Playwright test project, or when an existing one is sliding into flakiness and slow runs. This skill lays down the skeleton that makes e2e tests a long-term asset rather than the thing everyone avoids touching.

## Process

1. One config file: `playwright.config.ts`. Set `workers: 4` (or `os.cpus() / 2`), `retries: 2` on CI, `forbidOnly: !!process.env.CI`, `reporter: 'html'`, `use.baseURL` from env.
2. Adopt the **page object** pattern. Each URL or major component gets a class with methods that model user intent: `tasksPage.createTask(title)`, not `page.click('#new-task-btn')`. Tests read like specs.
3. Shared setup via fixtures: `@playwright/test` custom fixtures extend the default `test` with your objects. `const test = base.extend<{ tasksPage: TasksPage }>({ tasksPage: async ({ page }, use) => { await use(new TasksPage(page)); } });` Pages get injected instead of instantiated per-test.
4. Stable test data: use unique names per test (`'Task ' + test.info().testId`) so parallel runs do not collide. Tear down in `afterEach` — do not rely on a clean DB between tests.
5. Auth state via `storageState`. Log in once in a setup project, save cookies+localStorage, reuse across all other tests. Never log in per-test.
6. Trace + screenshot on failure only: `trace: 'retain-on-failure', screenshot: 'only-on-failure'`. Full capture on every run blows up artifact storage.
7. Parallelism: test files run in parallel by default; tests within a file serialize. If you need test-level parallelism, `test.describe.configure({ mode: 'parallel' })`. Do not globalize; think per-file.
8. Flaky-test policy: if a test retries once on CI, investigate same day. `retries: 2` is a backstop, not a license to ignore.

## Example invocation

```typescript
// playwright.config.ts
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  workers: process.env.CI ? 4 : undefined,
  retries: process.env.CI ? 2 : 0,
  forbidOnly: !!process.env.CI,
  reporter: [['html'], ['list']],
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    storageState: 'tests/e2e/.auth/user.json',
  },
  projects: [
    { name: 'setup', testMatch: /global.setup\.ts/ },
    { name: 'chromium', use: { browserName: 'chromium' }, dependencies: ['setup'] },
  ],
});

// tests/e2e/pages/tasks-page.ts
export class TasksPage {
  constructor(private page: Page) {}
  async goto() { await this.page.goto('/tasks'); }
  async createTask(title: string) {
    await this.page.getByRole('button', { name: 'New task' }).click();
    await this.page.getByLabel('Title').fill(title);
    await this.page.getByRole('button', { name: 'Create' }).click();
  }
  taskCard(title: string) {
    return this.page.getByRole('article').filter({ hasText: title });
  }
}
```

## Outputs

- A `playwright.config.ts` with workers, retries, reporter, baseURL.
- Page-object classes under `tests/e2e/pages/`.
- Custom fixtures injecting pages into tests.
- Auth storageState reused across tests.

## Common failure modes

- Instantiating page objects inside every test instead of via fixtures; boilerplate accretes.
- Logging in per-test; runtime doubles.
- `retries: 5` on CI to silence flakiness; symptoms hide, causes grow.
- Hard-coded `http://localhost:3000` URLs; staging runs impossible.
