document.addEventListener("DOMContentLoaded", () => {
  const search = document.querySelector("#event-search");
  const month = document.querySelector("#event-month");
  const cards = [...document.querySelectorAll(".event-card")];
  const visibleCount = document.querySelector("#visible-events");
  const empty = document.querySelector("#filtered-empty");

  if (!search || !month) return;
  const filterEvents = () => {
    const query = search.value.trim().toLocaleLowerCase("es");
    let visible = 0;
    cards.forEach((card) => {
      const matches = card.dataset.search.includes(query)
        && (!month.value || card.dataset.month === month.value);
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    visibleCount.textContent = visible;
    empty.hidden = visible > 0;
  };
  search.addEventListener("input", filterEvents);
  month.addEventListener("change", filterEvents);

  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.08 });
    cards.forEach((card) => observer.observe(card));
  } else {
    cards.forEach((card) => card.classList.add("is-visible"));
  }
});
