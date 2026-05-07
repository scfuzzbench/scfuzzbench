import type { Theme } from "vitepress";
import DefaultTheme from "vitepress/theme";
import "./custom.css";

import CustomLayout from "../components/CustomLayout.vue";
import CostTransparencyDashboard from "../components/CostTransparencyDashboard.vue";
import StartBenchmark from "../components/StartBenchmark.vue";

export default {
  extends: DefaultTheme,
  Layout: CustomLayout,
  enhanceApp(ctx) {
    DefaultTheme.enhanceApp?.(ctx);
    ctx.app.component("CostTransparencyDashboard", CostTransparencyDashboard);
    ctx.app.component("StartBenchmark", StartBenchmark);
  },
} satisfies Theme;
