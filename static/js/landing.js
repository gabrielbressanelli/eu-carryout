(() => {
  const tour = document.querySelector("[data-tour]");
  if (!tour) return;
  const scenes = [...tour.querySelectorAll("[data-scene]")];
  const progress = [...tour.querySelectorAll("[data-progress]")];
  const count = tour.querySelector("[data-tour-count]");
  const caption = tour.querySelector("[data-tour-caption]");
  const captions = [
    "Set up a new restaurant in one clear workspace.",
    "Build the menu your customers already know.",
    "Price portions and add-ons with simple rules.",
    "Give customers a branded, easy ordering experience.",
    "Send every paid order to the people and tools that need it."
  ];
  let index = 0;
  let timer;
  const show = (next) => {
    index = next % scenes.length;
    scenes.forEach((scene, sceneIndex) => scene.classList.toggle("is-active", sceneIndex === index));
    progress.forEach((bar, barIndex) => bar.classList.toggle("is-active", barIndex === index));
    count.textContent = `0${index + 1} / 0${scenes.length}`;
    caption.textContent = captions[index];
  };
  const start = () => {
    clearInterval(timer);
    timer = setInterval(() => show(index + 1), 3400);
  };
  show(0);
  start();
  document.addEventListener("visibilitychange", () => document.hidden ? clearInterval(timer) : start());
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    clearInterval(timer);
    show(0);
  }
})();
