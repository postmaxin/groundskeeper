"""The Content-Security-Policy the panel sends while Robot video is on (design §13).

Only `connect-src`, nothing else: the panel uses inline styles and blob: workers, and a full policy is a much bigger
change. The point is narrow: the Agora Web SDK's statistics and log hosts must be unreachable from the page even if the
SDK's own switches (`ENABLE_EVENT_REPORT`, `setParameter`) stop working. A source list cannot say "all of *.agora.io
except statscollector-*", so it names the gateway-choice hosts and the signalling subdomains and nothing broader.

Every other request the panel's pages make goes to the panel itself: tiles, elevation, Frigate pictures and the rest
are fetched by the server and relayed, so no other host is listed (and imagery hosts are deliberately not).
"""

# Gateway choice (POST /api/v2/transpond/webrtc): the first two are the SDK's defaults for the "ap" area, the wildcard
# is the sd-rtn.com family it falls back to (a capture on 2026-11-11 saw webrtc2-2.ap.sd-rtn.com). Statistics hosts
# are statscollector-*.agora.io and *.statscollector.sd-rtn.com; none of them ends in .ap.sd-rtn.com.
GATEWAY = ("https://webrtc2-ap-web-1.agora.io", "https://webrtc2-ap-web-3.agora.io", "https://*.ap.sd-rtn.com")
# Signalling: the host name carries the gateway's IP, so it changes per session; the port is the gateway's.
EDGE = ("wss://*.edge.agora.io:*", "wss://*.edge.sd-rtn.com:*")

CONNECT_SRC = ("'self'", "blob:", "data:") + GATEWAY + EDGE


def video_policy() -> str:
    return "connect-src " + " ".join(CONNECT_SRC)
