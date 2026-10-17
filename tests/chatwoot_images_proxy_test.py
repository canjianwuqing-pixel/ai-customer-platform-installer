"""Real Nginx/TLS -> HTTP Chatwoot attachment protocol, no real credentials."""
import asyncio
import socket
import ssl
import unittest
from unittest.mock import patch

import httpx
from fastapi.responses import RedirectResponse, Response

from ai_runtime.chatwoot import ChatwootClient
from ai_runtime.images import ChatwootImages
from ai_runtime.config import RuntimeFailure
from chatwoot_images_test import image_bytes
import website_nginx_test as nginx


class ImageProxyFixtures(unittest.TestCase):
    def setUp(self):
        self.h = nginx.WebsiteNginxFixtures()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.raw = image_bytes()
        self.requests = []

        @self.h.backend.get('/rails/active_storage/{path:path}')
        def attachment(path: str):
            self.requests.append(path)
            if path.startswith('blobs/redirect/'):
                return RedirectResponse('/rails/active_storage/disk/signed-fixture-private-path/picture.png', status_code=302)
            return Response(self.raw, media_type='image/png')

        self.h.start()
        self.url = 'https://' + self.h.domains[0] + ':' + str(self.h.driver.https_port)
        self.row = {'id': 1, 'conversation_id': 100, 'attachments': [{'id': 21, 'message_id': 1,
            'file_type': 'image', 'extension': 'png', 'file_size': len(self.raw),
            'data_url': self.url + '/rails/active_storage/blobs/redirect/signed-fixture-private-path/picture.png'}]}

    def reader(self, trusted):
        context = ssl.create_default_context()
        if trusted:
            for cert in (self.h.driver.directory / 'certbot/live').glob('*/fullchain.pem'):
                context.load_verify_locations(cafile=str(cert))
        transport = httpx.AsyncHTTPTransport(verify=context)
        # An explicit internal endpoint may use loopback ONLY inside this
        # isolated fixture. The real certificate/SNI is still verified.
        client = ChatwootClient({'base_url': self.url, 'account_id': 42, 'inbox_id': 7}, {}, transport,
            attachment_private=True)
        return ChatwootImages(client)

    def test_real_https_redirect_download_and_hostname_verification(self):
        with patch('ai_runtime.images.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', self.h.driver.https_port))]):
            result = asyncio.run(self.reader(True).read(self.row, 100))
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith('data:image/jpeg;base64,'))
        self.assertEqual(len(self.requests), 2)

    def test_invalid_tls_chain_is_refused_without_insecure_fallback(self):
        with patch('ai_runtime.images.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', self.h.driver.https_port))]):
            with self.assertRaisesRegex(RuntimeFailure, 'image_download_failed'):
                asyncio.run(self.reader(False).read(self.row, 100))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
