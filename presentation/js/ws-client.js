(() => {
  const wsUrl = "ws://localhost:8760/ws/slide-control";
  const hiddenSlides = Array.from(document.querySelectorAll('[data-visibility="hidden"]'));

  const hideSlide = (slide) => {
    slide.setAttribute("data-visibility", "hidden");
    slide.setAttribute("aria-hidden", "true");
    slide.style.display = "none";
  };

  const showSlide = (slide) => {
    slide.setAttribute("data-visibility", "visible");
    slide.setAttribute("aria-hidden", "false");
    slide.style.display = "";
  };

  hiddenSlides.forEach(hideSlide);

  const deck = new Reveal({
    hash: true,
    slideNumber: true,
    transition: "fade",
  });

  deck.initialize();

  const navigateToSlide = (slideId, revealHidden) => {
    const slide = document.getElementById(slideId);
    if (!slide) {
      return;
    }
    if (revealHidden || slide.getAttribute("data-visibility") === "hidden") {
      showSlide(slide);
    }
    deck.sync();
    deck.slide(slide);
  };

  // Tracks the in-progress/finalized generated-slide <section> per pain point, so streamed
  // partials update the same element instead of appending a duplicate on every partial and
  // so the final `inject_generated_slide` message finishes that element rather than creating
  // a second one.
  const generatedSlideSections = new Map();

  const slideSectionKey = (painPoint) =>
    typeof painPoint === "string" && painPoint.trim() ? painPoint.trim() : "__default__";

  const renderGeneratedSlideContent = (section, slide) => {
    section.innerHTML = "";
    const badge = document.createElement("div");
    badge.className = "ai-generated-badge";
    badge.textContent = "AI Generated";
    section.appendChild(badge);
    const title = document.createElement("h2");
    title.textContent = slide.title.slice(0, 160);
    section.appendChild(title);
    if (typeof slide.description === "string" && slide.description.trim()) {
      const description = document.createElement("p");
      description.textContent = slide.description.slice(0, 1000);
      section.appendChild(description);
    }
    if (Array.isArray(slide.metrics) && slide.metrics.length) {
      const metrics = document.createElement("ul");
      slide.metrics.slice(0, 2).forEach((metric) => {
        if (typeof metric === "string" && metric.trim()) {
          const item = document.createElement("li");
          item.textContent = metric.slice(0, 200);
          metrics.appendChild(item);
        }
      });
      section.appendChild(metrics);
    }
  };

  // Returns the tracked section for this pain point, creating (and appending) one if it
  // doesn't exist yet or fell out of the DOM. `created` tells the caller whether this is the
  // section's first paint, so it knows whether to bring it into view.
  const ensureGeneratedSlideSection = (key, painPoint) => {
    const existing = generatedSlideSections.get(key);
    if (existing && existing.isConnected) {
      return { section: existing, created: false };
    }
    const slidesRoot = document.querySelector(".reveal .slides");
    if (!slidesRoot) {
      return { section: null, created: false };
    }
    const section = document.createElement("section");
    section.className = "case-slide case-slide--generated generated-slide";
    if (typeof painPoint === "string" && painPoint.trim()) {
      section.dataset.painPoint = painPoint.trim();
    }
    slidesRoot.appendChild(section);
    generatedSlideSections.set(key, section);
    return { section, created: true };
  };

  const navigateToGeneratedSlide = (section) => {
    deck.sync();
    const slidesRoot = document.querySelector(".reveal .slides");
    if (!slidesRoot) {
      return;
    }
    const topLevelSlides = Array.from(slidesRoot.children).filter(
      (child) => child.tagName === "SECTION",
    );
    const newIndex = Math.max(0, topLevelSlides.indexOf(section));
    deck.slide(newIndex, 0);
  };

  const injectGeneratedSlide = (slide, painPoint) => {
    if (!slide || typeof slide !== "object" || typeof slide.title !== "string") {
      return;
    }
    const { section } = ensureGeneratedSlideSection(slideSectionKey(painPoint), painPoint);
    if (!section) {
      return;
    }
    renderGeneratedSlideContent(section, slide);
    navigateToGeneratedSlide(section);
  };

  const updateGeneratedSlidePartial = (slide, painPoint) => {
    if (!slide || typeof slide !== "object" || typeof slide.title !== "string") {
      return;
    }
    const { section, created } = ensureGeneratedSlideSection(slideSectionKey(painPoint), painPoint);
    if (!section) {
      return;
    }
    renderGeneratedSlideContent(section, slide);
    if (created) {
      // First content for this pain point: bring it into view so the streaming growth is
      // actually visible instead of building up off-screen. Later partials update the same
      // slide in place without re-navigating.
      navigateToGeneratedSlide(section);
    } else {
      deck.sync();
    }
  };

  const handleMessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.action === "navigate_to_case" && payload.slide_id) {
        navigateToSlide(payload.slide_id, true);
      }
      if (payload.action === "navigate_to_slide" && payload.slide_id) {
        navigateToSlide(payload.slide_id, false);
      }
      if (payload.action === "inject_generated_slide") {
        injectGeneratedSlide(payload.slide, payload.pain_point);
      }
      if (payload.action === "update_generated_slide_partial") {
        updateGeneratedSlidePartial(payload.slide, payload.pain_point);
      }
    } catch (error) {
      console.error("Invalid slide message", error);
    }
  };

  const connectSocket = () => {
    const socket = new WebSocket(wsUrl);

    socket.addEventListener("message", handleMessage);

    socket.addEventListener("close", () => {
      setTimeout(connectSocket, 3000);
    });

    socket.addEventListener("error", () => {
      socket.close();
    });
  };

  connectSocket();
})();
