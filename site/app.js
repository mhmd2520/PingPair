/* PingPair site — progressive enhancement only.
   The page is fully usable with JavaScript disabled: the gallery shows the
   Dark screenshots, every section is visible (the .js class gates the
   scroll-reveal hide), and the lightbox simply doesn't open. Nothing here is
   required to read the content or download the app. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- Scroll-reveal: fade/slide sections in as they enter the viewport ----
  var revealables = document.querySelectorAll("[data-reveal]");
  if (revealables.length) {
    if (reduceMotion || !("IntersectionObserver" in window)) {
      // No motion (or no observer support): just show everything.
      revealables.forEach(function (el) { el.classList.add("in"); });
    } else {
      // threshold 0 (not a fraction): a fractional threshold's crossing can be
      // skipped entirely during fast scrolling, leaving the element invisible.
      var io = new IntersectionObserver(function (entries, obs) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("in");
            obs.unobserve(entry.target);
          }
        });
      }, { rootMargin: "0px 0px -8% 0px", threshold: 0 });
      revealables.forEach(function (el) { io.observe(el); });

      // Safety sweep: if the observer missed anything (fast flicks, anchor
      // jumps), reveal every element whose top has already entered the
      // viewport. Runs at most once per frame while scrolling.
      var sweepPending = false;
      var sweep = function () {
        sweepPending = false;
        var limit = window.innerHeight * 0.92;
        revealables.forEach(function (el) {
          if (!el.classList.contains("in") && el.getBoundingClientRect().top < limit) {
            el.classList.add("in");
            io.unobserve(el);
          }
        });
      };
      window.addEventListener("scroll", function () {
        if (!sweepPending) {
          sweepPending = true;
          requestAnimationFrame(sweep);
        }
      }, { passive: true });
    }
  }

  // ---- Sticky header gains a border + deeper bg once you scroll ----
  var header = document.getElementById("site-header");
  if (header) {
    var onScroll = function () {
      header.classList.toggle("scrolled", window.scrollY > 8);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  // ---- Scroll-spy: underline the nav link of the section in view ----
  // Only same-page anchors participate (docs/changelog nav links are plain
  // hrefs, so this block is inert on those pages).
  var spyPairs = [];
  document.querySelectorAll('.nav-links a[href^="#"]').forEach(function (link) {
    var section = document.getElementById(link.getAttribute("href").slice(1));
    if (section) spyPairs.push({ link: link, section: section });
  });
  if (spyPairs.length) {
    var spy = function () {
      // The "reading line" sits just below the sticky header. The active
      // section is the last one whose top has crossed it, so the highlight
      // persists through unlinked sections (stats, trust) until the next
      // linked one takes over. Nothing is active above the first section.
      var line = 120;
      var atBottom = window.innerHeight + window.scrollY >=
        document.documentElement.scrollHeight - 2;
      var active = null;
      spyPairs.forEach(function (pair) {
        if (pair.section.getBoundingClientRect().top <= line) active = pair.link;
      });
      // A short final section may never reach the line; at the very bottom
      // of the page hand the highlight to the last linked section.
      if (atBottom) active = spyPairs[spyPairs.length - 1].link;
      spyPairs.forEach(function (pair) {
        pair.link.classList.toggle("is-active", pair.link === active);
      });
    };
    var spyQueued = false;
    window.addEventListener("scroll", function () {
      if (!spyQueued) {
        spyQueued = true;
        requestAnimationFrame(function () {
          spyQueued = false;
          spy();
        });
      }
    }, { passive: true });
    window.addEventListener("resize", spy, { passive: true });
    spy();
  }

  // ---- Screenshot Light/Dark toggle (echoes the app's theme switcher) ----
  var toggle = document.querySelector(".theme-toggle");
  if (toggle) {
    var buttons = toggle.querySelectorAll(".tt-btn");
    var imgs = document.querySelectorAll(".tour-img");

    // Pre-warm the other theme's screenshots once the page is idle, so the
    // first Dark/Light switch swaps from cache instead of stalling on six
    // fresh fetches (the tour images are lazy-loaded, so the alternate set
    // is never requested otherwise). decode() also pays the decode cost off
    // the click path.
    var warmAlternates = function () {
      imgs.forEach(function (img) {
        ["data-dark", "data-light"].forEach(function (attr) {
          var url = img.getAttribute(attr);
          if (url && url !== img.getAttribute("src")) {
            var pre = new Image();
            pre.decoding = "async";
            pre.src = url;
            if (pre.decode) pre.decode().catch(function () {});
          }
        });
      });
    };
    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(warmAlternates, { timeout: 3000 });
    } else {
      window.setTimeout(warmAlternates, 1500);
    }

    toggle.addEventListener("click", function (e) {
      var btn = e.target.closest(".tt-btn");
      if (!btn) return;
      var theme = btn.getAttribute("data-theme");
      buttons.forEach(function (b) {
        var on = b === btn;
        b.classList.toggle("is-active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      imgs.forEach(function (img) {
        var next = img.getAttribute("data-" + theme);
        if (next) img.setAttribute("src", next);
        // Keep the lightbox target in sync with the visible theme.
        var trigger = img.closest(".zoomable");
        if (trigger && next) trigger.setAttribute("data-full", next);
      });
    });
  }

  // ---- Click-to-zoom lightbox ----
  var box = document.getElementById("lightbox");
  var boxImg = document.getElementById("lightbox-img");
  var boxClose = document.getElementById("lightbox-close");
  if (box && boxImg) {
    function open(src, alt) {
      boxImg.setAttribute("src", src);
      boxImg.setAttribute("alt", alt || "");
      box.hidden = false;
      document.body.classList.add("no-scroll");
    }
    function close() {
      box.hidden = true;
      boxImg.setAttribute("src", "");
      document.body.classList.remove("no-scroll");
    }
    document.querySelectorAll(".zoomable").forEach(function (trigger) {
      trigger.addEventListener("click", function () {
        var full = trigger.getAttribute("data-full");
        var img = trigger.querySelector("img");
        if (full) open(full, img ? img.getAttribute("alt") : "");
      });
    });
    box.addEventListener("click", function (e) {
      if (e.target === box || e.target === boxImg) close();
    });
    if (boxClose) boxClose.addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !box.hidden) close();
    });
  }
})();
