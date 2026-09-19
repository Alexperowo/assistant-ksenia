import { createServer } from 'node:https';
import { request as httpRequest } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(__dirname, '..');

const crtPath = join(projectRoot, 'certs', 'openhands-lan.crt');
const keyPath = join(projectRoot, 'certs', 'openhands-lan.key');

if (!existsSync(crtPath) || !existsSync(keyPath)) {
  console.error('[Gateway] Сертификаты не найдены в certs/');
  process.exit(1);
}

const cert = readFileSync(crtPath);
const key = readFileSync(keyPath);

const HTTPS_PORT = parseInt(process.env.KSENIA_LAN_PORT || '8765', 10);
const TARGET_HOST = '127.0.0.1';
const TARGET_PORT = parseInt(process.env.KSENIA_BACKEND_PORT || '18765', 10);

const server = createServer({ cert, key, minVersion: 'TLSv1.2' }, (clientReq, clientRes) => {
  const options = {
    hostname: TARGET_HOST,
    port: TARGET_PORT,
    path: clientReq.url,
    method: clientReq.method,
    headers: {
      ...clientReq.headers,
      'x-forwarded-proto': 'https',
      'x-forwarded-for': clientReq.socket.remoteAddress,
    },
  };

  const proxyReq = httpRequest(options, (proxyRes) => {
    clientRes.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(clientRes, { end: true });
  });

  proxyReq.on('error', () => {
    clientRes.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
    clientRes.end('Ксения запускается, подождите несколько секунд...');
  });

  clientReq.pipe(proxyReq, { end: true });
});

server.listen(HTTPS_PORT, '0.0.0.0', () => {
  console.log(`[Ксения PWA Gateway] ONLINE на порту ${HTTPS_PORT} (HTTPS) -> 127.0.0.1:${TARGET_PORT}`);
});
