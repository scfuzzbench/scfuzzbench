<script setup lang="ts">
import targetManifest from "../../../benchmarks/targets.json";

type Target = (typeof targetManifest.targets)[number];

function commitUrl(target: Target): string {
  return `${target.repo}/commit/${target.commit}`;
}

function shortCommit(commit: string): string {
  return commit.slice(0, 12);
}
</script>

<template>
  <table>
    <thead>
      <tr>
        <th>Target</th>
        <th>Pinned commit</th>
        <th>Properties path</th>
        <th>Why it is included</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="target in targetManifest.targets" :key="target.id">
        <td><a :href="target.repo">{{ target.label }}</a></td>
        <td><a :href="commitUrl(target)"><code>{{ shortCommit(target.commit) }}</code></a></td>
        <td><code>{{ target.properties_path }}</code></td>
        <td>{{ target.rationale }}</td>
      </tr>
    </tbody>
  </table>
</template>
