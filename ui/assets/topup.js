import { byId } from "/ui/assets/site.js";
import { apiFetch, apiJson, getLoginHref } from "/ui/assets/page-auth.js?v=20260330";

function topupIdFromPath() {
  const segs = window.location.pathname.split("/").filter(Boolean);
  return segs[segs.length - 1] || "";
}

function setState(state, metaText) {
  const statusPill = byId("statusPill");
  const meta = byId("meta");
  const stateCopy = byId("stateCopy");
  const stateTextMap = {
    paid: "Funding confirmed. Your balance is ready for use.",
    pending: "Payment created. Complete checkout to finish funding.",
    checkout_created: "Payment created. Complete checkout to finish funding.",
    created: "Payment created. Complete checkout to finish funding.",
    failed: "This funding attempt did not complete. Review the status and try again.",
    expired: "This checkout session expired. Start a new top-up to continue.",
    manual_reconciliation_required: "Payment was detected but needs review before funds are finalized.",
    unauthorized: "You need to be signed in to view this top-up.",
    not_found: "This top-up could not be found.",
  };
  if (statusPill) {
    statusPill.dataset.state = state;
    statusPill.textContent = state;
  }
  if (meta && metaText) {
    meta.textContent = metaText;
  }
  if (stateCopy) {
    stateCopy.textContent = stateTextMap[state] || stateTextMap.pending;
  }
}

export async function loadTopup() {
  const topupId = topupIdFromPath();
  if (!topupId || topupId === "topup") {
    setState("error", "Missing topup_id in URL path.");
    return;
  }

  const checkoutLink = byId("checkoutLink");
  const qrImage = byId("qrImage");
  const failure = byId("failureReason");
  const loginLink = byId("loginLink");

  if (loginLink) loginLink.href = getLoginHref(`/ui/topup/${topupId}`);

  try {
    const data = await apiJson(`/billing/topups/${encodeURIComponent(topupId)}`, {
      method: "GET",
    });

    const status = data.manual_reconciliation_required
      ? "manual_reconciliation_required"
      : String(data.status || "unknown");

    setState(status, `${data.topup_id} · $${data.amount_usd || "?"} · ${data.mode || "unknown"}`);

    if (checkoutLink) {
      if (data.checkout_url) {
        checkoutLink.hidden = false;
        checkoutLink.href = data.checkout_url;
        checkoutLink.textContent = data.checkout_url;
      } else {
        checkoutLink.hidden = true;
      }
    }

    if (qrImage) {
      if (data.checkout_url) {
        const qrRes = await apiFetch(`/billing/topups/${encodeURIComponent(topupId)}/qr.svg`, {
          method: "GET",
        });
        if (qrRes.ok) {
          const svg = await qrRes.text();
          qrImage.hidden = false;
          qrImage.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
          if (svg.includes("QR unavailable in this runtime")) {
            setState(status, `${data.topup_id} · $${data.amount_usd || "?"} · ${data.mode || "unknown"} · QR fallback rendered (see reason in image)`);
          }
        } else {
          qrImage.hidden = true;
          const detail = await qrRes.text().catch(() => "");
          setState(
            status,
            `${data.topup_id} · $${data.amount_usd || "?"} · ${data.mode || "unknown"} · QR fetch failed (${qrRes.status})${detail ? `: ${detail.slice(0, 120)}` : ""}`,
          );
        }
      } else {
        qrImage.hidden = true;
      }
    }

    if (failure) {
      if (data.failure_reason) {
        failure.hidden = false;
        failure.textContent = String(data.failure_reason);
      } else {
        failure.hidden = true;
      }
    }
  } catch (err) {
    const msg = String(err);
    if (msg.includes("401") || msg.includes("403")) {
      setState("unauthorized", "You need to sign in to view this top-up.");
      return;
    }
    if (msg.includes("404")) {
      setState("not_found", "Top-up not found.");
      return;
    }
    setState("error", `Failed to load top-up: ${msg}`);
  }
}
