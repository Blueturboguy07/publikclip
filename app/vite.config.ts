import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Tauri expects a fixed dev port and no auto-open.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1430,
    strictPort: true
  },
  build: {
    target: 'es2022',
    rollupOptions: {
      input: {
        // the main window
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        // the Sign in with publik window, opened by the Rust side before main
        gate: fileURLToPath(new URL('./gate.html', import.meta.url))
      }
    }
  }
})
