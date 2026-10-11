<script setup>
// A small sparkle-over-card glyph: "something was brought back for you".
defineProps({size: {type: Number, default: 14}});
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 16 16" fill="none" aria-hidden="true" class="rec-entry-icon">
    <rect x="1.75" y="4.25" width="9.5" height="10" rx="2" stroke="currentColor" stroke-width="1.3"/>
    <path d="M4.25 8.25h4.5M4.25 10.75h3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
    <path d="M12.5 1.25l.62 1.63 1.63.62-1.63.62-.62 1.63-.62-1.63-1.63-.62 1.63-.62.62-1.63z" fill="currentColor"/>
  </svg>
</template>
