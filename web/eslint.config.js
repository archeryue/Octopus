import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // OFF deliberately, not as a backlog item. Every occurrence was the
      // same house pattern: a pure, tested helper exported alongside the
      // component that uses it — SLASH_COMMANDS / slashQuery /
      // filterSlashCommands / buildRememberPrompt in SlashCommandMenu,
      // parseDelegationEvent in AgentDelegationEventCard (each carrying a
      // comment saying it is "exported for testing"), plus shadcn's
      // buttonVariants in ui/button.tsx. The rule is a Vite fast-refresh
      // optimisation hint, not a correctness rule, and satisfying it would
      // mean scattering tested helpers into sibling files — or fighting
      // shadcn's own convention — for no behavioural gain.
      'react-refresh/only-export-components': 'off',

      // A leading underscore is this codebase's marker for "required by the
      // signature, deliberately unused" (e.g. MessageBubble's
      // `sessionId: _sessionId`). Honour it instead of reporting it.
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
        destructuredArrayIgnorePattern: '^_',
      }],
    },
  },
  {
    // Playwright specs run in Node and drive a browser over CDP — they are
    // not browser React. Linting them with the app's config reported 7
    // findings that were all artefacts of the wrong context.
    files: ['e2e/**/*.{ts,tsx}'],
    languageOptions: {
      globals: globals.node,
    },
    rules: {
      // E2E specs traffic in untyped JSON from the API under test. Insisting
      // on precise types here would mean maintaining a second copy of the
      // server's response shapes, which is what contracts.ts exists to avoid
      // — and a wrong test type is a false failure, not a caught bug.
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
])
