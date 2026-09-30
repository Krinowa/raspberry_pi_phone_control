"use strict";

(() => {
  const SWIPE_THRESHOLD_PX = 8;

  function point(event, element) {
    const box = element.getBoundingClientRect();
    return [
      (event.clientX - box.left) / box.width,
      (event.clientY - box.top) / box.height,
    ];
  }

  function classify(start, end, element, elapsedMs) {
    const box = element.getBoundingClientRect();
    const distance = Math.hypot(
      (end[0] - start[0]) * box.width,
      (end[1] - start[1]) * box.height,
    );
    return {
      type: distance > SWIPE_THRESHOLD_PX ? "swipe" : "tap",
      start,
      end,
      duration_ms: Math.max(50, Math.min(60000, Math.round(elapsedMs))),
    };
  }

  window.phonePreviewGestures = Object.freeze({ point, classify });
})();
