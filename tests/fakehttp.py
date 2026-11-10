"""A stand-in for httpx.AsyncClient: records every request and answers with images. Never touches the network."""
import asyncio
import io
import json
import re
from urllib.parse import urlsplit

from PIL import Image


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def noise(w: int = 256, h: int = 256) -> Image.Image:
    return Image.effect_noise((w, h), 60).convert("RGB")


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", ctype: str = "image/jpeg"):
        self.status_code, self.content, self.headers = status, content, {"content-type": ctype}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)

    async def aiter_bytes(self):
        for i in range(0, len(self.content), 65536):
            yield self.content[i:i + 65536]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def size_of(params: dict) -> tuple[int, int]:
    """The image size a request asks for (ArcGIS size=w,h; WMS WIDTH/HEIGHT; tiles 256)."""
    if "size" in params:
        w, h = str(params["size"]).split(",")
        return int(w), int(h)
    if "WIDTH" in params:
        return int(params["WIDTH"]), int(params["HEIGHT"])
    return 256, 256


class FakeHTTP:
    def __init__(self, answer=None, delay=None, answer_post=None):
        self.calls: list[dict] = []
        self.posts: list[dict] = []
        self.answer_post = answer_post           # (url, json_body, headers) -> FakeResponse
        self.answer = answer or (lambda url, params, headers: FakeResponse(200, jpeg(noise(*size_of(params)))))
        self.delay = delay                       # (url, params) -> seconds to wait before answering

    async def get(self, url, params=None, headers=None, **_):
        if params and urlsplit(url).query:       # what httpx does: the address's own query would be dropped
            raise AssertionError(f"httpx would drop the query of {url} because params are given")
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.delay:
            await asyncio.sleep(self.delay(url, dict(params or {})))
        return self.answer(url, dict(params or {}), dict(headers or {}))

    async def post(self, url, json=None, headers=None, **_):
        self.posts.append({"url": url, "json": dict(json or {}), "headers": dict(headers or {})})
        if self.answer_post is None:
            raise AssertionError(f"must not post to {url}")
        return self.answer_post(url, dict(json or {}), dict(headers or {}))

    def stream(self, method, url, params=None, headers=None, **_):
        fake = self

        class Stream:
            async def __aenter__(self):
                self.r = await fake.get(url, params=params, headers=headers)
                return self.r

            async def __aexit__(self, *exc):
                return False
        return Stream()

    async def aclose(self):
        pass


def no_network(url, params, headers):
    raise AssertionError(f"must not fetch {url}")


def zxy(url: str) -> tuple[int, int, int]:
    z, x, y = re.search(r"/(\d+)/(\d+)/(\d+)\.\w+$", url).groups()
    return int(z), int(x), int(y)
