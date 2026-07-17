import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import prettier from "eslint-config-prettier";

// Flat config (ESLint 9). Lints the React app for real defects — undefined
// vars, unused code, and the rules-of-hooks / exhaustive-deps foot-guns — and
// leaves all formatting to Prettier (eslint-config-prettier turns the
// stylistic rules off). See .github/workflows/ci.yml for the CI gate.
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
    settings: { react: { version: "detect" } },
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
  prettier,
];
