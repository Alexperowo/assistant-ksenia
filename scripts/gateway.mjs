import { createServer as createNetServer } from 'node:net';
import { createServer as createHttpsServer } from 'node:https';
import { createServer as createHttpServer, request as httpRequest } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(__dirname, '..');

const kseniaCrt = join(projectRoot, 'certs', 'ksenia-lan.crt');
const kseniaKey = join(projectRoot, 'certs', 'ksenia-lan.key');
const openhandsCrt = join(projectRoot, 'certs', 'openhands-lan.crt');
const openhandsKey = join(projectRoot, 'certs', 'openhands-lan.key');

const crtPath = existsSync(kseniaCrt) ? kseniaCrt : openhandsCrt;
const keyPath = existsSync(kseniaKey) ? kseniaKey : openhandsKey;

if (!existsSync(crtPath) || !existsSync(keyPath)) {
  console.error('[Gateway] Сертификаты не найдены в certs/');
  process.exit(1);
}

const cert = readFileSync(crtPath);
const key = readFileSync(keyPath);

const GATEWAY_PORT = parseInt(process.env.KSENIA_LAN_PORT || '8765', 10);
const TARGET_HOST = '127.0.0.1';
const TARGET_PORT = parseInt(process.env.KSENIA_BACKEND_PORT || '18765', 10);

function handleProxy(clientReq, clientRes, proto) {
  const options = {
    hostname: TARGET_HOST,
    port: TARGET_PORT,
    path: clientReq.url,
    method: clientReq.method,
    headers: {
      ...clientReq.headers,
      'x-forwarded-proto': proto,
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
}

const httpsServer = createHttpsServer({ cert, key, minVersion: 'TLSv1.2' }, (req, res) => {
  handleProxy(req, res, 'https');
});

const httpServer = createHttpServer((req, res) => {
  const accept = req.headers.accept || '';
  const isHtmlNavigation = (req.method === 'GET' && (req.url === '/' || req.url.startsWith('/?') || accept.includes('text/html')));
  if (isHtmlNavigation) {
    const host = req.headers.host || `192.168.0.14:${GATEWAY_PORT}`;
    const targetUrl = `https://${host}${req.url}`;
    res.writeHead(302, {
      'Location': targetUrl,
      'Content-Type': 'text/plain; charset=utf-8',
    });
    res.end(`Перенаправление на защищённое соединение: ${targetUrl}`);
    return;
  }
  handleProxy(req, res, 'http');
});

for (const srv of [httpsServer, httpServer]) {
  srv.on('clientError', (err, socket) => {
    if (socket.writable) {
      socket.end('HTTP/1.1 400 Bad Request\r\n\r\n');
    } else {
      socket.destroy();
    }
  });
}

// Dual HTTP/HTTPS Socket Sniffer
const netServer = createNetServer((socket) => {
  socket.on('error', (_err) => {
    // Gracefully absorb client ECONNRESET / EPIPE from mobile browsers or network switches
  });

  socket.once('data', (buf) => {
    socket.pause();
    socket.unshift(buf);
    if (buf[0] === 0x16) {
      httpsServer.emit('connection', socket);
    } else {
      httpServer.emit('connection', socket);
    }
    process.nextTick(() => socket.resume());
  });
});

netServer.on('error', (err) => {
  console.error('[Ксения Gateway] Ошибка сокета сервера:', err);
});

process.on('uncaughtException', (err) => {
  if (['ECONNRESET', 'EPIPE', 'ETIMEDOUT', 'ERR_STREAM_DESTROYED'].includes(err.code)) {
    return;
  }
  console.error('[Ксения Gateway] Предупреждение:', err.message);
});

netServer.listen(GATEWAY_PORT, '0.0.0.0', () => {
  console.log(`[Ксения Dual Gateway] ONLINE на порту ${GATEWAY_PORT} (HTTP & HTTPS) -> 127.0.0.1:${TARGET_PORT}`);
  console.log(`- HTTP:  http://192.168.0.14:${GATEWAY_PORT}/ (Прямой вход без TLS/сертификатов)`);
  console.log(`- HTTPS: https://192.168.0.14:${GATEWAY_PORT}/ (PWA режим с сертификатом)`);
});

process.on('SIGINT', () => { netServer.close(); process.exit(0); });
process.on('SIGTERM', () => { netServer.close(); process.exit(0); });
