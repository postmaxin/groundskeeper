# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# MapLibre GL, three.js and the Agora Web SDK, vendored at build time (checked against their hashes) so the UI needs no CDN at runtime.
# three.js is only loaded by the 3D map (web/robot3d.js). The Agora SDK is Apache-2.0: its licence text ships beside it
# (LICENSE-agora-rtc-sdk-ng.txt, section 4(a)); the package has no NOTICE file, and the SDK file is served unmodified.
# (The folder is made first: ADD --chmod would give the folders it creates mode 644, unreadable to a non-root user.)
RUN mkdir -p /app/web/vendor
ADD --chmod=644 --checksum=sha256:be9633c4d870e26fb37f1cfe5c5a77181667114003ea16207ac7850d8da8add1 \
    https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js /app/web/vendor/maplibre-gl.js
ADD --chmod=644 --checksum=sha256:576b085fdd9487a65a19215328c1e086c07ce5bf6da09b666b3806d3d008dae9 \
    https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css /app/web/vendor/maplibre-gl.css
ADD --chmod=644 --checksum=sha256:08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7 \
    https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.min.js /app/web/vendor/three.module.min.js
ADD --chmod=644 --checksum=sha256:63a04a823e6e589a6dd4384e2c175a8e6f70c313f6f58aa88fd92e436ced55fe     https://cdn.jsdelivr.net/npm/agora-rtc-sdk-ng@4.24.9/AgoraRTC_N-production.js /app/web/vendor/AgoraRTC_N-production.js
ADD --chmod=644 --checksum=sha256:cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30     https://www.apache.org/licenses/LICENSE-2.0.txt /app/web/vendor/LICENSE-agora-rtc-sdk-ng.txt

COPY app /app/app
COPY catalog /app/catalog
COPY web /app/web
COPY docs/user /app/docs/user
COPY CHANGELOG.md /app/CHANGELOG.md
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh
RUN chmod -R a+rX /app                          # readable by PUID whatever the build machine's file modes were

# The git commit this image was built from (CI and scripts/deploy.sh pass it); shown next to the version.
ARG PANEL_BUILD=
ENV PANEL_BUILD=$PANEL_BUILD

# The panel runs as PUID:PGID (default 1000:1000), not root; the entrypoint hands /data to that user once.
ENV DATA_DIR=/data PORT=8425 PUID=1000 PGID=1000
EXPOSE 8425
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8425\")}/health', timeout=5).status == 200 else 1)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
