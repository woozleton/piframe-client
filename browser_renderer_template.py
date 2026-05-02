#!/usr/bin/env python3
"""Browser renderer HTML template for the PiFrame Chromium kiosk."""

from __future__ import annotations

import json


def render_browser_html(
    *,
    rotation_degrees: int,
    show_hud: bool,
    transition_duration_ms: int,
    state_file_uri: str,
    nas_root: str,
    poll_ms: int,
    event_endpoint: str = "",
    butterchurn_lib_uri: str = "",
    butterchurn_presets_uri: str = "",
    visualizer_presets: list | None = None,
) -> str:
    """Render the self-contained Chromium kiosk page."""
    show_hud_css = "block" if show_hud else "none"
    show_hud_js = "true" if show_hud else "false"
    reset_delay_ms = max(transition_duration_ms + 50, 150)
    hidden_cursor = (
        'url("data:image/png;base64,'
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9s0qK1wAAAAASUVORK5CYII=") 0 0, none'
    )
    # Butterchurn vendor script tags. Empty when the bundle isn't
    # deployed; the visualizer JS gracefully no-ops in that case.
    butterchurn_scripts = ""
    if butterchurn_lib_uri and butterchurn_presets_uri:
        butterchurn_scripts = (
            f'<script src="{butterchurn_lib_uri}"></script>\n'
            f'  <script src="{butterchurn_presets_uri}"></script>'
        )
    visualizer_presets_json = json.dumps(list(visualizer_presets or []))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PiFrame Browser Renderer</title>
  <style>
    :root {{
      --bg: #050505;
      --fg: #f3f0e8;
      --muted: rgba(243, 240, 232, 0.72);
      --accent: #e0b35b;
      --hidden-cursor: {hidden_cursor};
    }}
    * {{ box-sizing: border-box; }}
    * {{ cursor: var(--hidden-cursor) !important; }}
    html, body {{
      margin: 0;
      width: 100%;
      height: 100%;
      background: var(--bg);
      overflow: hidden;
      color: var(--fg);
      cursor: var(--hidden-cursor);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    body {{
      display: grid;
      place-items: center;
    }}
    .frame {{
      position: fixed;
      inset: 0;
      overflow: hidden;
      background: #050505;
    }}
    .banner {{
      position: fixed;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%) rotate({rotation_degrees}deg)
        translateY(calc(-50vw + 90px));
      transform-origin: center center;
      z-index: 40;
      min-width: 320px;
      max-width: min(70vh, 1100px);
      padding: 18px 28px;
      border-radius: 18px;
      background: rgba(20, 16, 10, 0.82);
      color: #fff3d6;
      border: 1px solid rgba(224, 179, 91, 0.35);
      backdrop-filter: blur(8px);
      font-size: 24px;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-align: center;
      opacity: 0;
      pointer-events: none;
      transition: opacity 180ms ease-in-out;
    }}
    .banner.visible {{
      opacity: 1;
    }}
    .banner.error {{
      background: rgba(48, 10, 10, 0.84);
      color: #ffe2e2;
      border-color: rgba(255, 130, 130, 0.4);
    }}
    .osd {{
      position: fixed;
      left: 50%;
      top: 50%;
      z-index: 45;
      min-width: 250px;
      max-width: min(56vh, 760px);
      padding: 20px 24px 22px;
      border-radius: 24px;
      background:
        linear-gradient(180deg, rgba(26, 24, 20, 0.84), rgba(12, 12, 12, 0.78));
      border: 1px solid rgba(255, 255, 255, 0.09);
      box-shadow:
        0 18px 42px rgba(0, 0, 0, 0.34),
        inset 0 1px 0 rgba(255, 255, 255, 0.06);
      backdrop-filter: blur(14px);
      transform: translate(-50%, -50%) rotate({rotation_degrees}deg)
        translateY(calc(50vw - 150px)) scale(0.92);
      transform-origin: center center;
      opacity: 0;
      pointer-events: none;
      transition:
        opacity 180ms ease-out,
        transform 220ms cubic-bezier(0.2, 0.9, 0.2, 1);
    }}
    .osd.visible {{
      opacity: 1;
      transform: translate(-50%, -50%) rotate({rotation_degrees}deg)
        translateY(calc(50vw - 150px)) scale(1);
    }}
    .osd-head {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 14px;
      flex-wrap: nowrap;
      white-space: nowrap;
    }}
    .osd-icon {{
      width: 34px;
      height: 34px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: var(--fg);
      opacity: 0.96;
    }}
    .osd-icon svg {{
      width: 100%;
      height: 100%;
      display: block;
      fill: currentColor;
    }}
    .osd-value {{
      font-size: 22px;
      font-weight: 600;
      letter-spacing: 0.01em;
      color: var(--fg);
    }}
    .osd-label {{
      font-size: 14px;
      font-weight: 500;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .osd-bar {{
      margin-top: 16px;
      height: 12px;
      width: 100%;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.11);
      box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.35);
    }}
    .osd-bar-fill {{
      height: 100%;
      width: 0%;
      border-radius: 999px;
      background: linear-gradient(90deg, #c68f30 0%, #e0b35b 55%, #f5d58e 100%);
      box-shadow: 0 0 18px rgba(224, 179, 91, 0.28);
      transition: width 140ms ease-out;
    }}
    .osd.error {{
      background:
        linear-gradient(180deg, rgba(48, 18, 14, 0.88), rgba(28, 10, 8, 0.84));
      border-color: rgba(255, 140, 120, 0.38);
      box-shadow:
        0 18px 42px rgba(0, 0, 0, 0.4),
        inset 0 1px 0 rgba(255, 200, 190, 0.08);
    }}
    .osd.error .osd-icon {{
      color: #ffb39c;
      width: 38px;
      height: 38px;
    }}
    .osd.error .osd-head {{
      white-space: normal;
      flex-wrap: nowrap;
      align-items: center;
      text-align: left;
      gap: 16px;
    }}
    .osd.error .osd-value {{
      font-size: 19px;
      font-weight: 500;
      letter-spacing: 0.01em;
      line-height: 1.35;
      white-space: normal;
      text-align: left;
      max-width: 44vh;
      color: #fff1ec;
    }}
    .stage {{
      position: absolute;
      inset: 0;
      opacity: 0;
      transition: opacity {transition_duration_ms}ms ease-in-out;
      pointer-events: none;
    }}
    .stage.active {{
      opacity: 1;
    }}
    .backdrop {{
      position: absolute;
      inset: -8%;
      opacity: 0;
      transform: scale(1.06);
      transition: opacity 180ms ease-in-out;
      filter: blur(34px) brightness(0.62) saturate(1.08);
      background: transparent;
    }}
    .backdrop.ready {{
      opacity: 1;
    }}
    .media {{
      position: absolute;
      left: 50%;
      top: 50%;
      width: auto;
      height: auto;
      object-fit: contain;
      transform: translate(-50%, -50%) rotate({rotation_degrees}deg);
      transform-origin: center center;
      filter: drop-shadow(0 0 24px rgba(0,0,0,0.45));
      opacity: 0;
      transition: opacity 180ms ease-in-out;
      background: transparent;
    }}
    .media.ready {{
      opacity: 1;
    }}
    .video {{
      display: none;
    }}
    .html-frame {{
      /* Sized to the rotated display: width = viewport height, height =
         viewport width, so after the rotate() the iframe fills the screen
         exactly. The page inside lays out for this rotated viewport
         natively - no rotation awareness needed in idle.html itself. */
      position: absolute;
      left: 50%;
      top: 50%;
      width: 100vh;
      height: 100vw;
      border: 0;
      background: transparent;
      transform: translate(-50%, -50%) rotate({rotation_degrees}deg);
      transform-origin: center center;
      display: none;
      opacity: 0;
      transition: opacity 180ms ease-in-out;
    }}
    .html-frame.ready {{
      opacity: 1;
    }}
    .hud {{
      display: {show_hud_css};
      position: fixed;
      left: 20px;
      bottom: 18px;
      padding: 10px 14px;
      background: rgba(0, 0, 0, 0.42);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      backdrop-filter: blur(8px);
      letter-spacing: 0.03em;
    }}
    .hud strong {{
      color: var(--accent);
      display: block;
      font-size: 14px;
      margin-bottom: 3px;
    }}
    .hud span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    /* Audio visualizer overlay.
       Fullscreen layer above the stages, only shown while the active
       item carries is_audio=true. Rotates to match the device's
       physical mount orientation (270deg for portrait Pi clients).
       Composition:
         .audio-vis           rotated wrapper, dimensions are the
                              POST-rotation viewport (height=1080 if
                              physical=1920 with 90/270 rotation).
         .audio-vis__art      blurred waveform PNG as background.
                              Scales subtly via Ken Burns animation.
         canvas               FFT bars + bass pulse drawn here.
         .audio-vis__vignette top/bottom soft fade so bars never hit
                              a hard edge. */
    .audio-vis {{
      position: fixed;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%) rotate({rotation_degrees}deg);
      transform-origin: center center;
      pointer-events: none;
      opacity: 0;
      transition: opacity .35s ease;
      z-index: 4;
      overflow: hidden;
      /* Width / height land via JS once we know the post-rotation
         dimensions; default fallback fills viewport in landscape. */
      width: 100vw;
      height: 100vh;
      background: #060410;
    }}
    .audio-vis.is-active {{
      opacity: 1;
    }}
    .audio-vis__art {{
      position: absolute;
      inset: -8%;
      width: 116%;
      height: 116%;
      background-position: center;
      background-repeat: no-repeat;
      background-size: cover;
      filter: blur(36px) saturate(135%) brightness(0.7);
      opacity: 0;
      transition: opacity .8s ease, background-image 0s;
      transform: scale(1);
      animation: audio-vis-pan 24s ease-in-out infinite alternate;
    }}
    .audio-vis__art.is-loaded {{
      opacity: 1;
    }}
    @keyframes audio-vis-pan {{
      from {{ transform: scale(1.0); }}
      to   {{ transform: scale(1.12); }}
    }}
    .audio-vis canvas {{
      position: absolute;
      inset: 0;
      display: block;
      width: 100%;
      height: 100%;
    }}
    .audio-vis__vignette {{
      display: none;
    }}
    /* Track-name OSD. Shown for ~5s when a new audio item starts.
       Soft glassy pill, system serif/sans for a polished look (NOT
       monospace - the operator wanted something that doesn't read
       as a debug overlay). Letter-spacing tight, generous padding. */
    .audio-vis__track-name {{
      position: absolute;
      left: 50%;
      bottom: 8%;
      transform: translate(-50%, 12px);
      max-width: min(82%, 800px);
      padding: 14px 26px;
      background: rgba(10, 8, 18, 0.62);
      -webkit-backdrop-filter: blur(14px) saturate(140%);
      backdrop-filter: blur(14px) saturate(140%);
      color: rgba(255, 255, 255, 0.96);
      font-family:
        ui-rounded, "SF Pro Rounded", "SF Pro Display",
        -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      font-size: 22px;
      font-weight: 500;
      letter-spacing: 0.005em;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 14px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
      pointer-events: none;
      opacity: 0;
      transition: opacity .55s ease, transform .55s ease;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-align: center;
    }}
    .audio-vis__track-name.is-visible {{
      opacity: 1;
      transform: translate(-50%, 0);
    }}
  </style>
