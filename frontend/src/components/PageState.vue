<script setup lang="ts">
import { computed } from "vue";
import { NAlert, NButton, NEmpty, NSpin } from "naive-ui";

const props = withDefaults(
  defineProps<{
    loading?: boolean;
    error?: string | Error | null;
    empty?: boolean;
    title?: string;
    description?: string;
  }>(),
  { loading: false, empty: false, title: "暂无数据", description: "" },
);
const emit = defineEmits<{ retry: [] }>();
const errorText = computed(() =>
  props.error instanceof Error ? props.error.message : props.error,
);
</script>

<template>
  <div v-if="loading" class="page-state" role="status" aria-live="polite">
    <n-spin size="medium" /><span>正在读取数据…</span>
  </div>
  <n-alert
    v-else-if="error"
    type="error"
    title="数据暂时无法读取"
    class="page-error"
  >
    <div>{{ errorText }}</div>
    <n-button class="retry-button" size="small" secondary @click="emit('retry')"
      >重新读取</n-button
    >
  </n-alert>
  <div v-else-if="empty" class="page-state">
    <slot name="empty"
      ><n-empty :description="title"
        ><template v-if="description" #extra
          ><span class="data-note">{{ description }}</span></template
        ></n-empty
      ></slot
    >
  </div>
  <slot v-else />
</template>
