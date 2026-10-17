"""Offline Chatwoot attachment -> selected OpenAI-compatible vision protocol."""
import asyncio
import base64
import copy
from io import BytesIO
import json
import logging
import socket
import unittest
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

import openai_chatwoot_test as integration
from admin_forms_test import Editor
from ai_runtime.chatwoot import ChatwootBridge, ChatwootClient
from ai_runtime.config import RuntimeFailure, ServiceResolver
from ai_runtime.engine import RuntimeEngine, ChatProvider
from ai_runtime.images import ChatwootImages, IMAGE_FEEDBACK, MAX_IMAGE_BYTES, normalize_image, storage_url


def image_bytes(fmt='PNG', color='blue'):
    stream = BytesIO()
    with Image.new('RGB', (300, 100), color) as image:
        ImageDraw.Draw(image).text((10, 40), 'ERROR: connection refused', fill='white')
        image.save(stream, fmt)
    return stream.getvalue()


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data, delay=0):
        self.data, self.delay = data, delay

    async def __aiter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        for offset in range(0, len(self.data), 1024):
            yield self.data[offset:offset + 1024]


class ImageFlowFixtures(unittest.TestCase):
    def setUp(self):
        self.f = integration.IntegrationFixtures()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.prepared()
        self.enable_vision()
        self.downloads, self.inputs, self.files = [], [], {}
        self.redirect = None
        self.failure = None
        self.provider_code = 200
        self.delay = 0
        self.f.bridge.transport = httpx.MockTransport(self.chatwoot)
        self.f.engine.provider.transport = httpx.MockTransport(self.provider)
        self.dns = patch('ai_runtime.images.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def enable_vision(self, enabled=True, key='model_a'):
        profile = self.f.api.get('/management/profiles/ai/' + key).json()
        profile.pop('id')
        profile['supports_vision'] = enabled
        result = self.f.api.put('/management/profiles/ai/' + key, json=profile)
        self.assertEqual(result.status_code, 200, result.text)

    def picture(self, content=None, *, cid=100, inbox=7, fmt='PNG', color='blue'):
        row = self.f.protocol.visitor(conversation_id=cid, inbox_id=inbox, content=content)
        extension, media = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'), 'WEBP': ('webp', 'image/webp')}[fmt]
        path = '/rails/active_storage/blobs/redirect/' + ('signed-private-token-' + str(row['id']).zfill(4)) + '/picture.' + extension
        data = image_bytes(fmt, color)
        self.files[path] = data, media
        self.files[path.replace('/blobs/redirect/', '/disk/')] = data, media
        row['attachments'] = [{'id': row['id'] + 20, 'message_id': row['id'], 'file_type': 'image',
            'extension': extension, 'file_size': len(data), 'width': 300, 'height': 100,
            'data_url': 'https://chatwoot.example.test' + path}]
        return row

    def chatwoot(self, request):
        if not request.url.path.startswith('/rails/active_storage/'):
            return self.f.protocol(request)
        self.downloads.append(request)
        self.assertNotIn('api_access_token', request.headers)
        self.assertNotIn('authorization', request.headers)
        self.assertNotIn('cookie', request.headers)
        self.assertEqual(request.headers['host'], 'chatwoot.example.test')
        self.assertEqual(request.extensions['sni_hostname'], 'chatwoot.example.test')
        self.assertEqual(request.url.host, '93.184.215.14')
        if self.failure:
            return httpx.Response(self.failure)
        if '/blobs/redirect/' in request.url.path:
            location = self.redirect or str(request.url.copy_with(host='chatwoot.example.test', path=request.url.path.replace('/blobs/redirect/', '/disk/')))
            return httpx.Response(302, headers={'location': location, 'set-cookie': 'must-not-forward=private'})
        data, media = self.files[request.url.path]
        return httpx.Response(200, headers={'content-type': media, 'content-length': str(len(data))}, stream=BytesStream(data, self.delay))

    def provider(self, request):
        body = json.loads(request.content)
        self.inputs.append(body)
        if self.provider_code != 200:
            return httpx.Response(self.provider_code, json={'error': {'message': 'PRIVATE_PROVIDER_BODY'}})
        content = body['messages'][-1]['content']
        if isinstance(content, str):
            return httpx.Response(200, json={'choices': [{'message': {'content': '文字回复：' + content}}]})
        self.assertIn(body['model'], {'vendor/arbitrary-model-2077', 'other/vision-model'})
        self.assertTrue(any(p['type'] == 'image_url' for p in content))
        self.assertTrue(all(p['image_url']['url'].startswith('data:image/') for p in content if p['type'] == 'image_url'))
        return httpx.Response(200, json={'choices': [{'message': {'content': '模拟图片分析：连接被拒绝，请检查服务地址和端口。'}}]})

    def deliver(self, row, service='service_a'):
        result = self.f.hook(row, service)
        self.assertEqual(result.json()['status'], 'accepted', result.text)
        self.assertTrue(self.f.tick())

    def feedback(self, row, expected):
        before = len(self.inputs)
        self.deliver(row)
        self.assertEqual(self.f.protocol.sent[-1]['content'], IMAGE_FEEDBACK[expected])
        self.assertEqual(len(self.inputs), before)

    def test_empty_text_image_is_queued_instead_of_silently_ignored(self):
        row = self.picture()
        self.assertEqual(self.f.hook(row).json()['status'], 'accepted')
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.assertEqual(len(self.downloads), 2)
        self.assertIsInstance(self.inputs[-1]['messages'][-1]['content'], list)

    def test_png_jpeg_webp_with_text_and_software_error_request(self):
        for fmt in ('PNG', 'JPEG', 'WEBP'):
            with self.subTest(fmt=fmt):
                self.deliver(self.picture('请分析截图中的软件错误', cid=100 + len(self.inputs), fmt=fmt))
                parts = self.inputs[-1]['messages'][-1]['content']
                self.assertEqual(parts[0]['text'], '请分析截图中的软件错误')
                raw = base64.b64decode(parts[1]['image_url']['url'].split(',', 1)[1])
                with Image.open(BytesIO(raw)) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.size, (300, 100))
                    self.assertEqual(dict(image.getexif()), {})
                self.assertIn('连接被拒绝', self.f.protocol.sent[-1]['content'])

    def test_text_and_context_still_work_without_redownloading_historical_images(self):
        self.deliver(self.picture('这是截图'))
        count = len(self.downloads)
        self.deliver(self.f.protocol.visitor(content='然后怎么办？'))
        self.assertEqual(len(self.downloads), count)
        self.assertEqual(self.inputs[-1]['messages'][-1]['content'], '然后怎么办？')
        history = json.dumps(self.inputs[-1], ensure_ascii=False)
        self.assertIn('未重新读取历史图片', history)
        self.assertNotIn('data:image', history)
        self.assertNotIn('active_storage', history)

    def test_duplicate_webhook_and_restart_do_not_download_or_reply_again(self):
        row = self.picture()
        self.deliver(row)
        self.f.hook(row)
        self.assertFalse(self.f.tick())
        new = ChatwootBridge(RuntimeEngine(ServiceResolver(self.f.resolver.config_dir, self.f.resolver.data_dir, self.f.resolver.env)))
        self.assertFalse(asyncio.run(new.tick()))
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.assertEqual(len(self.downloads), 2)

    def test_multiple_images_are_combined_once_and_uppercase_extension_is_supported(self):
        row = self.picture()
        extra = copy.deepcopy(row['attachments'][0])
        extra.update(id=99, extension='PNG')
        row['attachments'].append(extra)
        self.deliver(row)
        parts = self.inputs[-1]['messages'][-1]['content']
        self.assertEqual(len([p for p in parts if p['type'] == 'image_url']), 2)
        self.assertEqual(len(self.f.protocol.sent), 1)

    def test_identity_or_conversation_mismatch_prevents_attachment_access(self):
        row = self.picture()
        self.assertEqual(self.f.hook(row).json()['status'], 'accepted')
        self.f.protocol.conversations[100]['meta']['sender']['id'] += 1
        self.f.tick()
        self.assertEqual(self.f.protocol.sent[-1]['content'], IMAGE_FEEDBACK['image_invalid_attachment'])
        row = self.picture(cid=101)
        verified = copy.deepcopy(row)
        verified['conversation_id'] = 999
        original = self.f.protocol.__call__
        def corrupt_api(request):
            response = original(request)
            if request.url.path.endswith('/messages') and request.method == 'GET':
                body = response.json()
                body['payload'] = [verified if x['id'] == row['id'] else x for x in body['payload']]
                return httpx.Response(200, json=body)
            return response
        self.f.bridge.transport = httpx.MockTransport(corrupt_api)
        self.feedback(row, 'image_invalid_attachment')
        self.assertEqual(self.downloads, [])

    def test_model_flag_missing_or_disabled_never_downloads_or_claims_vision(self):
        self.enable_vision(False)
        self.feedback(self.picture(), 'vision_not_supported')
        self.assertEqual(self.downloads, [])
        self.assertIn('我没有查看', self.f.protocol.sent[-1]['content'])
        legacy = self.f.resolver._runtime_config('legacy', {'model': 'arbitrary-existing-model'}, {}, {})
        self.assertFalse(legacy.supports_vision)
        response = self.f.api.put('/management/profiles/ai/model_a', json={'model': 'arbitrary', 'supports_vision': 'true'})
        self.assertEqual(response.status_code, 422)

    def test_plain_text_with_null_attachments_remains_valid(self):
        row = self.f.protocol.visitor(content='纯文字消息')
        row['attachments'] = None
        self.deliver(row)
        self.assertEqual(self.f.protocol.sent[-1]['content'], '文字回复：纯文字消息')
        self.assertEqual(self.downloads, [])

    def test_malformed_metadata_is_reported_without_poisoning_message_queue(self):
        cases = (('message_id', True, 'image_invalid_attachment'), ('file_type', {}, 'image_unsupported'),
                 ('width', True, 'image_limit_exceeded'))
        for i, (key, value, code) in enumerate(cases):
            row = self.picture(cid=100 + i)
            row['attachments'][0][key] = value
            self.feedback(row, code)
        self.assertEqual(self.downloads, [])

    def test_provider_vision_rejection_and_failure_are_honest_and_not_retried(self):
        for code in (400, 415, 422, 429, 500):
            self.provider_code = code
            self.deliver(self.picture(cid=100 + code))
            expected = 'vision_request_rejected' if code in (400, 415, 422) else 'image_provider_failed'
            self.assertEqual(self.f.protocol.sent[-1]['content'], IMAGE_FEEDBACK[expected])
        self.assertEqual(len(self.inputs), 5)
        self.assertFalse(self.f.tick())

    def test_bad_format_and_count_size_pixel_limits_fail_before_any_download(self):
        mutations = [lambda a: a.update(extension='svg'), lambda a: a.update(file_size=MAX_IMAGE_BYTES + 1),
            lambda a: a.update(width=8193), lambda a: a.update(width=4000, height=4000)]
        for i, change in enumerate(mutations):
            row = self.picture(cid=100 + i)
            change(row['attachments'][0])
            self.feedback(row, 'image_unsupported' if i == 0 else 'image_limit_exceeded')
        row = self.picture(cid=110)
        row['attachments'] *= 4
        self.feedback(row, 'image_limit_exceeded')
        row = self.picture(cid=111)
        row['attachments'] *= 3
        for i in range(3):
            row['attachments'][i] = {**row['attachments'][i], 'id': 100 + i, 'file_size': MAX_IMAGE_BYTES}
        self.feedback(row, 'image_limit_exceeded')
        self.assertEqual(self.downloads, [])

    def test_message_attachment_account_inbox_and_duplicate_id_mismatches_rejected(self):
        for i, key in enumerate(('message_id', 'account_id', 'inbox_id')):
            row = self.picture(cid=100 + i)
            row['attachments'][0][key] = 999
            self.feedback(row, 'image_invalid_attachment')
        row = self.picture(cid=110)
        row['attachments'] *= 2
        self.feedback(row, 'image_invalid_attachment')
        self.assertEqual(self.downloads, [])

    def test_untrusted_urls_and_redirects_cannot_be_used_for_ssrf(self):
        result = self.f.api.post('/chat', json={'message': 'test', 'images': ['http://169.254.169.254/']},
            headers={'authorization': 'Bearer ' + self.f.resolver.env['INTERNAL_API_TOKEN']})
        self.assertEqual(result.status_code, 422)  # no new arbitrary-URL chat/upload API
        for i, value in enumerate(('http://169.254.169.254/latest/meta-data/', 'https://evil.example.test/x',
                'https://chatwoot.example.test/api/v1/accounts/42/inboxes',
                'https://user:pass@chatwoot.example.test/rails/active_storage/disk/abcdefghijklmnop/x',
                'https://chatwoot.example.test/rails/active_storage/disk/abcdefghijklmnop/%2e%2e',
                'https://chatwoot.example.test/rails/active_storage/disk/abcdefghijklmnop/x?url=http://localhost/')):
            row = self.picture(cid=100 + i)
            row['attachments'][0]['data_url'] = value
            self.feedback(row, 'image_invalid_attachment')
        self.assertEqual(self.downloads, [])
        self.redirect = 'https://evil.example.test/rails/active_storage/disk/abcdefghijklmnop/x.png'
        self.feedback(self.picture(cid=120), 'image_invalid_attachment')
        self.assertEqual(len(self.downloads), 1)
        self.assertNotIn('evil.example.test', str(self.downloads[0].url))

    def test_dns_rebinding_to_private_addresses_is_refused(self):
        with patch('ai_runtime.images.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443))]):
            self.feedback(self.picture(), 'image_download_failed')
        self.assertEqual(self.downloads, [])

    def test_only_metadata_from_authenticated_message_api_is_read(self):
        row = self.picture()
        event = copy.deepcopy(row)
        event['attachments'][0]['data_url'] = 'http://169.254.169.254/evil'
        self.deliver(event)
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(len(self.downloads), 2)

    def test_failed_download_does_not_pause_service_or_other_normal_messages(self):
        self.failure = 404
        self.feedback(self.picture(), 'image_download_failed')
        self.assertFalse(self.f.bridge.conversation_state('service_a', 100)[0])
        self.failure = None
        self.deliver(self.f.protocol.visitor(content='继续文字咨询'))
        self.assertEqual(self.f.protocol.sent[-1]['content'], '文字回复：继续文字咨询')

    def test_download_deadline_and_wrong_mime_are_safe(self):
        self.delay = .3
        with patch('ai_runtime.images.DOWNLOAD_SECONDS', .05):
            self.feedback(self.picture(), 'image_download_failed')
        self.delay = 0
        row = self.picture(cid=101)
        path = row['attachments'][0]['data_url'].split('example.test')[1].replace('/blobs/redirect/', '/disk/')
        self.files[path] = (self.files[path][0], 'text/html')
        self.feedback(row, 'image_unsupported')

    def test_stream_limit_size_mismatch_compression_and_redirect_loops_are_rejected(self):
        original = self.chatwoot
        mode = ''
        def response(request):
            if '/disk/' not in request.url.path:
                return original(request)
            self.downloads.append(request)
            if mode == 'loop':
                return httpx.Response(302, headers={'location': str(request.url)})
            raw, media = self.files[request.url.path]
            if mode == 'length':
                return httpx.Response(200, headers={'content-type': media, 'content-length': str(len(raw) + 1)}, stream=BytesStream(raw))
            if mode == 'compression':
                return httpx.Response(200, headers={'content-type': media, 'content-encoding': 'gzip'}, stream=BytesStream(raw))
            return httpx.Response(200, headers={'content-type': media}, stream=BytesStream(raw + b'overflow'))
        self.f.bridge.transport = httpx.MockTransport(response)
        for i, (mode, expected) in enumerate((('length', 'image_invalid_content'), ('compression', 'image_download_failed'),
                ('bytes', 'image_limit_exceeded'), ('loop', 'image_download_failed'))):
            self.feedback(self.picture(cid=100 + i), expected)

    def test_local_chatwoot_uses_only_explicit_internal_endpoint_and_safe_origins(self):
        self.f.resolver.env.update(CHATWOOT_LOCAL_ORIGIN='https://chatwoot.example.test', CHATWOOT_INTERNAL_URL='http://rails:3000')
        row = self.picture()
        requests = []
        def internal(request):
            if request.url.path.startswith('/rails/active_storage/'):
                requests.append(request)
                self.assertEqual(request.url.host, '172.20.0.9')
                self.assertEqual(request.headers['host'], 'rails:3000')
                self.assertNotIn('api_access_token', request.headers)
                raw, media = self.files[request.url.path]
                return httpx.Response(200, headers={'content-type': media}, stream=BytesStream(raw))
            return self.f.protocol(request)
        self.f.bridge.transport = httpx.MockTransport(internal)
        with patch('ai_runtime.images.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('172.20.0.9', 3000))]):
            self.deliver(row)
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(self.inputs), 1)

    def test_provider_cannot_persist_image_data_even_if_echoed_in_reply(self):
        bare = False
        def echo(request):
            body = json.loads(request.content)
            data = body['messages'][-1]['content'][1]['image_url']['url']
            return httpx.Response(200, json={'choices': [{'message': {'content': data.split(',', 1)[1] if bare else data}}]})
        self.f.engine.provider.transport = httpx.MockTransport(echo)
        self.deliver(self.picture())
        self.assertEqual(self.f.protocol.sent[-1]['content'], '[图片数据已隐藏]')
        self.assertNotIn(b'data:image/', self.f.bridge.store.path.read_bytes())
        bare = True
        self.deliver(self.picture(cid=101))
        self.assertEqual(self.f.protocol.sent[-1]['content'], '[图片数据已隐藏]')

    def test_human_takeover_prevents_download_and_inflight_image_send(self):
        row = self.picture()
        self.f.hook(self.f.protocol.human())
        self.deliver(row)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.f.protocol.sent, [])
        # A different conversation is initially bot-owned; human wins if they
        # reply while the selected model is generating its answer.
        original = self.provider
        def takeover(request):
            result = original(request)
            self.f.hook(self.f.protocol.human(101))
            return result
        self.f.engine.provider.transport = httpx.MockTransport(takeover)
        self.deliver(self.picture(cid=101))
        self.assertEqual(self.f.protocol.sent, [])

    def test_two_inboxes_keep_images_prompts_and_knowledge_isolated(self):
        self.f.model('model_b', secret=self.f.key_b, model='other/vision-model')
        self.enable_vision(key='model_b')
        self.f.service('service_b', model='model_b')
        self.f.connect('service_b', 8)
        for sid in ('service_a', 'service_b'):
            for kind, record in (('prompts', {'id': sid, 'system_prompt': 'prompt-' + sid}),
                    ('knowledge', {'id': sid, 'source': 'knowledge.txt'})):
                self.assertEqual(self.f.api.post('/management/profiles/' + kind, json=record).status_code, 201)
            root = self.f.resolver.data_dir / 'services' / sid
            root.mkdir(parents=True, exist_ok=True)
            (root / 'knowledge.txt').write_text('knowledge-' + sid, encoding='utf-8')
            self.assertEqual(self.f.api.patch('/management/services/' + sid, json={'prompt_config': sid, 'knowledge_config': sid}).status_code, 200)
        a = self.picture(color='blue')
        b = self.picture(cid=101, inbox=8, color='red')
        self.deliver(a)
        self.deliver(b, 'service_b')
        for index, sid in enumerate(('service_a', 'service_b')):
            other = 'service_b' if sid == 'service_a' else 'service_a'
            text = json.dumps(self.inputs[index])
            self.assertIn('prompt-' + sid, text)
            self.assertIn('knowledge-' + sid, text)
            self.assertNotIn(other, text)
        self.assertNotEqual(self.inputs[0]['messages'][-1]['content'][1], self.inputs[1]['messages'][-1]['content'][1])
        self.assertEqual([item['model'] for item in self.inputs], ['vendor/arbitrary-model-2077', 'other/vision-model'])
        self.assertEqual(self.f.hook(a, 'service_b').status_code, 422)

    def test_admin_switch_persists_selected_profile_without_key_change_and_probe_uses_image(self):
        page = self.f.client.get('/admin/edit/ai/model_a')
        form = Editor(page.text).data
        self.assertEqual(form['supports_vision'], 'true')
        form.update(api_key=self.f.key_a)
        self.assertEqual(self.f.client.post('/admin/edit/ai/model_a', data=form).status_code, 303)
        self.assertTrue(self.f.resolver.resolve('service_a').supports_vision)
        response = self.f.api.post('/management/models/test', json={'profile_id': 'model_a',
            'model': 'vendor/arbitrary-model-2077', 'supports_vision': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(self.inputs[-1]['messages'][-1]['content'], list)
        restarted = ServiceResolver(self.f.resolver.config_dir, self.f.resolver.data_dir, self.f.resolver.env)
        self.assertTrue(restarted.resolve('service_a').supports_vision)

    def test_image_payload_urls_tokens_are_not_logged_or_persisted(self):
        row = self.picture()
        with self.assertLogs('ai_runtime', level=logging.INFO) as logs:
            self.deliver(row)
        text = '\n'.join(logs.output)
        data_url = self.inputs[-1]['messages'][-1]['content'][1]['image_url']['url']
        for secret in (row['attachments'][0]['data_url'], 'signed-private-token', self.f.protocol.token, self.f.key_a, data_url):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret.encode(), self.f.bridge.store.path.read_bytes())


class ImageDecodeFixtures(unittest.TestCase):
    def test_magic_mismatch_truncation_and_forged_small_metadata_are_rejected(self):
        for raw, ext, media in ((b'<svg>fake</svg>', 'png', 'image/png'), (image_bytes()[:40], 'png', 'image/png'),
                (image_bytes(), 'jpg', 'image/jpeg')):
            with self.assertRaises(RuntimeFailure):
                normalize_image(raw, ext, media)
        output = BytesIO()
        with Image.new('L', (8193, 1)) as image:
            image.save(output, 'PNG')
        with self.assertRaisesRegex(RuntimeFailure, 'image_limit_exceeded'):
            normalize_image(output.getvalue(), 'png', 'image/png')

    def test_animation_rejected_and_large_valid_image_is_resized(self):
        output = BytesIO()
        with Image.new('RGB', (100, 100), 'red') as a, Image.new('RGB', (100, 100), 'blue') as b:
            a.save(output, 'PNG', save_all=True, append_images=[b], duration=100)
        with self.assertRaisesRegex(RuntimeFailure, 'image_unsupported'):
            normalize_image(output.getvalue(), 'png', 'image/png')
        output = BytesIO()
        with Image.new('RGB', (3000, 1000)) as image:
            image.save(output, 'PNG')
        result = normalize_image(output.getvalue(), 'png', 'image/png')
        with Image.open(BytesIO(base64.b64decode(result.split(',', 1)[1]))) as image:
            self.assertLessEqual(max(image.size), 2048)
        output = BytesIO()
        with Image.new('RGBA', (10, 10), (250, 0, 0, 0)) as transparent:
            transparent.save(output, 'PNG')
        result = normalize_image(output.getvalue(), 'png', 'image/png')
        with Image.open(BytesIO(base64.b64decode(result.split(',', 1)[1]))) as image:
            self.assertTrue(all(channel >= 250 for channel in image.getpixel((5, 5))))


if __name__ == '__main__':
    unittest.main()
