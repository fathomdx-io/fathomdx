// Flat eslint config for addons/. Site and ui/ have their own worlds.
import js from "@eslint/js";
import globals from "globals";

export default [
  {
    ignores: [
      "addons/*/node_modules/**",
      "addons/browser-extension/dist/**",
      "addons/*/local-ui/**", // bundled/vendored UI
      "node_modules/**",
      "site/**",
      "ui/**",
    ],
  },
  js.configs.recommended,
  {
    files: ["addons/**/*.{js,mjs,cjs}"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: {
        ...globals.node,
      },
    },
    rules: {
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "no-console": "off", // CLIs and agents print to console legitimately
      "prefer-const": "warn",
      eqeqeq: ["error", "smart"],
    },
  },
  {
    // browser-extension runs in a browser context, not node
    files: ["addons/browser-extension/**/*.js"],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.webextensions,
      },
    },
  },
];
