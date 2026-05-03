// Forces video sites off AV1 by lying about codec support. The Pi 5
// has no AV1 hardware decode block and libdav1d on the CPU caps
// real-world 1080p60 playback at ~50% delivered frames. By making
// the page believe AV1 isn't supported, the player falls back to
// VP9 or H.264 (lighter on this CPU and, for HEVC sites, on V3D
// hardware decode).
//
// Implementation: shadow MediaSource.isTypeSupported and the static
// HTMLVideoElement.canPlayType to return false / empty for any AV1
// query. Same approach as the well-known h264ify / enhanced-h264ify
// extensions - YouTube, Vimeo, Twitch, etc. all probe those two
// functions to pick a codec, so flipping them is sufficient.

(function () {
  const isAv1 = function (s) {
    if (!s || typeof s !== "string") return false;
    const lower = s.toLowerCase();
    return lower.indexOf("av01") !== -1 || lower.indexOf("av1") !== -1;
  };

  const origIsTypeSupported = window.MediaSource && window.MediaSource.isTypeSupported;
  if (origIsTypeSupported) {
    window.MediaSource.isTypeSupported = function (type) {
      if (isAv1(type)) return false;
      return origIsTypeSupported.call(this, type);
    };
  }

  const proto = window.HTMLVideoElement && window.HTMLVideoElement.prototype;
  if (proto && proto.canPlayType) {
    const origCanPlayType = proto.canPlayType;
    proto.canPlayType = function (type) {
      if (isAv1(type)) return "";
      return origCanPlayType.call(this, type);
    };
  }
})();
