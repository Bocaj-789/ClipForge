import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Unmount between tests (RTL only auto-registers this when vitest globals are enabled).
afterEach(() => cleanup());
