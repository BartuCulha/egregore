# Coding Conventions

**Analysis Date:** 2026-03-05

## Naming Patterns

**Files:**
- React components: PascalCase with `.tsx` extension (e.g., `AsciiOracle.tsx`, `DensitySlider.tsx`, `ModeIndicator.tsx`)
- React hooks: camelCase prefixed with `use` (e.g., `useOracleState.ts`, `useGridSize.ts`, `useInteraction.ts`)
- Utility/lib files: camelCase (e.g., `markov.ts`, `characters.ts`, `constants.ts`, `createMaskTexture.ts`)
- Type definitions: singular lowercase (e.g., `types.ts`)
- Shell scripts: kebab-case with `.sh` extension (e.g., `graph-op.sh`, `session-start.sh`, `notify.sh`)
- Test files: `.test.js` or `.test.ts` suffix (e.g., `auth.test.js`)

**Functions:**
- camelCase for all functions (e.g., `createCharAtlas`, `loadSvgImage`, `getNextFamily`, `createOrbitingPulse`)
- React hooks follow `use` prefix (e.g., `useMode`, `useOracleState`, `usePulses`)
- Zustand store actions follow `setX` pattern (e.g., `setMode`, `setOracleState`, `setPointerPosition`)

**Variables:**
- camelCase for local variables (e.g., `gridCols`, `maskTexture`, `charAtlas`, `timeRef`)
- UPPER_SNAKE_CASE for constants (e.g., `COLORS`, `ZONES`, `TIMING`, `GRID`, `NUM_CHARS`)
- Shell scripts use UPPER_SNAKE_CASE for env/config vars (e.g., `SCRIPT_DIR`, `API_URL`, `API_KEY`, `STATE_FILE`)

**Types:**
- PascalCase for interfaces and types (e.g., `OracleStore`, `Pulse`, `CharacterFamily`, `InteractionMode`, `Zone`)
- Type exports use `type` keyword explicitly: `export type InteractionMode = ...`

## Code Style

**Formatting:**
- Tool: TypeScript/JavaScript uses ESLint (detected in `package.json`)
- Indentation: 2 spaces (inferred from source files)
- Line length: Approximately 120-140 characters (no hard limit enforced)
- Trailing commas: Used consistently in objects and arrays
- Semicolons: Not used in TypeScript/React files (omitted style)
- Shell scripts: 2-space indentation, semicolons after commands when chaining

**Linting:**
- Tool: ESLint v9+ (flat config)
- Plugins: `@eslint/js`, `eslint-plugin-react-hooks`, `eslint-plugin-react-refresh`
- TypeScript: Strict mode enabled in `tsconfig.json` with `noUnusedLocals`, `noUnusedParameters`, `noFallthroughCasesInSwitch`
- Run command: `npm run lint` (defined in `package.json`)

## Import Organization

**Order:**
1. React core imports (`react`, `react-dom`)
2. Third-party libraries (`three`, `zustand`, `@react-three/fiber`)
3. Internal utilities/lib (`../lib/constants`, `../lib/markov`)
4. Internal hooks (`../hooks/useOracleState`)
5. Internal components (`./DensitySlider`, `./InputOverlay`)
6. Internal types (`../types`)
7. Assets (`../assets/digital-souls.svg`)

**Path Aliases:**
- `@/*` resolves to `src/*` (configured in `tsconfig.json`)
- Relative imports (`../`, `./`) used throughout codebase
- No absolute imports from project root observed

## Error Handling

**Patterns:**
- Async/Promise chains use `.catch()` with fallback values: `onOracleQuery(question).catch(() => 'The oracle is silent.')`
- Shell scripts use `set -euo pipefail` at the top to fail fast on errors
- Shell scripts redirect errors to stderr with `>&2`
- Error responses checked with `jq -e` before processing: `if echo "$response" | jq -e '.detail' >/dev/null 2>&1; then`
- Network errors include timeouts: `--max-time 30` on curl commands

## Logging

**Framework:** console (TypeScript) / stderr redirects (shell)

**Patterns:**
- TypeScript: `console.error` for error logging (e.g., `.catch(console.error)`)
- Shell scripts: `echo "Error: message" >&2` for error output
- Shell scripts: `2>/dev/null` to suppress expected errors
- Telemetry events emitted fire-and-forget: `bash bin/telemetry.sh emit "event" '{}' 2>/dev/null &`
- Debug info: Shell scripts include comments explaining complex operations

## Comments

**When to Comment:**
- Shader code: Inline comments explain GLSL logic (e.g., `// Simple hash for randomness`, `// Zone-based energy`)
- Complex algorithms: Markov transitions include explanatory comments (e.g., `// Bias transitions based on energy`)
- Shell script headers: Each script has usage/operation documentation at top
- Public APIs: Component props documented inline or through TypeScript types

**JSDoc/TSDoc:**
- Not consistently used
- Types provide primary documentation through TypeScript interfaces
- Inline comments preferred over JSDoc blocks

## Function Design

**Size:**
- Functions range from 5-100 lines
- Shader functions embedded as template strings can exceed 200 lines
- Shell script functions typically 10-30 lines
- React components with hooks: 50-100 lines typical

**Parameters:**
- TypeScript: Destructured object parameters for React components
- Shell scripts: Positional parameters with validation (e.g., `SID="${1:?missing session-id}"`)
- Default parameters used in TypeScript: `onOracleQuery?: (q: string) => Promise<string>`

**Return Values:**
- TypeScript functions return strongly typed values
- React hooks return tuples or objects: `const { cols, rows } = useGridSize()`
- Shell scripts return via stdout (data) or exit codes (status)
- Async functions return Promises explicitly typed

## Module Design

**Exports:**
- Named exports preferred: `export function createCharAtlas()`, `export const COLORS = ...`
- Default exports used for React components: `export default function App()`
- Type-only exports: `export type InteractionMode = ...`
- Barrel pattern: Zustand store exports multiple selector hooks from same file

**Barrel Files:**
- Not extensively used
- Each module exports its own entities
- Central `constants.ts` aggregates shared configuration
- `types.ts` aggregates type definitions

---

*Convention analysis: 2026-03-05*
