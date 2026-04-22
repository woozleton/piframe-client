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
) -> str:
    """Render the self-contained Chromium kiosk page."""
    show_hud_css = "block" if show_hud else "none"
    show_hud_js = "true" if show_hud else "false"
    reset_delay_ms = max(transition_duration_ms + 50, 150)
    hidden_cursor = (
        'url("data:image/png;base64,'
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9s0qK1wAAAAASUVORK5CYII=") 0 0, none'
    )
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
  </style>
</head>
<body>
  <div class="frame">
    <div id="stage0" class="stage active">
      <img id="bgImage0" class="backdrop" alt="">
      <img id="image0" class="media image" alt="">
      <video id="video0" class="media video" muted playsinline preload="auto"></video>
    </div>
    <div id="stage1" class="stage">
      <img id="bgImage1" class="backdrop" alt="">
      <img id="image1" class="media image" alt="">
      <video id="video1" class="media video" muted playsinline preload="auto"></video>
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
  <script>
    const stateUrl = {json.dumps(state_file_uri)};
    const hiddenCursor = getComputedStyle(document.documentElement)
      .getPropertyValue('--hidden-cursor')
      .trim() || 'none';
    const stages = [
      {{
        root: document.getElementById("stage0"),
        bgImage: document.getElementById("bgImage0"),
        image: document.getElementById("image0"),
        video: document.getElementById("video0"),
      }},
      {{
        root: document.getElementById("stage1"),
        bgImage: document.getElementById("bgImage1"),
        image: document.getElementById("image1"),
        video: document.getElementById("video1"),
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
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 4.5a1 1 0 0 1 1.7.7v13.6a1 1 0 0 1-1.7.7L9.7 15H6a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2h3.7l4.8-4.5Zm3.9 1.8a1 1 0 0 1 1.4 0 8 8 0 0 1 0 11.4 1 1 0 1 1-1.4-1.4 6 6 0 0 0 0-8.6 1 1 0 0 1 0-1.4Zm-2.8 2.8a1 1 0 0 1 1.4 0 4 4 0 0 1 0 5.7 1 1 0 0 1-1.4-1.4 2 2 0 0 0 0-2.9 1 1 0 0 1 0-1.4Z"></path></svg>';
    }}

    function showOsd(kind, value = "", label = "", percent = null, durationMs = 1000) {{
      if (osdTimer) {{
        window.clearTimeout(osdTimer);
        osdTimer = null;
      }}
      osdIconEl.innerHTML = osdIconSvg(kind);
      osdValueEl.textContent = value || "";
      osdLabelEl.textContent = label || "";
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
      stage.bgImage.onload = null;
      stage.image.onload = null;
      stage.video.onloadedmetadata = null;
      stage.video.onended = null;
      stage.bgImage.classList.remove("ready");
      stage.image.classList.remove("ready");
      stage.video.classList.remove("ready");
      stage.bgImage.style.display = "none";
      stage.image.style.display = "none";
      stage.video.style.display = "none";
      stage.video.pause();
      stage.video.removeAttribute("src");
      stage.video.load();
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
      hideOsd();
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
      const token = pendingAdvanceToken;
      window.clearInterval(intervalHandle);
      intervalHandle = window.setInterval(() => {{
        if (token !== pendingAdvanceToken) {{
          return;
        }}
        advancePlaylist(1);
      }}, perItemSeconds * 1000);
    }}

    function prepareStage(stage, item, state, perItemSeconds) {{
      resetStage(stage);
      if (item.kind === "video") {{
        const isSingleRepeatingPlaylist =
          state.mode === "playlist" &&
          !!state.repeat &&
          Array.isArray(state.items) &&
          state.items.length === 1;
        const fillMode = state.video_fill_mode || "contain";
        stage.video.style.display = "block";
        stage.video.dataset.desiredMuted = (!!state.muted).toString();
        stage.video.dataset.desiredVolume = Math.max(0, Math.min(1, (state.volume || 0) / 100)).toString();
        stage.video.muted = !!state.muted;
        stage.video.volume = Math.max(0, Math.min(1, (state.volume || 0) / 100));
        stage.video.loop = state.mode === "single" ? !!state.loop : isSingleRepeatingPlaylist;
        stage.video.preload = "auto";
        stage.video.onloadedmetadata = () => {{
          const foregroundMode = fillMode === "cover" ? "cover" : "contain";
          fitMedia(stage.video, stage.video.videoWidth || 1, stage.video.videoHeight || 1, foregroundMode);
          stage.video.classList.add("ready");
        }};
        stage.video.onerror = () => {{
          showBanner(itemErrorMessage(item), "error");
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
        stage.video.src = item.src;
        stage.video.currentTime = 0;
      }} else {{
        stage.image.style.display = "block";
        stage.image.onload = () => {{
          fitMedia(stage.image, stage.image.naturalWidth || 1, stage.image.naturalHeight || 1, "contain");
          stage.image.classList.add("ready");
        }};
        stage.image.onerror = () => {{
          showBanner(itemErrorMessage(item), "error");
        }};
        stage.image.src = item.src;
      }}
      setHud(item, state, perItemSeconds);
    }}

    function renderItem(item, state, perItemSeconds) {{
      if (!item) {{
        return;
      }}
      const targetStage = getInactiveStage();
      prepareStage(targetStage, item, state, perItemSeconds);
      activateStage(targetStage);
      const previousStage = getInactiveStage();
      window.setTimeout(() => {{
        resetStage(previousStage);
      }}, {reset_delay_ms});
      if (item.kind === "video") {{
        stagePlay(targetStage.video);
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

    function applyLiveAudioState(state) {{
      for (const stage of stages) {{
        stage.video.dataset.desiredMuted = (!!state.muted).toString();
        stage.video.dataset.desiredVolume = Math.max(0, Math.min(1, (state.volume || 0) / 100)).toString();
        stage.video.muted = !!state.muted;
        stage.video.volume = Math.max(0, Math.min(1, (state.volume || 0) / 100));
      }}
      const nextVolume = Number(state.volume || 0);
      const nextMuted = !!state.muted;
      if (lastVolume !== null && (nextVolume !== lastVolume || nextMuted !== lastMuted)) {{
        const kind = nextMuted || nextVolume <= 0 ? "mute" : "volume";
        const value = nextMuted || nextVolume <= 0 ? "" : `${{Math.round(nextVolume)}}%`;
        const label = nextMuted || nextVolume <= 0 ? "Muted" : "";
        showOsd(kind, value, label, nextMuted ? 0 : nextVolume, 1000);
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
    }}

    function startFromState(state) {{
      activeState = state;
      activeIndex = 0;
      hideOsd();
      stopTimers();
      if (!state.items || !state.items.length) {{
        showIdle(state.idle_item);
        return;
      }}
      showBanner(state.banner ? state.banner.message : "", state.banner ? state.banner.level : "warning");
      const perItemSeconds = state.interval || 5.0;
      renderItem(state.items[activeIndex], state, perItemSeconds);
    }}

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
          }} else {{
            activeStage.video.pause();
            showOsd("pause", "", "", null, 1000);
          }}
        }} else {{
          if (intervalHandle) {{
            stopTimers();
            showOsd("pause", "", "", null, 1000);
          }} else {{
            scheduleImageAdvance(activeState ? (activeState.interval || 5.0) : 5.0);
            hideOsd();
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
      }} catch (error) {{
        // ignore transient read errors while state file is being replaced
      }}
    }}

    hideCursor();
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
    pollState();
    window.setInterval(pollState, {poll_ms});
  </script>
</body>
</html>
"""
