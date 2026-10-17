"""Ephemeral, bounded Website Inbox images; never fetch a visitor-supplied URL.

Only authenticated conversation API metadata is accepted. The only network
destination is the configured Chatwoot, not arbitrary storage/CDN origins.
"""
import asyncio
import base64
from io import BytesIO
import ipaddress
import re
import socket
import warnings
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from .config import RuntimeFailure


MAX_IMAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_SIDE = 8192
OUTPUT_SIDE = 2048
DOWNLOAD_SECONDS = 20
FORMATS = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'), 'WEBP': ('webp', 'image/webp')}

IMAGE_FEEDBACK = {
    'vision_not_supported': '当前客服模型尚未启用图片识别，我没有查看这张图片。请用文字描述，或联系人工客服。',
    'vision_request_rejected': '模型服务拒绝了图片输入，可能不支持图片识别；我未能查看图片。请改用文字或联系人工客服。',
    'image_invalid_attachment': '图片附件归属或来源未通过安全校验，我未能读取图片。请重新上传或联系人工客服。',
    'image_unsupported': '暂时只支持 PNG、JPEG、WebP 静态图片，我未能读取该附件。请转换格式后重试。',
    'image_limit_exceeded': '图片超出限制：每条最多 3 张、单张 5 MiB、合计 10 MiB，单张不超过 800 万像素且任一边不超过 8192。请压缩或分开发送。',
    'image_download_failed': '图片暂时无法安全下载，可能已过期或网络异常；我没有查看图片。请重新上传，文字咨询仍可继续。',
    'image_invalid_content': '图片文件无法安全解码，我未能查看图片。请重新导出为 PNG、JPEG 或 WebP 后重试。',
    'image_provider_failed': '图片分析请求失败，我尚未获得识别结果。请稍后重试或联系人工客服；已发出的模型请求可能按提供方规则计费。',
}


def origin(url):
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or {'http': 80, 'https': 443}[p.scheme]


def storage_url(value, origins, *, disk_only=False):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) < 33 or c == '\\' for c in value):
            raise ValueError
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.fragment or origin(value) not in origins:
            raise ValueError
        path = unquote(p.path, errors='strict')
        routes = 'disk' if disk_only else r'(?:blobs/(?:redirect|proxy)|disk)'
        if not re.fullmatch('/rails/active_storage/' + routes + r'/[A-Za-z0-9_=+\-]{16,4096}/[^/]{1,255}', path):
            raise ValueError
        if any(part in {'.', '..'} or '\\' in part or '%' in part or any(ord(c) < 32 for c in part) for part in path.split('/')):
            raise ValueError
        if p.query and any(k != 'disposition' or v not in {'inline', 'attachment'} for k, v in parse_qsl(p.query, strict_parsing=True)):
            raise ValueError
        return httpx.URL(value)
    except (ValueError, TypeError, KeyError, UnicodeError, httpx.InvalidURL):
        raise RuntimeFailure('image_invalid_attachment', 422) from None


