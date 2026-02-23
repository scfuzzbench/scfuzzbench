<script setup>
import DefaultTheme from "vitepress/theme";
import SidebarShoutout from "./SidebarShoutout.vue";

const { Layout } = DefaultTheme;
</script>

<template>
  <Layout>
    <template #sidebar-nav-after>
      <SidebarShoutout />
    </template>
  </Layout>
</template>