</head>
<body>
  <div class="frame">
    <div id="stage0" class="stage active">
      <img id="bgImage0" class="backdrop" alt="">
      <img id="image0" class="media image" alt="">
      <video id="video0" class="media video" muted playsinline preload="auto"></video>
      <iframe id="html0" class="html-frame" sandbox="allow-scripts allow-same-origin"></iframe>
    </div>
    <div id="stage1" class="stage">
      <img id="bgImage1" class="backdrop" alt="">
      <img id="image1" class="media image" alt="">
      <video id="video1" class="media video" muted playsinline preload="auto"></video>
      <iframe id="html1" class="html-frame" sandbox="allow-scripts allow-same-origin"></iframe>
    </div>
    <!-- Audio companion: hidden <audio> element that plays a
         background queue in parallel with whatever visual is on
         stage. Polled out of state.companion. Positioned offscreen
         instead of display:none because some Chromium builds skip
         loading or autoplay on display:none media elements. -->
    <audio id="companionAudio" preload="auto"
           style="position:absolute; left:-9999px; width:1px; height:1px;"></audio>
    <!-- Audio visualizer overlay. Hidden by default; the renderer
         flips .is-active when the current item is_audio=true. The
         layer is itself rotated to match the device mount; canvas
         + art live inside the rotated coordinate space so the
         visualization reads upright on a portrait-mounted screen. -->
    <div id="audioVis" class="audio-vis" aria-hidden="true">
      <div id="audioVisArt" class="audio-vis__art"></div>
      <canvas id="audioVisCanvas"></canvas>
      <div class="audio-vis__vignette"></div>
      <div id="audioVisTrackName" class="audio-vis__track-name" aria-hidden="true"></div>
    </div>
  </div>
  <div id="banner" class="banner"></div>
  <div id="osd" class="osd">
    <div class="osd-head">
      <div id="osdIcon" class="osd-icon"></div>
      <div id="osdValue" class="osd-value"></div>
      <div id="osdLabel" class="osd-label"></div>
    </div>
    <div id="osdBar" class="osd-bar">
      <div id="osdBarFill" class="osd-bar-fill"></div>
    </div>
  </div>
  <div class="hud">
    <strong id="playlistName">Starting…</strong>
    <span id="fileName"></span>
    <span id="timing"></span>
  </div>
  <!-- Vendor scripts: Butterchurn (MilkDrop port) + its preset
       bundle. Loaded by file:// from /tmp/ alongside the HTML so
       same-origin same-disk - no CORS, no network. Empty when the
       vendor bundle isn't deployed; audioVis falls back to disabled. -->
  {butterchurn_scripts}
  <script>
    const stateUrl = {json.dumps(state_file_uri)};
    const eventEndpoint = {json.dumps(event_endpoint)};
    const hiddenCursor = getComputedStyle(document.documentElement)
      .getPropertyValue('--hidden-cursor')
      .trim() || 'none';
    const stages = [
      {{
        root: document.getElementById("stage0"),
        bgImage: document.getElementById("bgImage0"),
        image: document.getElementById("image0"),
        video: document.getElementById("video0"),
        html: document.getElementById("html0"),
      }},
      {{
        root: document.getElementById("stage1"),
        bgImage: document.getElementById("bgImage1"),
        image: document.getElementById("image1"),
        video: document.getElementById("video1"),
        html: document.getElementById("html1"),
      }},
    ];
    const playlistEl = document.getElementById("playlistName");
    const fileEl = document.getElementById("fileName");
    const timingEl = document.getElementById("timing");
    const bannerEl = document.getElementById("banner");
    const osdEl = document.getElementById("osd");
    const osdIconEl = document.getElementById("osdIcon");
    const osdValueEl = document.getElementById("osdValue");
    const osdLabelEl = document.getElementById("osdLabel");
    const osdBarEl = document.getElementById("osdBar");
    const osdBarFillEl = document.getElementById("osdBarFill");
    let currentSignature = "";
    let currentControlToken = -1;
    let activeState = null;
    let activeIndex = 0;
    let intervalHandle = null;
    let activeStageIndex = 0;
    let pendingAdvanceToken = 0;
    let osdTimer = null;
    let lastVolume = null;
    let lastMuted = null;
    let lastReportedSlideIndex = -1;
    let lastReportedPaused = null;
    // Sources we've already determined to be unplayable on this device.
    // Cleared when a state update brings a different items list, so a
    // legitimate retry (new content) gets a fresh attempt.
    const failedSrcs = new Set();
    const failedSrcReasons = new Map();
    function notifySlideChange(idx) {{
      if (!eventEndpoint || idx === lastReportedSlideIndex) return;
      lastReportedSlideIndex = idx;
      try {{
        fetch(eventEndpoint, {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{type: "slideshow_index", index: idx}}),
          keepalive: true,
        }}).catch(() => {{}});
      }} catch (_) {{ /* ignore */ }}
    }}
    function notifyPauseState(paused) {{
      const flag = !!paused;
      if (!eventEndpoint || flag === lastReportedPaused) return;
      lastReportedPaused = flag;
      try {{
        fetch(eventEndpoint, {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{type: "pause_state", paused: flag}}),
          keepalive: true,
        }}).catch(() => {{}});
      }} catch (_) {{ /* ignore */ }}
    }}

    function showBanner(message, level = "warning") {{
      if (!message) {{
        bannerEl.textContent = "";
        bannerEl.classList.remove("visible", "error");
        return;
      }}
      bannerEl.textContent = message;
      bannerEl.classList.toggle("error", level === "error");
      bannerEl.classList.add("visible");
    }}

    function itemErrorMessage(item) {{
      if (!item || !item.src) {{
        return "Content unavailable";
      }}
      if (item.src.startsWith("file://")) {{
        if (item.src.includes("{nas_root}/")) {{
          return "NAS unavailable";
        }}
        return "Media file missing";
      }}
      if (item.src.startsWith("http://") || item.src.startsWith("https://")) {{
        return "Website unavailable";
      }}
      return "Content unavailable";
    }}

    function describeVideoError(video, item) {{
      const err = video && video.error;
      const code = err ? err.code : 0;
      // MEDIA_ERR_SRC_NOT_SUPPORTED — codec/container the browser flat-out
      // refuses (e.g. HEVC on stock Chromium).
      if (code === 4) {{
        return "This video format isn't supported on this display";
      }}
      // MEDIA_ERR_DECODE — accepted the source but the decoder gave up
      // partway (e.g. 4K H.264 saturating CPU on the Pi 5).
      if (code === 3) {{
        return "This video can't be decoded on this display";
      }}
      // MEDIA_ERR_NETWORK — file became unreachable mid-load.
      if (code === 2) {{
        return itemErrorMessage(item);
      }}
      // MEDIA_ERR_ABORTED or no error object (watchdog path) — generic.
      return "This video can't be played on this display";
    }}

    function hideOsd() {{
      if (osdTimer) {{
        window.clearTimeout(osdTimer);
        osdTimer = null;
      }}
      osdEl.classList.remove("visible");
    }}

    function osdIconSvg(kind) {{
      if (kind === "pause") {{
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1.5"></rect><rect x="14" y="4" width="4" height="16" rx="1.5"></rect></svg>';
      }}
      if (kind === "mute") {{
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 4.5a1 1 0 0 1 1.7.7v13.6a1 1 0 0 1-1.7.7L9.7 15H6a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2h3.7l4.8-4.5ZM18.2 8.4l1.4 1.4-2.1 2.2 2.1 2.2-1.4 1.4-2.2-2.1-2.2 2.1-1.4-1.4 2.1-2.2-2.1-2.2 1.4-1.4 2.2 2.1 2.2-2.1Z"></path></svg>';
      }}
      if (kind === "error") {{
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.4c.62 0 1.2.34 1.5.88l8.7 15.4c.6 1.07-.16 2.4-1.5 2.4H3.3c-1.34 0-2.1-1.33-1.5-2.4L10.5 3.28c.3-.54.88-.88 1.5-.88Zm0 5.6a1.05 1.05 0 0 0-1.05 1.05v5.1a1.05 1.05 0 1 0 2.1 0v-5.1A1.05 1.05 0 0 0 12 8Zm0 9.1a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Z"></path></svg>';
      }}
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 4.5a1 1 0 0 1 1.7.7v13.6a1 1 0 0 1-1.7.7L9.7 15H6a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2h3.7l4.8-4.5Zm3.9 1.8a1 1 0 0 1 1.4 0 8 8 0 0 1 0 11.4 1 1 0 1 1-1.4-1.4 6 6 0 0 0 0-8.6 1 1 0 0 1 0-1.4Zm-2.8 2.8a1 1 0 0 1 1.4 0 4 4 0 0 1 0 5.7 1 1 0 0 1-1.4-1.4 2 2 0 0 0 0-2.9 1 1 0 0 1 0-1.4Z"></path></svg>';
    }}

    function showOsd(kind, value = "", label = "", percent = null, durationMs = 1000) {{
      // An active error OSD is sticky — routine pause / volume / mute
      // toasts must not displace it, otherwise the user loses the
      // explanation for why playback stopped.
      if (osdEl.classList.contains("error") && kind !== "error") {{
        return;
      }}
      if (osdTimer) {{
        window.clearTimeout(osdTimer);
        osdTimer = null;
      }}
      osdIconEl.innerHTML = osdIconSvg(kind);
      osdValueEl.textContent = value || "";
      osdLabelEl.textContent = label || "";
      // Hide empty slots so the flex gap doesn't push the icon off-center
      // when only the icon is shown (pause / play overlays).
      osdValueEl.style.display = value ? "" : "none";
      osdLabelEl.style.display = label ? "" : "none";
      osdEl.classList.toggle("error", kind === "error");
      const hasBar = typeof percent === "number";
      osdBarEl.style.display = hasBar ? "block" : "none";
      if (hasBar) {{
        const bounded = Math.max(0, Math.min(100, percent));
        osdBarFillEl.style.width = `${{bounded}}%`;
      }}
      osdEl.classList.add("visible");
      if (durationMs > 0) {{
        osdTimer = window.setTimeout(() => {{
          osdEl.classList.remove("visible");
          osdTimer = null;
        }}, durationMs);
      }}
    }}

    function hideCursor() {{
      document.documentElement.style.cursor = hiddenCursor;
      document.body.style.cursor = hiddenCursor;
      bannerEl.style.cursor = hiddenCursor;
      osdEl.style.cursor = hiddenCursor;
      osdEl.querySelector('.osd-head').style.cursor = hiddenCursor;
      osdIconEl.style.cursor = hiddenCursor;
      osdValueEl.style.cursor = hiddenCursor;
      osdLabelEl.style.cursor = hiddenCursor;
      osdBarEl.style.cursor = hiddenCursor;
      osdBarFillEl.style.cursor = hiddenCursor;
      playlistEl.style.cursor = hiddenCursor;
      fileEl.style.cursor = hiddenCursor;
      timingEl.style.cursor = hiddenCursor;
      for (const stage of stages) {{
        stage.root.style.cursor = hiddenCursor;
        stage.bgImage.style.cursor = hiddenCursor;
        stage.image.style.cursor = hiddenCursor;
        stage.video.style.cursor = hiddenCursor;
      }}
    }}

    function fitMedia(element, naturalWidth, naturalHeight, fitMode = "contain") {{
      const viewportWidth = window.innerWidth || 1;
      const viewportHeight = window.innerHeight || 1;
      const usableWidth = viewportHeight;
      const usableHeight = viewportWidth;
      const fitFn = fitMode === "cover" ? Math.max : Math.min;
      const scale = fitFn(usableWidth / naturalWidth, usableHeight / naturalHeight);
      element.style.width = `${{Math.max(1, naturalWidth * scale)}}px`;
      element.style.height = `${{Math.max(1, naturalHeight * scale)}}px`;
    }}

    function setHud(item, state, perItemSeconds) {{
      if ({show_hud_js} !== true) {{
        return;
      }}
      playlistEl.textContent = state.playlist_name || "PiFrame";
      fileEl.textContent = item ? item.label : "";
      timingEl.textContent = `mode ${{state.mode}} | per-item ${{perItemSeconds.toFixed(3)}}s | rotate {rotation_degrees}deg`;
    }}

    function preloadNextImage() {{
      if (!activeState || !activeState.items || activeState.items.length < 2) {{
        return;
      }}
      const itemCount = activeState.items.length;
      let nextIndex = activeIndex + 1;
      if (activeState.repeat) {{
        nextIndex = (nextIndex + itemCount) % itemCount;
      }} else if (nextIndex >= itemCount) {{
        return;
      }}
      const nextItem = activeState.items[nextIndex];
      if (!nextItem || nextItem.kind !== "image") {{
        return;
      }}
      const preload = new Image();
      preload.src = nextItem.src;
    }}

    function stopTimers() {{
      window.clearInterval(intervalHandle);
      intervalHandle = null;
      pendingAdvanceToken += 1;
    }}

    function resetStage(stage) {{
      if (stage.firstFrameWatchdog) {{
        window.clearTimeout(stage.firstFrameWatchdog);
        stage.firstFrameWatchdog = null;
      }}
      if (stage.progressWatchdog) {{
        window.clearInterval(stage.progressWatchdog);
        stage.progressWatchdog = null;
      }}
      if (stage.errorTimer) {{
        window.clearTimeout(stage.errorTimer);
        stage.errorTimer = null;
      }}
      stage.bgImage.onload = null;
      stage.image.onload = null;
      stage.video.onloadedmetadata = null;
      stage.video.onloadeddata = null;
      stage.video.onended = null;
      stage.video.onerror = null;
      stage.bgImage.classList.remove("ready");
      stage.image.classList.remove("ready");
      stage.video.classList.remove("ready");
      stage.html.classList.remove("ready");
      stage.bgImage.style.display = "none";
      stage.image.style.display = "none";
      stage.video.style.display = "none";
      stage.html.style.display = "none";
      stage.video.pause();
      stage.video.removeAttribute("src");
      stage.video.load();
      stage.html.onload = null;
      stage.html.onerror = null;
      // about:blank releases the previous document; setting src="" leaves
      // the prior page visible during the cross-fade.
      stage.html.src = "about:blank";
    }}

    function getActiveStage() {{
      return stages[activeStageIndex];
    }}

    function getInactiveStage() {{
      return stages[(activeStageIndex + 1) % stages.length];
    }}

    function activateStage(stage) {{
      const nextIndex = stages.indexOf(stage);
      if (nextIndex === -1) {{
        return;
      }}
      stages[activeStageIndex].root.classList.remove("active");
      stage.root.classList.add("active");
      activeStageIndex = nextIndex;
    }}

    function showIdle(item) {{
      // Preserve a visible error message across the fall-back to idle so
      // the user still sees why playback stopped.
      if (!osdEl.classList.contains("error")) {{
        hideOsd();
      }}
      if (!item) {{
        for (const stage of stages) {{
          resetStage(stage);
          stage.root.classList.remove("active");
        }}
        return;
      }}
      renderItem(item, {{
        playlist_name: "Idle",
        mode: "idle",
        muted: activeState ? activeState.muted : true,
        volume: activeState ? activeState.volume : 0,
        loop: true,
      }}, 0);
    }}

    function scheduleImageAdvance(perItemSeconds) {{
      window.clearInterval(intervalHandle);
      intervalHandle = null;
      // A 1-item repeating playlist has nothing to advance to - the
      // image is already painted. Re-arming the interval would just
      // re-render the same item every tick and the cross-fade between
      // stages reads as a barely-visible flicker.
      if (
        activeState &&
        Array.isArray(activeState.items) &&
        activeState.items.length === 1 &&
        activeState.repeat
      ) {{
        return;
      }}
      const token = pendingAdvanceToken;
      intervalHandle = window.setInterval(() => {{
        if (token !== pendingAdvanceToken) {{
          return;
        }}
        advancePlaylist(1);
      }}, perItemSeconds * 1000);
    }}

    function prepareStage(stage, item, state, perItemSeconds) {{
      if (stage.resetHandle) {{
        window.clearTimeout(stage.resetHandle);
        stage.resetHandle = null;
      }}
      resetStage(stage);
      if (item.kind === "html") {{
        stage.html.style.display = "block";
        stage.html.onload = () => {{
          stage.html.classList.add("ready");
          if (state.mode !== "idle" && osdEl.classList.contains("error")) {{
            hideOsd();
          }}
        }};
        stage.html.onerror = () => {{
          showBanner(itemErrorMessage(item), "error");
        }};
        stage.html.src = item.src;
      }} else if (item.kind === "video") {{
        const isSingleRepeatingPlaylist =
          state.mode === "playlist" &&
          !!state.repeat &&
          Array.isArray(state.items) &&
          state.items.length === 1;
        const fillMode = state.video_fill_mode || "contain";
        stage.video.style.display = "block";
        // video_mute_override is set by set_companion when the
        // companion is active AND mute_visual is on. Treat it as
        // "force-mute" on top of the user's state.muted toggle so
        // the override path doesn't have to re-fire after every
        // video transition.
        const forceMute = !!state.video_mute_override;
        const userMuted = !!state.muted;
        stage.video.dataset.desiredMuted = userMuted.toString();
        stage.video.dataset.desiredVolume = Math.max(0, Math.min(1, (state.volume || 0) / 100)).toString();
        stage.video.muted = userMuted || forceMute;
        stage.video.volume = forceMute ? 0 : Math.max(0, Math.min(1, (state.volume || 0) / 100));
        stage.video.loop = state.mode === "single" ? !!state.loop : isSingleRepeatingPlaylist;
        stage.video.preload = "auto";
        const failVideo = (reason) => {{
          if (stage.errorTimer) {{
            // already failing — don't double-trigger
            return;
          }}
          if (stage.firstFrameWatchdog) {{
            window.clearTimeout(stage.firstFrameWatchdog);
            stage.firstFrameWatchdog = null;
          }}
          if (stage.progressWatchdog) {{
            window.clearInterval(stage.progressWatchdog);
            stage.progressWatchdog = null;
          }}
          stage.video.pause();
          if (item && item.src) {{
            failedSrcs.add(item.src);
            failedSrcReasons.set(item.src, reason);
          }}
          // durationMs=0 → persists; cleared only by an explicit user
          // action or a state update that brings new content.
          showOsd("error", reason, "", null, 0);
          stage.errorTimer = window.setTimeout(() => {{
            stage.errorTimer = null;
            // Looping a fundamentally unplayable single video would just
            // retry forever, so always fall back to idle in single mode.
            if (state.mode === "single") {{
              showIdle(state.idle_item);
            }} else {{
              advancePlaylist(1);
            }}
          }}, 3500);
        }};
        stage.video.onloadedmetadata = () => {{
          // Metadata is necessary for fit calculations, but it isn't proof
          // that the codec actually decodes — Chromium fires this for
          // HEVC even though no frame will ever appear. Don't add .ready
          // here; wait for loadeddata.
          const foregroundMode = fillMode === "cover" ? "cover" : "contain";
          fitMedia(stage.video, stage.video.videoWidth || 1, stage.video.videoHeight || 1, foregroundMode);
        }};
        stage.video.onloadeddata = () => {{
          // First frame was actually decoded — the codec works.
          if (stage.firstFrameWatchdog) {{
            window.clearTimeout(stage.firstFrameWatchdog);
            stage.firstFrameWatchdog = null;
          }}
          stage.video.classList.add("ready");
          if (state.mode !== "idle" && osdEl.classList.contains("error")) {{
            hideOsd();
          }}
          // Progress watchdog — catches videos that decode their first
          // frame but then can't keep up (e.g. 4K H.264 saturating CPU
          // on the Pi 5). Sample every 1.5s; two consecutive stalls →
          // give up (~3s).
          let lastSampledTime = stage.video.currentTime;
          let stalledChecks = 0;
          stage.progressWatchdog = window.setInterval(() => {{
            const v = stage.video;
            if (v.paused || v.ended) {{
              lastSampledTime = v.currentTime;
              stalledChecks = 0;
              return;
            }}
            if (v.currentTime > lastSampledTime + 0.05) {{
              lastSampledTime = v.currentTime;
              stalledChecks = 0;
              return;
            }}
            stalledChecks += 1;
            if (stalledChecks >= 2) {{
              failVideo(describeVideoError(v, item));
            }}
          }}, 1500);
        }};
        stage.video.onerror = () => {{
          failVideo(describeVideoError(stage.video, item));
        }};
        stage.video.onended = () => {{
          if (state.mode === "single" && !state.loop) {{
            showIdle(state.idle_item);
            return;
          }}
          if (isSingleRepeatingPlaylist) {{
            return;
          }}
          advancePlaylist(1);
        }};
        // First-frame watchdog — if no frame is decoded within 7s
        // (Chromium silently rejecting the codec, decoder hung, etc.),
        // treat it as unsupported. readyState < HAVE_CURRENT_DATA (2)
        // means nothing has actually been decoded yet.
        stage.firstFrameWatchdog = window.setTimeout(() => {{
          stage.firstFrameWatchdog = null;
          if (stage.video.readyState < 2) {{
            failVideo(describeVideoError(stage.video, item));
          }}
        }}, 7000);
        stage.video.src = item.src;
        stage.video.currentTime = 0;
      }} else {{
        stage.image.style.display = "block";
        stage.image.onload = () => {{
          fitMedia(stage.image, stage.image.naturalWidth || 1, stage.image.naturalHeight || 1, "contain");
          stage.image.classList.add("ready");
          if (state.mode !== "idle" && osdEl.classList.contains("error")) {{
            hideOsd();
          }}
        }};
        stage.image.onerror = () => {{
          showBanner(itemErrorMessage(item), "error");
        }};
        stage.image.src = item.src;
      }}
      setHud(item, state, perItemSeconds);
    }}

    /* =================================================================
       Audio visualizer (Butterchurn / MilkDrop port).
       ----------------------------------------------------------------
       Drives a WebGL canvas via Butterchurn, fed by a silent <audio>
       element decoding the same source as the audible <video>. The
       audible audio is still produced by the existing <video>; the
       silent decode path exists purely to feed FFT data into
       Butterchurn's AnalyserNode hookup.
       ----------------------------------------------------------------
       Why parallel decode rather than tapping the <video>:
         createMediaElementSource on a <video> seizes the element's
         audio output - we'd lose audibility. A second silent decoder
         is the canonical workaround. Drift between the two streams
         is <150ms on Pi 5; we resync every 1.5s if it grows.
       ----------------------------------------------------------------
       Rotation: the wrapper is rotated via CSS. Butterchurn paints
       in the wrapper's post-rotation coordinate space, so a portrait
       Pi gets a portrait visualization with no extra math.
       ----------------------------------------------------------------
       Preset cycling: a curated subset of the bundled presets that
       skips the GPU-heaviest. Cycles every 45s + on track change.
       ================================================================= */
    const audioVis = (() => {{
      const root = document.getElementById("audioVis");
      const canvas = document.getElementById("audioVisCanvas");
      const artEl = document.getElementById("audioVisArt");
      const trackNameEl = document.getElementById("audioVisTrackName");
      let trackNameTimer = null;

      // Pretty filename: drop the extension, swap underscores for
      // spaces, trim leading "NN. " or "NN - " track-number prefixes.
      // Falls back to the raw label if anything's wrong.
      function prettyTrackName(label) {{
        if (!label || typeof label !== "string") return "";
        let s = label.replace(/\.[a-z0-9]{{1,5}}$/i, "");  // drop extension
        s = s.replace(/_/g, " ");
        s = s.replace(/^\s*\d{{1,3}}\s*[\.\-_]\s*/, "");   // strip "01. " / "12 - "
        return s.trim() || label;
      }}

      function showTrackName(label) {{
        if (!trackNameEl) return;
        trackNameEl.textContent = prettyTrackName(label);
        trackNameEl.classList.add("is-visible");
        if (trackNameTimer) {{
          window.clearTimeout(trackNameTimer);
        }}
        trackNameTimer = window.setTimeout(() => {{
          if (trackNameEl) trackNameEl.classList.remove("is-visible");
          trackNameTimer = null;
        }}, 5000);
      }}

      function hideTrackName() {{
        if (trackNameTimer) {{
          window.clearTimeout(trackNameTimer);
          trackNameTimer = null;
        }}
        if (trackNameEl) trackNameEl.classList.remove("is-visible");
      }}

      let audioCtx = null;
      let analyserAudio = null;
      let analyserSrc = null;
      let viz = null;
      let presets = null;
      let presetNames = [];
      let presetIdx = 0;
      let presetCycleHandle = null;
      const PRESET_CYCLE_MS = 5000;

      let active = false;
      let videoEl = null;
      let item = null;
      let rafHandle = null;
      // Performance scaffolding. Pi 5 GPU runs simple presets fine
      // at 1:1, but heavy shader presets stall to single digits FPS.
      // Render at a fixed lower resolution and upscale via CSS - cuts
      // shader work proportionally to the area ratio. The canvas
      // CSS size still fills the wrapper so the visual is fullscreen.
      const RENDER_SCALE = 0.6;     // 60% of viewport pixels
      const TARGET_FPS = 60;
      const FRAME_MIN_MS = 1000 / TARGET_FPS;
      let lastFrameTime = 0;
      // FPS sampling - measured over a 4-second window after each
      // preset load and reported to journalctl. No auto-blacklist;
      // the operator picks which presets to keep based on the logs.
      const FPS_SAMPLE_MS = 4000;
      let presetSampleStart = 0;
      let presetFrameCount = 0;

      function libAvailable() {{
        return !!(window.butterchurn && window.butterchurnPresets);
      }}


      function vizDiag(stage, detail) {{
        // Surface visualizer init / runtime status to the parent
        // process so we can diagnose blue-screen failures over the
        // existing browser-event channel.
        const payload = {{
          type: "audio_visualizer_status",
          stage: String(stage || ""),
          detail: detail == null ? "" : String(detail),
          has_butterchurn: !!window.butterchurn,
          has_presets: !!window.butterchurnPresets,
          ua: navigator.userAgent,
        }};
        try {{ console.log("[audioVis]", payload); }} catch (_) {{}}
        if (!eventEndpoint) return;
        try {{
          fetch(eventEndpoint, {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(payload),
            keepalive: true,
          }}).catch(() => {{}});
        }} catch (_) {{}}
      }}

      function paintVizError(message) {{
        // When init fails, paint the failure reason onto the canvas
        // (using its 2D context separately from any GL context that
        // may already be lost). Operators see it on screen instead
        // of a silent blue.
        if (!canvas) return;
        try {{
          const c = canvas.getContext("2d");
          if (!c) return;
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          const rot = (({rotation_degrees} % 360) + 360) % 360;
          const w = (rot === 90 || rot === 270) ? window.innerHeight : window.innerWidth;
          const h = (rot === 90 || rot === 270) ? window.innerWidth : window.innerHeight;
          canvas.width = Math.round(w * dpr);
          canvas.height = Math.round(h * dpr);
          c.setTransform(dpr, 0, 0, dpr, 0, 0);
          c.fillStyle = "#080612";
          c.fillRect(0, 0, w, h);
          c.fillStyle = "rgba(255,255,255,0.85)";
          c.font = "16px system-ui, sans-serif";
          c.textAlign = "center";
          c.textBaseline = "middle";
          c.fillText("Audio visualizer unavailable", w / 2, h / 2 - 14);
          c.fillStyle = "rgba(255,255,255,0.55)";
          c.font = "12px monospace";
          c.fillText(String(message || ""), w / 2, h / 2 + 14);
        }} catch (_) {{}}
      }}

      function pickPresetNames(allMap) {{
        // Curated calm allow-list, hand-matched against the actual
        // butterchurn-presets v2.4.7 bundle (~85 presets). Names
        // chosen on two signals:
        //   - "calm" thematic words in the filename (drift, frosty,
        //     glass, stormy sea, organic, glowsticks, songflower,
        //     reaction diffusion, etc.).
        //   - Author trends: Eo.S. + Amandio C. consistently calm;
        //     Martin's slower titles (excluding "extreme heat"-type
        //     names); Geiss's reaction-diffusion + radial work.
        // Specific titles excluded that match an author prefix but
        // are clearly fast (e.g. martin's "extreme heat", "disco
        // mix", "acid wiring", "chain breaker", "fruit machine").
        // Fall back to all-presets if the bundle has shifted under
        // us so the visualizer never goes silent.
        // Operator's hand-picked favorites injected by the Python
        // template (single source of truth via piframe_client.py
        // AUDIO_VISUALIZER_PRESETS - same list also reported to the
        // orchestrator in the heartbeat). Falls back to all presets
        // if the bundle has shifted under us so the visualizer never
        // goes silent.
        const NAMED_ALLOW = {visualizer_presets_json};
        const all = Object.keys(allMap);
        const curated = all.filter((n) => {{
          const low = n.toLowerCase();
          for (const allow of NAMED_ALLOW) {{
            if (low.includes(allow.toLowerCase())) return true;
          }}
          return false;
        }});
        return curated.length ? curated : all;
      }}

      function ensureViz() {{
        if (viz) return true;
        if (!libAvailable()) {{
          vizDiag("lib_missing", "butterchurn or presets bundle didn't load");
          paintVizError("vendor scripts didn't load");
          return false;
        }}
        // Probe WebGL availability up front so we get a useful error
        // message instead of "createVisualizer threw."
        try {{
          const probe = document.createElement("canvas");
          const gl = probe.getContext("webgl2") || probe.getContext("webgl");
          if (!gl) {{
            vizDiag("webgl_unavailable", "no WebGL context");
            paintVizError("WebGL unavailable on this device");
            return false;
          }}
        }} catch (probeErr) {{
          vizDiag("webgl_probe_threw", probeErr && probeErr.message);
          paintVizError("WebGL probe failed: " + (probeErr && probeErr.message || ""));
          return false;
        }}
        try {{
          const Ctx = window.AudioContext || window.webkitAudioContext;
          if (!Ctx) {{
            vizDiag("audio_ctx_missing", "no AudioContext constructor");
            paintVizError("AudioContext unavailable");
            return false;
          }}
          audioCtx = new Ctx();
          analyserAudio = document.createElement("audio");
          analyserAudio.crossOrigin = "anonymous";
          analyserAudio.muted = true;
          analyserAudio.volume = 0;
          analyserAudio.preload = "auto";
          analyserAudio.style.display = "none";
          document.body.appendChild(analyserAudio);
          analyserSrc = audioCtx.createMediaElementSource(analyserAudio);

          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          const rot = (({rotation_degrees} % 360) + 360) % 360;
          const w = (rot === 90 || rot === 270) ? window.innerHeight : window.innerWidth;
          const h = (rot === 90 || rot === 270) ? window.innerWidth : window.innerHeight;
          root.style.width = w + "px";
          root.style.height = h + "px";
          // Render at RENDER_SCALE * viewport pixels; CSS scales the
          // canvas back up to fill. ~36% as much shader work for
          // RENDER_SCALE=0.6 since pixel work is the bottleneck.
          canvas.width = Math.max(1, Math.round(w * dpr * RENDER_SCALE));
          canvas.height = Math.max(1, Math.round(h * dpr * RENDER_SCALE));

          // The vendored butterchurn UMD wraps its export under
          // `.default` (webpack's namespace marker). We tolerate
          // either shape so a future bundle change doesn't break us.
          const Butterchurn = (window.butterchurn && window.butterchurn.default)
            || window.butterchurn;
          if (!Butterchurn || typeof Butterchurn.createVisualizer !== "function") {{
            vizDiag("api_shape_unexpected",
              "shape=" + (Butterchurn ? Object.keys(Butterchurn).join(",") : "null"));
            paintVizError("Butterchurn API mismatch");
            return false;
          }}
          viz = Butterchurn.createVisualizer(audioCtx, canvas, {{
            width: canvas.width,
            height: canvas.height,
            pixelRatio: dpr,
          }});
          viz.connectAudio(analyserSrc);

          // Presets bundle exports the class directly (CommonJS path)
          // but we accept either shape for symmetry.
          const Presets = (window.butterchurnPresets && window.butterchurnPresets.default)
            || window.butterchurnPresets;
          if (!Presets || typeof Presets.getPresets !== "function") {{
            vizDiag("presets_api_shape_unexpected",
              "shape=" + (Presets ? Object.keys(Presets).join(",") : "null"));
            paintVizError("Butterchurn presets API mismatch");
            return false;
          }}
          presets = Presets.getPresets();
          presetNames = pickPresetNames(presets);
          loadPresetByIdx(Math.floor(Math.random() * presetNames.length));
          vizDiag("ready", "preset_count=" + presetNames.length);
          return true;
        }} catch (err) {{
          viz = null;
          audioCtx = null;
          vizDiag("init_threw", err && (err.stack || err.message) || "(unknown)");
          paintVizError("init failed: " + (err && err.message || "unknown"));
          return false;
        }}
      }}

      function loadPresetByIdx(idx) {{
        if (!viz || !presets || !presetNames.length) return;
        presetIdx = ((idx % presetNames.length) + presetNames.length) % presetNames.length;
        const name = presetNames[presetIdx];
        const preset = presets[name];
        if (preset) {{
          // Shorter blend (0.6s) since the cycle interval is short -
          // a longer dissolve makes consecutive presets all look
          // like the same dissolving mush.
          viz.loadPreset(preset, 0.6);
          // Start sampling AFTER the blend completes so the blend
          // dissolve doesn't get charged against the new preset's
          // FPS budget.
          presetSampleStart = 0;
          window.setTimeout(() => {{
            if (!active) return;
            presetSampleStart = performance.now();
            presetFrameCount = 0;
          }}, 800);
        }}
      }}

      function nextPreset() {{
        loadPresetByIdx(presetIdx + 1);
      }}

      function startAnalyser(src) {{
        if (!analyserAudio) return;
        try {{
          if (audioCtx && audioCtx.state === "suspended") {{
            audioCtx.resume().then(
              () => vizDiag("audio_ctx_resumed", "state=" + audioCtx.state),
              (e) => vizDiag("audio_ctx_resume_failed", e && e.message)
            );
          }}
          if (analyserAudio.src !== src) {{
            analyserAudio.src = src;
            analyserAudio.load();
          }}
          analyserAudio.play().then(
            () => vizDiag("analyser_playing", "src=" + (src || "").slice(-40)),
            (err) => vizDiag("analyser_play_rejected", err && err.message || "(unknown)")
          );
        }} catch (err) {{
          vizDiag("analyser_start_threw", err && err.message);
        }}
      }}

      // Diag heartbeat: while active, every 2s log whether the
      // silent analyser is actually feeding non-zero FFT bins. If
      // every byte is 0 we know reactivity is broken even though
      // butterchurn is happily painting its idle animation.
      function startReactivityHeartbeat() {{
        if (startReactivityHeartbeat.handle) {{
          window.clearInterval(startReactivityHeartbeat.handle);
        }}
        const probe = audioCtx ? audioCtx.createAnalyser() : null;
        if (!probe || !analyserSrc) return;
        probe.fftSize = 256;
        analyserSrc.connect(probe);
        const data = new Uint8Array(probe.frequencyBinCount);
        startReactivityHeartbeat.handle = window.setInterval(() => {{
          if (!active) return;
          probe.getByteFrequencyData(data);
          let max = 0;
          let sum = 0;
          for (let i = 0; i < data.length; i++) {{
            if (data[i] > max) max = data[i];
            sum += data[i];
          }}
          const avg = sum / data.length;
          vizDiag("reactivity_heartbeat",
            "max=" + max + " avg=" + avg.toFixed(1)
            + " analyser_paused=" + (analyserAudio ? analyserAudio.paused : "?")
            + " analyser_t=" + (analyserAudio && Number.isFinite(analyserAudio.currentTime)
              ? analyserAudio.currentTime.toFixed(2) : "?"));
        }}, 2000);
      }}

      function stopAnalyser() {{
        if (analyserAudio) {{
          try {{ analyserAudio.pause(); }} catch (_) {{}}
          try {{ analyserAudio.removeAttribute("src"); analyserAudio.load(); }} catch (_) {{}}
        }}
      }}

      function syncAnalyserToVideo() {{
        if (!analyserAudio || !videoEl) return;
        if (!Number.isFinite(videoEl.currentTime)) return;
        const drift = Math.abs(analyserAudio.currentTime - videoEl.currentTime);
        if (drift > 0.4) {{
          try {{ analyserAudio.currentTime = videoEl.currentTime; }} catch (_) {{}}
        }}
      }}

      function resize() {{
        if (!viz || !root || !canvas) return;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const rot = (({rotation_degrees} % 360) + 360) % 360;
        const w = (rot === 90 || rot === 270) ? window.innerHeight : window.innerWidth;
        const h = (rot === 90 || rot === 270) ? window.innerWidth : window.innerHeight;
        root.style.width = w + "px";
        root.style.height = h + "px";
        canvas.width = Math.max(1, Math.round(w * dpr * RENDER_SCALE));
        canvas.height = Math.max(1, Math.round(h * dpr * RENDER_SCALE));
        viz.setRendererSize(canvas.width, canvas.height);
      }}

      function render(now) {{
        if (!active || !viz) return;
        // FPS cap. rAF can fire >60 times/s on some Chromium builds;
        // skipping draws when we haven't accumulated FRAME_MIN_MS
        // keeps GPU load down and prevents heavy presets from
        // melting the V3D core trying to draw 60fps.
        if (now - lastFrameTime < FRAME_MIN_MS) {{
          rafHandle = window.requestAnimationFrame(render);
          return;
        }}
        lastFrameTime = now;
        viz.render();
        // FPS measurement only - we report a sample to journalctl so
        // the operator can decide which presets to keep / drop. No
        // auto-blacklist; the curated list is small enough that
        // manual evaluation works better than a heuristic.
        if (presetSampleStart > 0) {{
          presetFrameCount++;
          const elapsed = now - presetSampleStart;
          if (elapsed >= FPS_SAMPLE_MS) {{
            const fps = (presetFrameCount * 1000) / elapsed;
            presetSampleStart = 0;
            vizDiag("preset_fps",
              "name=" + (presetNames[presetIdx] || "?") + " fps=" + fps.toFixed(1));
          }}
        }}
        rafHandle = window.requestAnimationFrame(render);
      }}

      // Per-playlist visualizer pick. 'none' suppresses the overlay
      // entirely, 'random' picks a random preset and cycles every
      // PRESET_CYCLE_MS, anything else is treated as a preset name
      // to lock onto with no cycling. applyChoice() is called by the
      // state-poll loop whenever state.visualizer changes mid-stream.
      let currentChoice = "random";

      function applyChoice(choice) {{
        const next = (choice || "random").toString().trim() || "random";
        if (next === currentChoice && active) return;
        currentChoice = next;
        // If we're not currently active there's nothing to act on -
        // start() will pick up currentChoice the next time it runs.
        if (!active || !viz) return;
        rewireForChoice();
      }}

      function rewireForChoice() {{
        // Clear any existing cycle / heartbeat - they get rebuilt
        // below if the new choice wants them.
        if (presetCycleHandle) {{
          window.clearInterval(presetCycleHandle);
          presetCycleHandle = null;
        }}
        if (currentChoice === "none") {{
          // Tear down the overlay so the page reads as "no visual."
          if (root) {{
            root.classList.remove("is-active");
            root.setAttribute("aria-hidden", "true");
          }}
          if (artEl) artEl.classList.remove("is-loaded");
          return;
        }}
        // Active overlay (random or named).
        if (root) {{
          root.classList.add("is-active");
          root.setAttribute("aria-hidden", "false");
        }}
        if (artEl) artEl.classList.add("is-loaded");
        if (currentChoice === "random") {{
          if (presetNames.length) {{
            loadPresetByIdx(Math.floor(Math.random() * presetNames.length));
          }}
          presetCycleHandle = window.setInterval(nextPreset, PRESET_CYCLE_MS);
        }} else {{
          // Named preset: find by case-insensitive substring match
          // against the curated list, fall back to first preset.
          const wanted = currentChoice.toLowerCase();
          let idx = presetNames.findIndex((n) => n.toLowerCase().includes(wanted));
          if (idx < 0) idx = 0;
          loadPresetByIdx(idx);
        }}
      }}

      function start(itemRef, video) {{
        item = itemRef;
        videoEl = video;
        // 'none' = skip ensureViz entirely so we don't even spin up
        // the WebGL context for surfaces that don't want a visual.
        if (currentChoice === "none") {{
          active = true;  // mark active so applyChoice can react if it flips
          if (root) {{
            root.classList.remove("is-active");
            root.setAttribute("aria-hidden", "true");
          }}
          return;
        }}
        if (!ensureViz()) {{
          if (root) {{
            root.classList.add("is-active");
            root.setAttribute("aria-hidden", "false");
          }}
          return;
        }}
        active = true;
        rewireForChoice();
        if (itemRef && itemRef.src) startAnalyser(itemRef.src);
        // Track-name OSD: show the (prettified) filename for ~5s on
        // every new audio item. Re-triggers on each renderItem call,
        // so playlist advance / next press always pulses the name.
        showTrackName(itemRef && itemRef.label);
        if (start.syncTimer) window.clearInterval(start.syncTimer);
        start.syncTimer = window.setInterval(syncAnalyserToVideo, 1500);
        startReactivityHeartbeat();
        if (rafHandle == null) {{
          rafHandle = window.requestAnimationFrame(render);
        }}
      }}

      function stop() {{
        active = false;
        item = null;
        videoEl = null;
        if (root) {{
          root.classList.remove("is-active");
          root.setAttribute("aria-hidden", "true");
        }}
        if (artEl) artEl.classList.remove("is-loaded");
        hideTrackName();
        stopAnalyser();
        if (start.syncTimer) {{
          window.clearInterval(start.syncTimer);
          start.syncTimer = null;
        }}
        if (presetCycleHandle) {{
          window.clearInterval(presetCycleHandle);
          presetCycleHandle = null;
        }}
        if (startReactivityHeartbeat.handle) {{
          window.clearInterval(startReactivityHeartbeat.handle);
          startReactivityHeartbeat.handle = null;
        }}
        if (rafHandle != null) {{
          window.cancelAnimationFrame(rafHandle);
          rafHandle = null;
        }}
      }}

      window.addEventListener("resize", () => {{
        if (active) resize();
      }});

      return {{ start, stop, applyChoice }};
    }})();

    function renderItem(item, state, perItemSeconds) {{
      if (!item) {{
        return;
      }}
      const targetStage = getInactiveStage();
      prepareStage(targetStage, item, state, perItemSeconds);
      activateStage(targetStage);
      // Audio items: paint the visualizer over the (otherwise blank)
      // <video>. Video / image items: kill the visualizer.
      if (item && item.is_audio) {{
        audioVis.start(item, targetStage.video);
      }} else {{
        audioVis.stop();
      }}
      const previousStage = getInactiveStage();
      if (previousStage.resetHandle) {{
        window.clearTimeout(previousStage.resetHandle);
      }}
      previousStage.resetHandle = window.setTimeout(() => {{
        previousStage.resetHandle = null;
        resetStage(previousStage);
      }}, {reset_delay_ms});
      if (item.kind === "video") {{
        stagePlay(targetStage.video);
      }} else if (item.kind === "html") {{
        // HTML idles are static iframes - no interval, no preload.
        stopTimers();
      }} else {{
        scheduleImageAdvance(perItemSeconds);
        preloadNextImage();
      }}
      hideCursor();
    }}

    function stagePlay(video) {{
      window.clearInterval(intervalHandle);
      intervalHandle = null;
      video.muted = video.dataset.desiredMuted === "true";
      video.volume = Number.parseFloat(video.dataset.desiredVolume || "0.75");
      video.play().catch(() => {{}});
    }}

    /* When mid-stream OVR-on is requested, swap the <video> element
       for a freshly-created one that's muted from creation. Replacing
       the element is what actually frees Chromium's OS audio sink -
       removeAttribute('src') + load() leaves the sink attached to
       the existing element. */
    function recreateMutedVideo(stage) {{
      const old = stage.video;
      if (!old) return;
      const src = old.currentSrc || old.src || "";
      const t = Number.isFinite(old.currentTime) ? old.currentTime : 0;
      const wasPlaying = !old.paused;
      const fresh = document.createElement("video");
      fresh.id = old.id;
      fresh.className = old.className;
      fresh.muted = true;
      fresh.volume = 0;
      fresh.playsInline = true;
      fresh.setAttribute("playsinline", "");
      fresh.preload = "auto";
      fresh.loop = old.loop;
      // Carry over the inline style (width / height / object-fit set
      // by fitMedia) so the new element renders at the same size.
      fresh.style.cssText = old.style.cssText;
      fresh.dataset.desiredMuted = "true";
      fresh.dataset.desiredVolume = "0";
      // Drop the old element first so its audio sink is fully
      // released before the new element opens its own muted output.
      try {{ old.pause(); }} catch (_) {{}}
      old.removeAttribute("src");
      try {{ old.load(); }} catch (_) {{}}
      old.parentNode.replaceChild(fresh, old);
      stage.video = fresh;
      if (src) {{
        // Defer the src + play() so the replaceChild lands first.
        setTimeout(() => {{
          try {{
            fresh.src = src;
            fresh.currentTime = t;
            if (wasPlaying) fresh.play().catch(() => {{}});
          }} catch (_) {{ /* best-effort */ }}
        }}, 50);
      }}
    }}

    let lastForceMute = false;
    function applyLiveAudioState(state) {{
      const forceMute = !!state.video_mute_override;
      const userMuted = !!state.muted;
      const userVol = Math.max(0, Math.min(1, (state.volume || 0) / 100));
      const forceMuteJustEnabledMidStream = forceMute && !lastForceMute;
      lastForceMute = forceMute;
      // Companion audio is produced by a separate mpv sidecar
      // process, mixed by PulseAudio/PipeWire at the OS level. The
      // <video> element only needs to be muted when forceMute is on.
      for (const stage of stages) {{
        stage.video.dataset.desiredMuted = userMuted.toString();
        stage.video.dataset.desiredVolume = userVol.toString();
        stage.video.muted = userMuted || forceMute;
        stage.video.volume = forceMute ? 0 : userVol;
      }}
      // Mid-stream OVR-on: setting muted=true (or even reloading the
      // source) doesn't fully tear down Chromium's audio output - the
      // <video> element retains its audio sink and keeps occupying
      // the OS mixer slot, which drowns out mpv's companion stream.
      // The only thing that reliably frees the sink is replacing the
      // element entirely with a fresh <video muted> created from
      // scratch. Brief flash on the video frame at OVR-toggle time;
      // operator is choosing to switch audio sources, so the cost is
      // acceptable.
      if (forceMuteJustEnabledMidStream) {{
        for (const stage of stages) {{
          if (!stage.video || stage.video.style.display !== "block") continue;
          recreateMutedVideo(stage);
        }}
      }}
      const nextVolume = Number(state.volume || 0);
      const nextMuted = !!state.muted;
      if (lastVolume !== null && (nextVolume !== lastVolume || nextMuted !== lastMuted)) {{
        const kind = nextMuted || nextVolume <= 0 ? "mute" : "volume";
        const value = nextMuted || nextVolume <= 0 ? "" : `${{Math.round(nextVolume)}}%`;
        const label = nextMuted || nextVolume <= 0 ? "Muted" : "";
        // 2500ms gives a TV-style "I see the change" window without
        // lingering. Earlier 1000ms was so short rapid +/- presses
        // would let the OSD vanish between bumps.
        showOsd(kind, value, label, nextMuted ? 0 : nextVolume, 2500);
      }}
      lastVolume = nextVolume;
      lastMuted = nextMuted;
    }}

    function advancePlaylist(step) {{
      if (!activeState || !activeState.items || !activeState.items.length) {{
        return;
      }}
      const itemCount = activeState.items.length;
      let nextIndex = activeIndex + step;
      if (activeState.mode === "playlist") {{
        if (activeState.repeat) {{
          nextIndex = (nextIndex + itemCount) % itemCount;
        }} else {{
          if (nextIndex >= itemCount) {{
            showIdle(activeState.idle_item);
            return;
          }}
          if (nextIndex < 0) {{
            nextIndex = 0;
          }}
        }}
      }} else {{
        nextIndex = 0;
      }}
      activeIndex = nextIndex;
      const perItemSeconds = activeState.interval || 5.0;
      renderItem(activeState.items[activeIndex], activeState, perItemSeconds);
      notifySlideChange(activeIndex);
    }}

    function startFromState(state) {{
      // Drop the failed-src cache when the items list itself changes, so
      // a genuinely new piece of content always gets a fresh attempt.
      const newItemSrcs = (state.items || []).map(i => i && i.src).join("|");
      if (newItemSrcs !== startFromState.lastItemSrcs) {{
        failedSrcs.clear();
        failedSrcReasons.clear();
        startFromState.lastItemSrcs = newItemSrcs;
      }}
      activeState = state;
      activeIndex = 0;
      lastReportedSlideIndex = -1;
      // Preserve a sticky error across benign re-renders (banner change,
      // volume change, etc.) — it'll only clear via user action or
      // genuinely new content above.
      const startingOnFailedVideo =
        state.items && state.items.length === 1 &&
        state.items[0].kind === "video" &&
        failedSrcs.has(state.items[0].src);
      if (!osdEl.classList.contains("error")) {{
        hideOsd();
      }}
      stopTimers();
      if (!state.items || !state.items.length) {{
        showIdle(state.idle_item);
        notifyPauseState(false);
        return;
      }}
      showBanner(state.banner ? state.banner.message : "", state.banner ? state.banner.level : "warning");
      if (startingOnFailedVideo) {{
        // Re-show the cached error message so it survives even if some
        // intervening OSD cleared it (defensive — sticky guard should
        // already prevent that).
        const reason = failedSrcReasons.get(state.items[0].src)
          || "This video can't be played on this display";
        showOsd("error", reason, "", null, 0);
        showIdle(state.idle_item);
        notifyPauseState(false);
        return;
      }}
      const perItemSeconds = state.interval || 5.0;
      renderItem(state.items[activeIndex], state, perItemSeconds);
      notifySlideChange(activeIndex);
      notifyPauseState(false);
    }}
    startFromState.lastItemSrcs = null;

    function applyControl(control) {{
      if (!control) {{
        return;
      }}
      if (control.action === "next") {{
        advancePlaylist(1);
      }} else if (control.action === "previous") {{
        advancePlaylist(-1);
      }} else if (control.action === "pause") {{
        const activeStage = getActiveStage();
        if (activeStage.video.style.display === "block") {{
          if (activeStage.video.paused) {{
            activeStage.video.play().catch(() => {{}});
            hideOsd();
            notifyPauseState(false);
          }} else {{
            activeStage.video.pause();
            showOsd("pause", "", "", null, 1000);
            notifyPauseState(true);
          }}
        }} else {{
          if (intervalHandle) {{
            stopTimers();
            showOsd("pause", "", "", null, 1000);
            notifyPauseState(true);
          }} else {{
            scheduleImageAdvance(activeState ? (activeState.interval || 5.0) : 5.0);
            hideOsd();
            notifyPauseState(false);
          }}
        }}
      }}
    }}

    async function pollState() {{
      try {{
        const response = await fetch(`${{stateUrl}}?ts=${{Date.now()}}`, {{ cache: "no-store" }});
        const state = await response.json();
        const signature = JSON.stringify({{
          mode: state.mode,
          playlist_name: state.playlist_name,
          items: state.items,
          repeat: state.repeat,
          loop: state.loop,
          interval: state.interval,
          shuffle: state.shuffle,
          transition: state.transition,
          transition_duration_ms: state.transition_duration_ms,
          video_fill_mode: state.video_fill_mode,
          idle_item: state.idle_item,
          banner: state.banner,
        }});
        if (signature !== currentSignature) {{
          currentSignature = signature;
          currentControlToken = state.control ? state.control.token : -1;
          startFromState(state);
        }} else if (state.control && state.control.token !== currentControlToken) {{
          currentControlToken = state.control.token;
          applyControl(state.control);
        }}
        activeState = state;
        applyLiveAudioState(state);
        applyCompanionState(state);
        // Per-playlist visualizer pick lives on the state file. Apply
        // every poll - applyChoice short-circuits if the value hasn't
        // changed, so this is cheap on idle ticks and active mid-
        // stream toggles flip the visual without stopping audio.
        try {{ audioVis.applyChoice(state.visualizer || "random"); }} catch (_) {{}}
      }} catch (error) {{
        // ignore transient read errors while state file is being replaced
      }}
    }}

    /* ============================================================
       Audio companion playback. Hidden <audio> element that loops
       through state.companion.items when companion.active is true.
       Falls back to muting the visual when companion.mute_visual is
       set (override-embedded-audio path).
       ============================================================ */
    let companionToken = -1;
    let companionQueueIndex = 0;
    let companionItems = [];
    let companionRepeat = true;

    function applyCompanionState(state) {{
      const companionAudio = document.getElementById("companionAudio");
      if (!companionAudio) return;
      const comp = state.companion || null;
      const newToken = comp && typeof comp.token === "number" ? comp.token : -1;
      const wantActive = !!(comp && comp.active && Array.isArray(comp.items) && comp.items.length);

      // Video muting for the override path is handled by
      // startFromState + applyLiveAudioState reading
      // state.video_mute_override (set by BrowserController.
      // set_companion). Companion JS only manages the <audio>
      // element here.

      // Volume: companion follows the surface's volume + user mute,
      // so the volume slider on the operator's UI always controls
      // what they hear regardless of which element is playing.
      const userMuted = !!state.muted;
      const userVolume = Math.max(0, Math.min(1, (state.volume || 0) / 100));
      companionAudio.muted = userMuted;
      companionAudio.volume = userVolume;

      if (newToken !== companionToken) {{
        companionToken = newToken;
        companionItems = wantActive ? comp.items.map((it) => it.src || "").filter(Boolean) : [];
        companionRepeat = !!(comp && comp.repeat !== false);
        companionQueueIndex = 0;
        if (companionItems.length) {{
          companionAudio.src = companionItems[0];
          companionAudio.loop = false;
          companionAudio.load();
          companionAudio.play().catch(() => {{ /* best-effort */ }});
        }} else {{
          try {{ companionAudio.pause(); }} catch (_) {{}}
          companionAudio.removeAttribute("src");
          companionAudio.load();
        }}
      }} else if (wantActive && companionAudio.paused) {{
        companionAudio.play().catch(() => {{}});
      }} else if (!wantActive && !companionAudio.paused) {{
        try {{ companionAudio.pause(); }} catch (_) {{}}
      }}
    }}

    document.getElementById("companionAudio").addEventListener("ended", function () {{
      if (!companionItems.length) return;
      companionQueueIndex = (companionQueueIndex + 1) % companionItems.length;
      if (companionQueueIndex === 0 && !companionRepeat) {{
        return;
      }}
      this.src = companionItems[companionQueueIndex];
      this.play().catch(() => {{}});
    }});

    window.addEventListener("resize", () => {{
      const activeStage = getActiveStage();
      if (activeStage.video.style.display === "block") {{
        const fillMode = activeState ? (activeState.video_fill_mode || "contain") : "contain";
        fitMedia(
          activeStage.video,
          activeStage.video.videoWidth || 1,
          activeStage.video.videoHeight || 1,
          fillMode === "cover" ? "cover" : "contain"
        );
      }} else if (activeStage.image.style.display === "block") {{
        fitMedia(activeStage.image, activeStage.image.naturalWidth || 1, activeStage.image.naturalHeight || 1);
      }}
    }});
    window.addEventListener("mousemove", hideCursor, {{ passive: true }});
    window.addEventListener("pointermove", hideCursor, {{ passive: true }});
    window.addEventListener("focus", hideCursor);
    window.setInterval(hideCursor, 1000);
    hideCursor();
    pollState();
    window.setInterval(pollState, {poll_ms});
  </script>
</body>
</html>
"""