def normalize_image(raw, extension, content_type):
    """Check headers before decoding, reject animation/bombs, remove metadata."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=list(FORMATS)) as image:
                spec = FORMATS.get(image.format)
                if not spec or extension not in {spec[0], 'jpeg' if spec[0] == 'jpg' else spec[0]} or content_type != spec[1]:
                    raise RuntimeFailure('image_invalid_content', 422)
                width, height = image.size
                if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
                    raise RuntimeFailure('image_limit_exceeded', 422)
                if getattr(image, 'is_animated', False):
                    raise RuntimeFailure('image_unsupported', 422)
                image.load()
                # New pixels only: never send EXIF, file names or opaque metadata.
                with ImageOps.exif_transpose(image) as oriented:
                    oriented.thumbnail((OUTPUT_SIDE, OUTPUT_SIDE))
                    with oriented.convert('RGB') as rgb:
                        # Composite transparency instead of revealing hidden
                        # RGB values beneath fully transparent pixels.
                        if 'A' in oriented.getbands() or 'transparency' in oriented.info:
                            with oriented.convert('RGBA') as rgba, rgba.getchannel('A') as alpha:
                                with ImageOps.invert(alpha) as mask:
                                    rgb.paste('white', mask=mask)
                        rgb.info.clear()
                        output = BytesIO()
                        rgb.save(output, 'JPEG', quality=90)
                        value = output.getvalue()
        if len(value) > MAX_IMAGE_BYTES:
            raise RuntimeFailure('image_limit_exceeded', 422)
        return 'data:image/jpeg;base64,' + base64.b64encode(value).decode('ascii')
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise RuntimeFailure('image_invalid_content', 422) from None


class ChatwootImages:
    def __init__(self, client):
        self.client = client

    def metadata(self, message, conversation_id):
        if (type(message.get('conversation_id')) is not int or message['conversation_id'] != conversation_id
                or type(message.get('id')) is not int or message['id'] <= 0):
            raise RuntimeFailure('image_invalid_attachment', 422)
        attachments = message.get('attachments')
        if not isinstance(attachments, list) or not attachments:
            raise RuntimeFailure('image_invalid_attachment', 422)
        if len(attachments) > MAX_IMAGES:
            raise RuntimeFailure('image_limit_exceeded', 422)
        seen, total, records = set(), 0, []
        for item in attachments:
            if (not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0 or item['id'] in seen
                    or type(item.get('message_id')) is not int or item['message_id'] != message['id']
                    or any(key in item and item[key] != self.client.binding[key] for key in ('account_id', 'inbox_id'))):
                raise RuntimeFailure('image_invalid_attachment', 422)
            seen.add(item['id'])
            extension = item.get('extension', '')
            extension = extension.lower() if isinstance(extension, str) else ''
            file_type = item.get('file_type')
            if not (file_type == 'image' or type(file_type) is int and file_type == 0) or extension not in {'png', 'jpeg', 'jpg', 'webp'}:
                raise RuntimeFailure('image_unsupported', 422)
            size = item.get('file_size')
            if type(size) is not int or size <= 0 or size > MAX_IMAGE_BYTES:
                raise RuntimeFailure('image_limit_exceeded', 422)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise RuntimeFailure('image_limit_exceeded', 422)
            for key in ('width', 'height'):
                value = item.get(key)
                if value is not None and (type(value) is not int or not 0 < value <= MAX_SIDE):
                    raise RuntimeFailure('image_limit_exceeded', 422)
            if item.get('width') and item.get('height') and item['width'] * item['height'] > MAX_PIXELS:
                raise RuntimeFailure('image_limit_exceeded', 422)
            url = storage_url(item.get('data_url'), self.client.attachment_origins)
            records.append((url, extension, size))
        return records

    async def endpoint(self):
        """Pin DNS once for the entire download, retaining TLS hostname checks."""
        base = httpx.URL(self.client.binding['base_url'])
        rows = await asyncio.to_thread(socket.getaddrinfo, base.host, base.port or (443 if base.scheme == 'https' else 80), 0, socket.SOCK_STREAM)
        ips = list(dict.fromkeys(row[4][0] for row in rows))
        if not ips or any('%' in ip or ipaddress.ip_address(ip).is_unspecified or ipaddress.ip_address(ip).is_multicast
                          or ipaddress.ip_address(ip).is_link_local for ip in ips):
            raise RuntimeFailure('image_download_failed', 502)
        # Private Docker addresses are allowed ONLY for the installer's explicit
        # internal Rails endpoint. Public Chatwoot DNS must not rebind inward.
        if not self.client.attachment_private and any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise RuntimeFailure('image_download_failed', 502)
        return base, ips[0]

    async def download(self, url, size, base, ip):
        async with httpx.AsyncClient(transport=self.client.transport, timeout=httpx.Timeout(5, connect=3),
                follow_redirects=False, trust_env=False) as http:
            for hop in range(2):
                # Origin/path already validated. Only reach the same pinned
                # Chatwoot; do not forward API tokens, cookies or signed URLs to
                # any external host. No automatic redirect machinery is used.
                target = base.copy_with(path=url.path, query=url.query).copy_with(host=ip)
                http.cookies.clear()
                async with http.stream('GET', target, headers={'Host': base.netloc.decode(), 'Accept-Encoding': 'identity'},
                        extensions={'sni_hostname': base.host}) as response:
                    if response.status_code in {301, 302, 303, 307, 308} and hop == 0 and '/blobs/redirect/' in url.path:
                        url = storage_url(urljoin(str(url), response.headers.get('location', '')), self.client.attachment_origins, disk_only=True)
                        continue
                    if response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity':
                        raise RuntimeFailure('image_download_failed', 502)
                    declared = response.headers.get('content-length')
                    if declared is not None and (not declared.isdecimal() or int(declared) != size):
                        raise RuntimeFailure('image_invalid_content', 422)
                    media = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if media not in {spec[1] for spec in FORMATS.values()}:
                        raise RuntimeFailure('image_unsupported', 422)
                    raw = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        if len(raw) + len(chunk) > size or len(raw) + len(chunk) > MAX_IMAGE_BYTES:
                            raise RuntimeFailure('image_limit_exceeded', 422)
                        raw.extend(chunk)
                    if len(raw) != size:
                        raise RuntimeFailure('image_invalid_content', 422)
                    return raw, media
        raise RuntimeFailure('image_download_failed', 502)

    async def read(self, message, conversation_id):
        records = self.metadata(message, conversation_id)
        try:
            async with asyncio.timeout(DOWNLOAD_SECONDS):
                base, ip = await self.endpoint()
                images = []
                for url, extension, size in records:
                    raw, media = await self.download(url, size, base, ip)
                    images.append(await run_in_threadpool(normalize_image, raw, extension, media))
                return tuple(images)
        except (TimeoutError, httpx.HTTPError, OSError, ValueError):
            raise RuntimeFailure('image_download_failed', 502) from None


def probe_image():
    """Small synthetic image for an explicit administrator-initiated probe."""
    output = BytesIO()
    with Image.new('RGB', (16, 16), 'blue') as image:
        image.save(output, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
