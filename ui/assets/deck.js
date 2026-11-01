function go(rel) {
  const a = document.querySelector(`[data-nav="${rel}"]`);
  if (a) window.location.href = a.getAttribute("href");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") go("prev");
  if (e.key === "ArrowRight") go("next");
});

// Reuse the shared UI runtime so deck pages get theme behavior as well.
import("/ui/assets/site.js?v=20260330")
  .then((m) => m.installAssetGuard())
  .catch(() => {
    // Keep deck navigation functional even if shared assets fail.
  });
