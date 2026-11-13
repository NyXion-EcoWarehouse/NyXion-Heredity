import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const root = path.dirname(fileURLToPath(import.meta.url));
const backend = process.env.HEREDITY_WEBUI_BACKEND ?? 'http://127.0.0.1:5099';

export default defineConfig({
    root,
    cacheDir: path.join(root, '.cache/vite-native'),
    plugins: [react()],
    resolve: {
        alias: { '@': path.join(root, 'src') },
        dedupe: ['react', 'react-dom'],
    },
    server: {
        host: '127.0.0.1',
        port: Number(process.env.HEREDITY_WEBUI_PORT ?? 5178),
        strictPort: false,
        proxy: {
            '/api': { target: backend, changeOrigin: true },
        },
    },
    build: {
        outDir: path.join(root, 'dist'),
        emptyOutDir: true,
    },
    preview: {
        host: '127.0.0.1',
        port: Number(process.env.HEREDITY_WEBUI_PORT ?? 5178),
        strictPort: false,
        proxy: {
            '/api': { target: backend, changeOrigin: true },
        },
    },
});
