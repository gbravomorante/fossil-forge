const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/ui',
  timeout: 180000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8765',
    viewport: { width: 1440, height: 1100 },
    launchOptions: { channel: 'chrome' },
  },
  reporter: 'list',
});
