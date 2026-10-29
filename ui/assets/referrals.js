import { renderTopNav, renderFounderLinks, installAssetGuard } from "/ui/assets/site.js?v=20260330";
import { apiJson, authProbe, getLoginHref, subscribeAuthState } from "/ui/assets/page-auth.js?v=20260330";

renderTopNav();
renderFounderLinks();
installAssetGuard();

const $ = (id) => document.getElementById(id);
const invite = new URLSearchParams(location.search).get("invite") || "";
let generation = 0;
let busy = false;
let account = null;
let active = false;
const pending = new Map();

function tell(text) { $("referral-status").textContent = text; }
function disable() {
  for (const kind of ["trial", "reward"]) {
    $(`claim-${kind}`).disabled = true;
    $(`${kind}-item`).disabled = true;
  }
}

function render(data) {
  if (!/^\/ui\/referrals\?invite=[a-f0-9]{32}$/.test(data.invite_path || "")
      || data.required_friends !== 5 || data.trial_days !== 7
      || !Number.isInteger(data.qualified_friends) || data.qualified_friends < 0 || data.qualified_friends > 5
      || !Array.isArray(data.eligible_items) || !Array.isArray(data.grants)) throw new Error("Invalid promotion response.");
  account = data;
  $("referral-account").hidden = false;
  $("referral-login").hidden = true;
  $("invite-link").value = new URL(data.invite_path, location.origin).href;
  $("referral-progress").textContent = `${data.qualified_friends} of 5 verified friends have completed a first genuine session.`;
  $("referral-meter").value = data.qualified_friends;
  for (const kind of ["trial", "reward"]) {
    const select = $(`${kind}-item`);
    const old = select.value;
    select.replaceChildren();
    for (const item of data.eligible_items.slice(0, 100)) {
      if (!/^[a-zA-Z0-9._-]{1,128}$/.test(item.id) || typeof item.name !== "string") continue;
      const option = document.createElement("option");
      option.value = item.id; option.textContent = item.name; select.append(option);
    }
    if ([...select.options].some(o => o.value === old)) select.value = old;
    const grant = data.grants.find(g => g.kind === kind);
    if (grant) pending.delete(kind);
    const date = grant?.expires_at ? new Date(grant.expires_at) : null;
    $(`${kind}-state`).textContent = grant
      ? grant.status !== "active" ? "This grant is no longer active."
        : date ? `Trial ${date <= new Date() ? "expired" : "expires"}: ${date.toLocaleString()}. The server enforces expiry.`
          : "Your emote choice is recorded. Open your library to equip it."
      : !select.options.length ? "No eligible emotes are currently available."
        : kind === "trial" && !data.attributed ? "A verified new-account invitation is required."
          : kind === "reward" && !data.can_claim_reward ? "Five verified friends with genuine sessions are required."
            : "Choose one emote. This choice cannot be reset.";
    const allowed = !busy && data[`can_claim_${kind}`] === true && !!select.options.length;
    select.disabled = !allowed;
    $(`claim-${kind}`).disabled = !allowed;
  }
}

async function refresh(message = "") {
  if (!active) return;
  const current = ++generation;
  disable();
  try {
    const data = await apiJson("/referrals/me");
    if (current !== generation) return;
    render(data);
    tell(message || "Your referral progress is verified by the server.");
  } catch (error) {
    if (current !== generation) return;
    account = null;
    $("referral-account").hidden = true;
    tell(error.status === 401 ? "Sign in again to view your referrals." : "Referrals are unavailable or your account is not eligible. Verify your email and retry later.");
  }
}

async function claim(kind) {
  if (busy || !account || account[`can_claim_${kind}`] !== true) return;
  const itemId = $(`${kind}-item`).value;
  if (!account.eligible_items.some(item => item.id === itemId)) return;
  const choice = pending.get(kind);
  if (choice && choice.item_id !== itemId) {
    tell("A previous claim is unconfirmed. Refresh progress before choosing a different emote."); return;
  }
  const payload = choice || { kind, item_id: itemId, idempotency_key: crypto.randomUUID() };
  pending.set(kind, payload);
  busy = true; disable();
  const current = ++generation;
  let message = "";
  try {
    await apiJson("/referrals/claim", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    if (current !== generation) return;
    pending.delete(kind);
    tell("Choice recorded. Checking your library status...");
  } catch {
    message = "Claim could not be confirmed. Check your progress before retrying; trials are not extended by retries.";
  } finally {
    busy = false;
    if (current === generation) await refresh(message);
  }
}

$("copy-invite").addEventListener("click", async () => {
  if (!account) return;
  try { await navigator.clipboard.writeText($("invite-link").value); tell("Invite link copied."); }
  catch { tell("Select your invite link to copy it."); }
});
$("refresh-progress").addEventListener("click", () => { if (!busy) void refresh(); });
for (const kind of ["trial", "reward"]) $(`claim-${kind}`).addEventListener("click", () => void claim(kind));
subscribeAuthState(({authState}) => {
  if (authState === "signed_out") {
    ++generation; account = null; pending.clear(); disable();
    $("referral-account").hidden = true;
    $("invite-link").value = "";
    if (active) {
      $("referral-login").hidden = false;
      tell("Sign in to view your referrals.");
    }
  }
});

async function start() {
  let current = ++generation;
  const next = location.pathname + location.search;
  $("login-link").href = getLoginHref(next);
  const signup = new URL(getLoginHref(next), location.origin);
  signup.searchParams.set("mode", "signup"); $("signup-link").href = signup.pathname + signup.search;
  try {
    const response = await fetch("/referrals/config", {cache:"no-store", signal:AbortSignal.timeout(10000)});
    if (!response.ok || (await response.json()).enabled !== true) {
      tell("The referral promotion is not active. No trial or reward is being offered yet."); return;
    }
    active = true;
    if (invite) {
      if (!/^[a-f0-9]{32}$/.test(invite)) { tell("This invitation is invalid."); return; }
      const saved = await fetch("/referrals/visit", {method:"POST", credentials:"same-origin",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({code:invite}), signal:AbortSignal.timeout(10000)});
      if (!saved.ok) { tell("This invitation is unavailable. No trial has started."); return; }
      $("invite-notice").textContent = "Invitation saved. Create a new account and verify your email within 24 hours of your first visit to choose your trial. Existing accounts are not eligible.";
    }
    current = ++generation;
    const auth = await authProbe();
    if (current !== generation) return;
    if (!auth.ok) {
      $("referral-login").hidden = false;
      tell("Sign in or create an account to continue."); return;
    }
    if (invite) {
      try { await apiJson("/referrals/attribute", {method:"POST", headers:{"Content-Type":"application/json"},body:"{}"}); }
      catch {
        if (current === generation) await refresh("This invitation could not be applied. Only new, verified accounts are eligible.");
        return;
      }
    }
    if (current === generation) await refresh();
  } catch {
    if (current === generation) tell("Referral availability could not be checked. No claim has been made.");
  }
}
void start();
