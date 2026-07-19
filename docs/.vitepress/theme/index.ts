import type { Theme } from "vitepress";
import DefaultTheme from "vitepress/theme";
import "./custom.css";

import CustomLayout from "../components/CustomLayout.vue";
import StartBenchmark from "../components/StartBenchmark.vue";
import TransparencyDashboard from "../components/TransparencyDashboard.vue";

export default {
  extends: DefaultTheme,
  Layout: CustomLayout,
  enhanceApp(ctx) {
    DefaultTheme.enhanceApp?.(ctx);
    ctx.app.component("StartBenchmark", StartBenchmark);
    ctx.app.component("TransparencyDashboard", TransparencyDashboard);
  },
} satisfies Theme;
