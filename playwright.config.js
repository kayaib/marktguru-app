const { defineConfig, devices } = require('@playwright/test');
const path = require('path');

const LOCAL_URL = 'file://' + path.join(__dirname, 'docs', 'index.html');

module.exports = defineConfig({
  testDir: './tests',
  timeout: 15_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: { baseURL: LOCAL_URL },
  projects: [
    { name: 'desktop-chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile-safari',    use: { ...devices['iPhone 14 Pro'] } },
  ],
});
