import { defineConfig } from 'vitest/config';
import { transformWithOxc } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from 'vite-plugin-svgr';

const usePolling = String(process.env.CHOKIDAR_USEPOLLING || '').toLowerCase() === 'true';
const maxVendorChunkBytes = 450 * 1024;
const generatedTextureWatchIgnores = [
  '**/public/assets/textures/*.png',
];

const vendorChunkGroups = [
  {
    name: 'react-vendor',
    test: /node_modules[\\/](react|react-dom|scheduler|react-router|react-router-dom)[\\/]/,
    priority: 40,
  },
  {
    name: 'chart-series-vendor',
    test: /node_modules[\\/]echarts[\\/]lib[\\/]chart[\\/]/,
    priority: 35,
  },
  {
    name: 'chart-vendor',
    test: /node_modules[\\/](echarts|zrender)[\\/]/,
    priority: 30,
  },
  {
    name: 'emoji-vendor',
    test: /node_modules[\\/](@emoji-mart|emoji-mart)[\\/]/,
    priority: 20,
    maxSize: maxVendorChunkBytes,
  },
  {
    name: 'vendor',
    test: /node_modules[\\/]/,
    priority: 1,
    maxSize: maxVendorChunkBytes,
  },
];

function jsxInJsPlugin() {
  return {
    name: 'breaktwenty-jsx-in-js',
    enforce: 'pre',
    async transform(code, id) {
      const sourceId = id.split('?', 1)[0].replace(/\\/g, '/');
      if (!sourceId.endsWith('.js') || !sourceId.includes('/src/')) {
        return null;
      }

      const result = await transformWithOxc(code, id, {
        lang: 'jsx',
        sourceType: 'module',
        jsx: {
          runtime: 'automatic',
        },
      });

      return {
        code: result.code,
        map: result.map,
      };
    },
  };
}

export default defineConfig({
  plugins: [
    jsxInJsPlugin(),
    react(),
    svgr(),
  ],
  server: {
    host: '127.0.0.1',
    port: 3000,
    strictPort: true,
    watch: {
      ignored: generatedTextureWatchIgnores,
      usePolling,
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 3000,
    strictPort: true,
  },
  build: {
    outDir: 'build',
    emptyOutDir: true,
    rolldownOptions: {
      moduleTypes: {
        '.js': 'jsx',
      },
      output: {
        codeSplitting: {
          groups: vendorChunkGroups,
        },
      },
    },
  },
  optimizeDeps: {
    rolldownOptions: {
      moduleTypes: {
        '.js': 'jsx',
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/setupTests.js',
  },
});
