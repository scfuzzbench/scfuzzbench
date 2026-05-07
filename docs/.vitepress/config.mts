import { defineConfig } from "vitepress";

export default defineConfig({
  title: "scfuzzbench",
  description: "Benchmark suite for smart-contract fuzzers.",

  // Custom domain, so always build with root base.
  base: "/",

  // Start in dark mode by default, but keep the toggle available.
  //
  // VitePress persists the chosen theme in localStorage; this sets an initial
  // value only when the user has not picked one yet.
  head: [
    ["meta", { name: "viewport", content: "width=device-width, initial-scale=1" }],
    ["meta", { name: "theme-color", content: "#0b1220" }],
    ["link", { rel: "icon", type: "image/png", href: "/favicon.png" }],
    ["link", { rel: "apple-touch-icon", href: "/apple-touch-icon.png" }],
    [
      "script",
      {},
      `;(() => {
  try {
    const key = "vitepress-theme-appearance";
    if (localStorage.getItem(key) === null) localStorage.setItem(key, "dark");
  } catch {}
})();`,
    ],
  ],

  appearance: true,

  themeConfig: {
    logo: "/images/scfuzzbench-logo.png",

    nav: [
      { text: "Introduction", link: "/introduction" },
      { text: "Runs", link: "/runs/" },
      { text: "Benchmarks", link: "/benchmarks/" },
      { text: "Start benchmark", link: "/start" },
      { text: "Transparency", link: "/transparency" },
      { text: "Methodology", link: "/methodology" },
      { text: "Operations", link: "/operations" },
      { text: "GitHub", link: "https://github.com/Recon-Fuzz/scfuzzbench" },
    ],

    sidebar: [
      {
        text: "Explore",
        items: [
          { text: "Introduction", link: "/introduction" },
          { text: "Runs", link: "/runs/" },
          { text: "Benchmarks", link: "/benchmarks/" },
          { text: "Start Benchmark", link: "/start" },
          { text: "Transparency", link: "/transparency" },
          { text: "Methodology", link: "/methodology" },
          { text: "Operations", link: "/operations" },
        ],
      },
    ],

    outline: { level: [2, 3] },

    search: {
      provider: "local",
      options: {
        detailedView: true,
      },
    },

    socialLinks: [{ icon: "github", link: "https://github.com/Recon-Fuzz/scfuzzbench" }],

    footer: {
      message: "Fully static. Generated in CI from S3 run artifacts.",
      copyright: "Copyright © Recon-Fuzz.",
    },
  },
});
