# Third-party notices

The panel's own code is [MIT](LICENSE). The Docker image also carries these libraries in `web/vendor/`, fetched at
build time by the `Dockerfile` (pinned, checksummed) and never loaded from a CDN at runtime. They are not stored in git.

| Library | Version | Licence |
|---|---|---|
| MapLibre GL JS | 4.7.1 | BSD-3-Clause |
| three.js | 0.170.0 | MIT |
| Agora Web SDK NG (`agora-rtc-sdk-ng`) | 4.24.9 | MIT |

The image also installs Yarbo's own Python SDK from PyPI, which the panel uses to sign in to the Yarbo cloud:

| Library | Version | Licence |
|---|---|---|
| Yarbo Data SDK (`yarbo-data-sdk`) | 0.3.0 | PolyForm Noncommercial 1.0.0 |

From 0.3.0 Yarbo publishes the SDK under the PolyForm Noncommercial licence (0.2.1 and earlier were MIT; the code is
the same). The panel's own code stays MIT, but the Docker image includes the SDK, so the image as a whole may be used
for noncommercial purposes only.

## Agora Web SDK NG

`web/vendor/AgoraRTC_N-production.js` is Agora's unmodified UMD build. It is used under the MIT licence, as the npm
package declares (`"license": "MIT"` in the `package.json` of `agora-rtc-sdk-ng@4.24.9`, the artifact we ship).
The root of Agora's GitHub repository ([AgoraIO/agora-rtc-web](https://github.com/AgoraIO/agora-rtc-web)) shows
Apache-2.0; we follow the package's own declaration. Agora also has a separate proprietary SDK agreement, and the
package ships no LICENSE file; we have asked Agora to add one
([agora-rtc-web#14](https://github.com/AgoraIO/agora-rtc-web/issues/14)).

The package states no copyright holder in `package.json` and has no LICENSE file, but the bundle's own header reads
`AgoraWebSDK_N-v4.24.9-0-g2319458dd Copyright AgoraInc.`, so the line below uses that attribution (the package's
`author` field is "Agora WebRTC Team"). The bundle also contains small third-party modules (for example core-js) that
carry their own notices inside the file.
This file is copied into the image beside the SDK, as `web/vendor/THIRD_PARTY_NOTICES.md`.

```
MIT License

Copyright AgoraInc.

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
