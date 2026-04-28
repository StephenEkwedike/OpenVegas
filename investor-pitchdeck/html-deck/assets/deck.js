function go(rel) {
  const a = document.querySelector(`[data-nav="${rel}"]`);
  if (a) window.location.href = a.getAttribute("href");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") go("prev");
  if (e.key === "ArrowRight") go("next");
});
