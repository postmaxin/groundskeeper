# Third-party notices

The panel's own code is [MIT](LICENSE). The Docker image also carries these libraries in `web/vendor/`, fetched at
build time by the `Dockerfile` (pinned, checksummed) and never loaded from a CDN at runtime. They are not stored in git.

| Library | Version | Licence |
|---|---|---|
| MapLibre GL JS | 4.7.1 | BSD-3-Clause |
| three.js | 0.170.0 | MIT |
| Agora Web SDK NG (`agora-rtc-sdk-ng`) | 4.24.9 | Apache-2.0 |

## Agora Web SDK NG

`web/vendor/AgoraRTC_N-production.js` is Agora's unmodified UMD build. It is used under the Apache License 2.0, which
is what Agora's source tree states (root `package.json` of [AgoraIO/agora-rtc-web](https://github.com/AgoraIO/agora-rtc-web)).
The npm package says MIT and ships no LICENSE file, and Agora has a separate proprietary SDK agreement; we have asked
Agora to add a LICENSE file ([agora-rtc-web#14](https://github.com/AgoraIO/agora-rtc-web/issues/14)).

The package has no NOTICE file. The Apache-2.0 text is shipped beside the SDK in the image, as
`web/vendor/LICENSE-agora-rtc-sdk-ng.txt` (from <https://www.apache.org/licenses/LICENSE-2.0.txt>, checksummed in the `Dockerfile`).
