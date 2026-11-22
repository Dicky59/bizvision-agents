import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "e2e/**", "scripts/**", "playwright.config.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { rules: { "no-eval": "error" } },
  {
    // Model output must never reach the DOM as markup. Tests may read innerHTML to assert.
    files: ["src/**/*.ts"],
    rules: {
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Build DOM nodes with textContent instead." },
        { property: "outerHTML", message: "Build DOM nodes with textContent instead." },
        { property: "insertAdjacentHTML", message: "Build DOM nodes with textContent instead." },
      ],
    },
  },
);
