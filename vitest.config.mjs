import { defineConfig } from 'vitest/config';

// Скоупим Vitest строго на фронт-тесты webapp/static, чтобы он не пытался
// исполнять Python-тесты из tests/ (их гоняет pytest).
export default defineConfig({
  test: {
    include: ['webapp/static/__tests__/**/*.test.js'],
    environment: 'node',
  },
});
