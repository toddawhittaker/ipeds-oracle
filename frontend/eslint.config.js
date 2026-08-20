import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

// Flat config (ESLint 10). Lints the React app for real defects — undefined
// vars, unused code, and the rules-of-hooks / exhaustive-deps foot-guns.
// Formatting is not enforced by any tool: this codebase keeps a compact
// hand-written style that Prettier disagreed with in 144 of 169 files, so
// Prettier was dropped rather than mass-reformat. See
// .github/workflows/ci.yml for the CI gate.
export default [
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "test-results/**",
      "playwright-report/**",
    ],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { react, "react-hooks": reactHooks },
    // Pinned rather than "detect": eslint-plugin-react's version
    // auto-detection calls context.getFilename(), which ESLint 10 removed,
    // and every rule then throws. Nothing else in the plugin needs the
    // removed APIs — lib/util/eslint.js falls back to sourceCode.* — so
    // pinning is the whole fix. The value only gates version-specific
    // rules; bump it if a rule ever needs to know a newer React.
    settings: { react: { version: "19.2" } },
    rules: {
      ...react.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      // Vite's automatic JSX runtime means React need not be in scope, and this
      // codebase deliberately doesn't use prop-types.
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
      // Allow `const { node, ...rest } = props` to drop react-markdown's `node`
      // prop (so it isn't spread onto real DOM elements) without a lint error.
      "no-unused-vars": ["error", { ignoreRestSiblings: true }],
    },
  },
  {
    // Playwright specs + root config files run in Node (process, etc.).
    files: ["e2e/**/*.js", "*.config.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.node },
    },
  },
];
