# Third-party notices

The panel's own code is [MIT](LICENSE). The Docker image also carries these libraries in `web/vendor/`, fetched at
build time by the `Dockerfile` (pinned, checksummed) and never loaded from a CDN at runtime. They are not stored in git.

| Library | Version | Licence |
|---|---|---|
| MapLibre GL JS | 4.7.1 | BSD-3-Clause |
| three.js | 0.170.0 | MIT |
| Agora Web SDK NG (`agora-rtc-sdk-ng`) | 4.24.9 | MIT |

## Agora Web SDK NG

`web/vendor/AgoraRTC_N-production.js` is Agora's unmodified UMD build. It is used under the MIT licence, as the npm
package declares (`"license": "MIT"` in the `package.json` of `agora-rtc-sdk-ng@4.24.9`, the artifact we ship).
The root of Agora's GitHub repository ([AgoraIO/agora-rtc-web](https://github.com/AgoraIO/agora-rtc-web)) shows
Apache-2.0; we follow the package's own declaration. Agora also has a separate proprietary SDK agreement, and the
package ships no LICENSE file; we have asked Agora to add one
([agora-rtc-web#14](https://github.com/AgoraIO/agora-rtc-web/issues/14)).

The package states no copyright holder, so the line below uses the package's `author` field, "Agora WebRTC Team".
This file is copied into the image beside the SDK, as `web/vendor/THIRD_PARTY_NOTICES.md`.

```
MIT License

Copyright (c) Agora WebRTC Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
