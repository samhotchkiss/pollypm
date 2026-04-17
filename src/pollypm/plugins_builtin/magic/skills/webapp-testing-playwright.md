---
name: webapp-testing-playwright
description: Drive a local web app via Playwright, capture screenshots, and assert behavior in real browsers.
when_to_trigger:
  - test the UI
  - playwright test
  - verify in browser
  - e2e
  - end to end test
kind: magic_skill
attribution: https://github.com/microsoft/playwright
---

# Webapp Testing with Playwright

## When to use

Use for any behavior that only exists in the browser — user interactions that cross components, real-browser rendering, network mocking with service-worker precision, screenshot-based visual regression. Do not use Playwright when a unit test will do; the cost per test is 10-100x a unit test.

## Process

1. Pin the Playwright version in the test project's deps. Version drift breaks fixtures. `playwright==1.42.0` concrete, not a range.
2. Use TypeScript/JavaScript tests over Python ones unless your team standard is Python. The JS API is more complete and the debugging tools (Trace Viewer, UI Mode) are richer.
3. Start with `page.goto` and `page.waitForLoadState('networkidle')`. Do not paper over flakiness with `page.waitForTimeout(5000)` — that is the flakiness.
4. Locate elements by role first, then by label, then by text. Never by CSS class — classes churn. `page.getByRole('button', { name: 'Submit' })` survives refactors; `page.locator('.btn-primary')` breaks every week.
5. Assert via `expect(locator).toBeVisible()` and friends. These auto-retry up to the timeout. Do not `expect(await locator.count()).toBe(1)` — that snapshots at one instant.
6. Use `page.route()` to mock network calls. Real API calls in e2e tests are the #1 flakiness source. Mock them at the network boundary.
7. Run headed locally, headless in CI. `--ui` mode for debugging: time-travel over actions, re-run from any point.
8. Capture traces on failure: `trace: 'retain-on-failure'` in config. Opening the trace in UI mode shows the DOM snapshot at every step — invaluable.

## Example invocation

```typescript
// tests/e2e/task-create.spec.ts
import { test, expect } from '@playwright/test';

test('create task flow', async ({ page }) => {
  await page.route('**/api/projects', route =>
    route.fulfill({ json: [{ id: 'p1', name: 'Polly' }] })
  );

  await page.goto('/');
  await page.getByRole('button', { name: 'New task' }).click();

  await page.getByLabel('Title').fill('Ship magic skills');
  await page.getByLabel('Project').selectOption('p1');
  await page.getByRole('button', { name: 'Create' }).click();

  await expect(page.getByText('Ship magic skills')).toBeVisible();
  await expect(page.getByRole('status')).toHaveText(/created/i);
});
```

## Outputs

- One test file per user-visible flow.
- Role/label-based locators, no CSS selectors.
- Network calls mocked via `page.route`.
- Traces retained on failure.

## Common failure modes

- `waitForTimeout` instead of `waitFor` assertions; tests flake when the app is slow.
- CSS class locators; break every time a designer touches styles.
- Real network calls in e2e; one upstream flake takes down CI.
- No trace retention; when a CI failure happens, you cannot debug.
