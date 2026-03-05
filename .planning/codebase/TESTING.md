# Testing Patterns

**Analysis Date:** 2026-03-05

## Test Framework

**Runner:**
- Vitest (inferred from test file directive `// @vitest-environment node`)
- Version: Not explicitly specified in main `package.json`, likely inherited from site dependencies

**Config:**
- No root-level `vitest.config.js` detected
- Configuration likely embedded in `vite.config.ts` or inherited from Vite defaults

**Assertion Library:**
- Vitest built-in assertions (`expect`)

**Run Commands:**
```bash
npm run test              # Run all tests (not defined in package.json)
npm run lint              # Linting (defined)
```

Note: Test running not yet integrated into main workflow.

## Test File Organization

**Location:**
- Co-located with source: `site/src/auth.test.js` alongside `site/src/auth.js`
- No separate `tests/` or `__tests__/` directories observed in TypeScript codebase

**Naming:**
- Pattern: `{module}.test.{js|ts|tsx}`
- Example: `auth.test.js` tests `auth.js`

**Structure:**
```
site/src/
├── auth.js
└── auth.test.js
```

## Test Structure

**Suite Organization:**
```javascript
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { functionToTest, CONSTANT } from './module'

describe('feature or function name', () => {
  it('describes expected behavior', () => {
    expect(functionToTest(input)).toBe(expected)
  })

  it('handles edge case', () => {
    expect(functionToTest(edgeInput)).toBe(edgeExpected)
  })
})
```

**Patterns:**
- Top-level `describe` block names the feature/module being tested
- Multiple `it` blocks test individual behaviors
- Test names use natural language (e.g., `'allows oguzhan'`, `'rejects empty string'`)
- Environment directives at file top when needed: `// @vitest-environment node`

## Mocking

**Framework:**
- Not observed in current test files
- Vitest's built-in mocking likely available (`vi.mock()`)

**Patterns:**
Not yet established in codebase.

**What to Mock:**
- External API calls (e.g., to Egregore API, Telegram, Neo4j)
- File system operations in shell scripts
- Network requests via `curl`

**What NOT to Mock:**
- Pure functions (Markov transitions, character selection)
- Type definitions
- Constants

## Fixtures and Factories

**Test Data:**
```javascript
// Pattern observed: inline test data
expect(ADMIN_USERS).toEqual(['oguzhan', 'fcdagdelen'])
```

**Location:**
- Test data defined inline within test files
- No separate fixtures directory detected

## Coverage

**Requirements:**
- None enforced
- No coverage scripts in `package.json`

**View Coverage:**
```bash
# Not yet configured
```

## Test Types

**Unit Tests:**
- Scope: Individual functions and modules
- Example: `auth.test.js` tests access control logic
- Approach: Import function, call with various inputs, assert outputs

**Integration Tests:**
- Not yet present in codebase
- Future use: Test interaction between shell scripts and API endpoints

**E2E Tests:**
- Not used
- No Playwright, Cypress, or similar tooling detected

## Common Patterns

**Async Testing:**
```typescript
// Pattern for async operations (not yet observed in tests, but used in source):
it('handles async query', async () => {
  const result = await asyncFunction()
  expect(result).toBe(expected)
})
```

**Error Testing:**
```typescript
// Pattern observed in source, applicable to tests:
it('rejects invalid input', () => {
  expect(functionToTest(null)).toBe(false)
  expect(functionToTest(undefined)).toBe(false)
})
```

**Exhaustive Testing:**
```javascript
// Pattern from auth.test.js:
it('only has exactly 2 admins', () => {
  expect(ADMIN_USERS).toHaveLength(2)
  expect(ADMIN_USERS).toEqual(['oguzhan', 'fcdagdelen'])
})
```

**Case Sensitivity Testing:**
```javascript
// Pattern from auth.test.js:
it('is case-sensitive (GitHub logins are lowercase)', () => {
  expect(isAdmin('Oguzhan')).toBe(false)
  expect(isAdmin('OGUZHAN')).toBe(false)
})
```

## Testing Status

**Current State:**
- Minimal testing infrastructure present
- Single test file detected: `site/src/auth.test.js`
- No tests for TypeScript/React code in `ascii-oracle/`
- Shell scripts untested (no bash unit testing framework detected)

**Recommended Additions:**
- Tests for Markov chain logic (`src/lib/markov.ts`)
- Tests for character selection algorithms
- Tests for Zustand store actions
- Shell script tests using bats or similar
- Integration tests for API gateway interactions

---

*Testing analysis: 2026-03-05*
